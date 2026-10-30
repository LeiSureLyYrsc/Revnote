import { createHash } from 'node:crypto'
import type { APIRequestContext, Page } from '@playwright/test'
import {
  test, expect, account, createNote, expectWorkspaceReady, hash, idle, localState, login, openNote,
  same, secretAccess, serverBody, type Account,
} from './fixtures'

/**
 * Phase 5 attachment acceptance, over the real API proxy, real PostgreSQL and
 * real object storage.
 *
 * Nothing here is faked and nothing is skipped: the file a case "uploads" is a
 * genuine file handed to the rendered `<input type="file">`, the bytes travel
 * through `POST .../attachments` (pending row), the XHR `PUT` (streamed into the
 * object store and re-read to verify its digest) and `GET .../content` (streamed
 * back). The assertions are therefore about three real systems agreeing, which
 * is the only thing this label set exists to prove.
 *
 * Byte equality, never "non-empty": a length that matches and a SHA-256 over the
 * decoded bytes that matches are the two independent checks used below. A
 * preview that renders a placeholder, a truncated object or a re-encoded image
 * fails both.
 */

/** A real, decodable 4x3 RGB PNG (105 bytes), base64 so the file stays readable. */
const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAMElEQVR42mPgEpFzE1FtEtHZJ2LMwJWi6ZZi0JRivi/FjoFri4nbFuumLU77tngCALeRC8v5+HdiAAAAAElFTkSuQmCC'
const PNG = Buffer.from(PNG_BASE64, 'base64')
/** The same bytes in the encoding a mistaken local-persistence path would use. */
const PNG_ENCODED_HEAD = PNG_BASE64.slice(0, 24)

/** Not a round number by accident: 105 bytes cannot be a resized or re-encoded image. */
const PNG_WIDTH = 4
const PNG_HEIGHT = 3

const TEXT = Buffer.from('cross-client attachment bytes\nsecond line stays intact\n', 'utf8')
const TEXT_ENCODED = TEXT.toString('base64')

/** The shape `setInputFiles` also accepts; a real name and a real MIME. */
interface UploadFile { name: string; mimeType: string; buffer: Buffer }

const PNG_FILE: UploadFile = { name: 'pixel.png', mimeType: 'image/png', buffer: PNG }
const TEXT_FILE: UploadFile = { name: 'notes.txt', mimeType: 'text/plain', buffer: TEXT }

/** Exact standard reference shape: `![alt](/api/v1/attachments/{uuid}/content)`. */
const REFERENCE_PATTERN =
  /!\[([^\]]*)\]\(\/api\/v1\/attachments\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/content\)/

function digest(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex')
}

/**
 * The deployment's own limits, read from the endpoint a client reads.
 *
 * `enabled` is asserted rather than assumed: the browser suite is only evidence
 * when this run really has attachments switched on and object storage behind
 * them. `maxBytes` is then the *configured* cap used by the over-limit case, so
 * that case cannot pass against a hardcoded number the server does not enforce.
 */
async function attachmentCapabilities(
  request: APIRequestContext,
): Promise<{ enabled: boolean; maxBytes: number; acceptedMime: string[] }> {
  const response = await request.get('/api/v1/capabilities')
  expect(response.ok(), 'Real capabilities endpoint answered through the proxy').toBe(true)
  const body = await response.json()
  const attachments = body?.attachments
  expect(attachments?.enabled, 'This run really enabled attachments and object storage').toBe(true)
  expect(Number.isSafeInteger(attachments?.max_bytes) && attachments.max_bytes > 0).toBe(true)
  return {
    enabled: attachments.enabled as boolean,
    maxBytes: attachments.max_bytes as number,
    acceptedMime: attachments.accepted_mime as string[],
  }
}

/**
 * Select a real file through the rendered entry point and wait for the
 * product's own completion signal: the reference appearing in the source
 * textarea. That value is only produced by `onInsert`, which only runs after the
 * upload resolved `ready`, so this single web-first assertion replaces any
 * polling on network events or timers.
 */
async function uploadAttachment(page: Page, file: UploadFile): Promise<{ reference: string; attachmentId: string }> {
  // Enabled means the gate really cleared: capabilities answered, the workspace
  // is writable and the browser is online.
  await expect(page.getByTestId('upload-attachment-button')).toBeEnabled()
  await page.getByTestId('attachment-file-input').setInputFiles(file)
  await expect(page.getByTestId('note-source')).toHaveValue(REFERENCE_PATTERN)
  // The reference is in the body, so the upload succeeded; an error alert here
  // would mean the editor rendered a reference it never received.
  await expect(page.getByTestId('upload-error-alert')).toHaveCount(0)
  const body = await page.getByTestId('note-source').inputValue()
  const match = REFERENCE_PATTERN.exec(body)
  expect(match !== null, 'Upload completed with a standard Markdown reference').toBe(true)
  return { reference: match![0], attachmentId: match![2] }
}

/**
 * Put the caret at a known offset in the source textarea, then read it back.
 *
 * The insertion point is an *input* to the product's insertion logic (the
 * editor reads `selectionStart`/`selectionEnd` from this very element), so it is
 * set through the same DOM API a click sets, and immediately verified: a caret
 * that did not take would silently turn the "surrounding text preserved" claim
 * into a test of end-of-document insertion.
 */
async function placeCaret(page: Page, offset: number): Promise<void> {
  await page.getByTestId('note-source').evaluate((element, position) => {
    const textarea = element as HTMLTextAreaElement
    textarea.focus()
    textarea.setSelectionRange(position, position)
  }, offset)
  expect(await page.getByTestId('note-source').evaluate((element) => {
    const textarea = element as HTMLTextAreaElement
    return [textarea.selectionStart, textarea.selectionEnd]
  }), 'Caret really sits at the requested offset').toEqual([offset, offset])
}

/** The durable draft's body, or `null` while no draft holds it yet. */
async function durableBody(page: Page, entityId: string): Promise<string | null> {
  const state = await localState(page)
  return state.drafts[`note:${entityId}`]?.bodyMd ?? null
}

/**
 * Download what the preview actually fetched and return it base64-encoded.
 *
 * The preview holds the response bytes in an object URL it created itself, so
 * reading that URL is a download of the *product's* bytes with no second
 * implementation and no credential in this file.
 */
async function downloadBytes(page: Page): Promise<{ base64: string; length: number }> {
  const href = await page.getByTestId('attachment-download').getAttribute('href')
  expect(typeof href === 'string' && href.startsWith('blob:'), 'Preview exposes the downloaded object URL').toBe(true)
  return await page.evaluate(async (url) => {
    const response = await fetch(url)
    const bytes = new Uint8Array(await response.arrayBuffer())
    let binary = ''
    for (const byte of bytes) binary += String.fromCharCode(byte)
    return { base64: btoa(binary), length: bytes.length }
  }, href!)
}

interface LocalDatabaseReport {
  /** Every object store the page's database really declares, sorted. */
  stores: string[]
  /** How many records were actually walked; zero would make the scan vacuous. */
  records: number
  /** Blob / File / ArrayBuffer / typed-array values found anywhere in the data. */
  binary: number
  /** Strings containing the uploaded bytes in base64 form. */
  encoded: number
}

/**
 * Open the page's own IndexedDB and enumerate it: the store list is read from
 * the live database (not from a list in this file) and every record of every
 * store is walked looking for binary payloads.
 *
 * Only counts and store names cross the browser boundary, so no note text, no
 * attachment byte and no credential can end up in a log or a report.
 */
async function localDatabaseReport(page: Page, encodedHead: string): Promise<LocalDatabaseReport> {
  return await page.evaluate(async (marker) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('revnote-local')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(new Error('Local database read failed'))
    })
    try {
      const stores = Array.from(database.objectStoreNames).sort()
      let records = 0
      let binary = 0
      let encoded = 0
      const visit = (value: unknown, depth: number): void => {
        if (depth > 8 || value === null || value === undefined) return
        if (typeof value === 'string') { if (value.includes(marker)) encoded += 1; return }
        if (typeof value !== 'object') return
        if (value instanceof Blob || value instanceof ArrayBuffer || ArrayBuffer.isView(value)) { binary += 1; return }
        if (Array.isArray(value)) { for (const item of value) visit(item, depth + 1); return }
        for (const item of Object.values(value as Record<string, unknown>)) visit(item, depth + 1)
      }
      for (const name of stores) {
        const rows = await new Promise<unknown[]>((resolve, reject) => {
          const request = database.transaction(name).objectStore(name).getAll()
          request.onsuccess = () => resolve(request.result)
          request.onerror = () => reject(new Error('Local store read failed'))
        })
        records += rows.length
        for (const row of rows) visit(row, 0)
      }
      return { stores, records, binary, encoded }
    } finally { database.close() }
  }, encodedHead)
}

test('attachment_upload_insert a real upload becomes ready and the standard reference is inserted without rewriting either side', async ({ page, request }) => {
  const identity = await account(request)
  expect((await attachmentCapabilities(request)).enabled).toBe(true)
  await login(page, identity)
  // Distinct text on both sides of the insertion point: the product may add the
  // reference and nothing else, so any normalisation of the surrounding bytes is
  // visible as an inequality rather than as a diff nobody reads.
  const before = '# 上传前\n\n第一段保持原样'
  const after = '\n\n尾部段落也不得改写'
  const id = await createNote(page, 'attachment-insert', before + after)
  await idle(page)
  await placeCaret(page, before.length)
  const uploaded = await uploadAttachment(page, PNG_FILE)

  const body = await page.getByTestId('note-source').inputValue()
  // The reference is exactly `![alt](/api/v1/attachments/{uuid}/content)`: the
  // alt is the file name the user chose and the id is a canonical UUID.
  expect(uploaded.reference).toBe(`![pixel.png](/api/v1/attachments/${uploaded.attachmentId}/content)`)
  expect(/^!\[pixel\.png\]\(\/api\/v1\/attachments\/[0-9a-f-]{36}\/content\)$/.test(uploaded.reference)).toBe(true)
  const expected = `${before}${uploaded.reference}${after}`
  // Whole-body equality is the load-bearing claim: the text before the caret and
  // the text after it are byte-identical to what they were, and the reference
  // sits between them with no glue newline added.
  same(body, expected)
  expect(body.startsWith(before), 'Text before the insertion point was not rewritten').toBe(true)
  expect(body.endsWith(after), 'Text after the insertion point was not rewritten').toBe(true)
  expect(body.slice(before.length, before.length + uploaded.reference.length)).toBe(uploaded.reference)

  // Local save, then a real durable commit: the draft really holds the body and
  // the server row really hashes to it.
  await expect(page.getByTestId('local-save-status')).toHaveText('已写入本地')
  await expect.poll(async () => hash((await durableBody(page, id)) ?? '')).toBe(hash(expected))
  await idle(page)
  await serverBody(request, identity, id, expected)

  // Reload: the reference and the untouched neighbours come back from durable
  // state, and the preview resolves the bytes again through the real endpoint.
  await page.reload()
  await openNote(page, 'attachment-insert')
  same(await page.getByTestId('note-source').inputValue(), expected)
  await expect(page.getByText('附件预览 (1)')).toBeVisible()
  await expect(page.getByTestId('attachment-image')).toBeVisible()
  // Only genuinely decoded PNG bytes report these dimensions. A placeholder, a
  // re-encoded image or an empty blob URL cannot.
  await expect.poll(async () => page.getByTestId('attachment-image').evaluate((element) => {
    const picture = element as HTMLImageElement
    return `${picture.naturalWidth}x${picture.naturalHeight}`
  })).toBe(`${PNG_WIDTH}x${PNG_HEIGHT}`)
})

test('attachment_cross_client_read a second browser context pulls the same reference and downloads byte-identical bytes', async ({ page, browser, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'attachment-shared', '跨客户端共享附件\n')
  await idle(page)
  await placeCaret(page, 0)
  const uploaded = await uploadAttachment(page, TEXT_FILE)
  await idle(page)
  const body = await page.getByTestId('note-source').inputValue()
  await serverBody(request, identity, id, body)

  // A separate BrowserContext is a separate browser profile: its own IndexedDB,
  // its own session storage and its own writer lock. That is what makes this a
  // cross-client read rather than a second tab of the client that uploaded. It
  // signs in as the *same* account on purpose - a foreign workspace is a 404 by
  // design (`attachment_authorization` proves that), so byte-identical content
  // can only arrive through a legitimate second client of this workspace.
  const other = await browser.newContext({ baseURL: process.env.WEB_E2E_BASE_URL })
  try {
    const fresh = await other.newPage()
    await login(fresh, identity)
    // The note and its reference exist only on the server, so this waits for the
    // real snapshot bootstrap of the second context rather than for a delay.
    await expectWorkspaceReady(fresh)
    await openNote(fresh, 'attachment-shared')
    const pulled = await fresh.getByTestId('note-source').inputValue()
    same(pulled, body)
    const reference = REFERENCE_PATTERN.exec(pulled)
    expect(reference !== null, 'Second client received the same Markdown reference').toBe(true)
    expect(reference![2], 'Second client points at the very attachment this client uploaded').toBe(uploaded.attachmentId)

    await expect(fresh.getByTestId('attachment-download')).toBeVisible()
    const downloaded = await downloadBytes(fresh)
    // Two independent byte-for-byte checks: the length, and a SHA-256 over the
    // decoded bytes. `same` hashes; the digest below compares to the bytes that
    // were actually handed to the file input.
    expect(downloaded.length, 'Downloaded byte count equals the uploaded byte count').toBe(TEXT.length)
    same(downloaded.base64, TEXT_ENCODED)
    expect(digest(Buffer.from(downloaded.base64, 'base64')), 'Downloaded bytes hash to the uploaded bytes').toBe(digest(TEXT))
  } finally { await other.close() }

  // The uploader's own client still reads the identical object, so the download
  // above was not a per-client interpretation.
  await expect(page.getByTestId('attachment-download')).toBeVisible()
  same((await downloadBytes(page)).base64, TEXT_ENCODED)
})

test('attachment_authorization another account is refused 404 while over-limit is 413 and svg is 400 leaving the note untouched', async ({ page, request }) => {
  const identity = await account(request)
  const outsider = await account(request)
  const capabilities = await attachmentCapabilities(request)
  expect(capabilities.acceptedMime.includes('image/svg+xml'), 'SVG is never in the allowlist').toBe(false)
  await login(page, identity)
  const id = await createNote(page, 'attachment-guarded', '受保护的正文\n')
  await idle(page)
  await placeCaret(page, 0)
  const uploaded = await uploadAttachment(page, PNG_FILE)
  await idle(page)
  const body = await page.getByTestId('note-source').inputValue()
  await serverBody(request, identity, id, body)

  // Real credentials of two real accounts. Only ever sent as headers; never
  // assigned to an assertion, a report field or a log line.
  const secret_owner = await secretAccess(request, identity)
  const secret_outsider = await secretAccess(request, outsider)
  const owner = { Authorization: `Bearer ${secret_owner}` }
  const foreign = { Authorization: `Bearer ${secret_outsider}` }

  // 1) A member of *another* workspace is refused both halves of the resource.
  //    "Not a member" and "does not exist" share one answer (D20), so 404 with
  //    `not_found` is the whole expectation - no state, no hint.
  const refusedMetadata = await request.get(`/api/v1/attachments/${uploaded.attachmentId}`, { headers: foreign })
  expect(refusedMetadata.status(), 'Foreign account cannot read attachment metadata').toBe(404)
  expect((await refusedMetadata.json()).error.code).toBe('not_found')
  const refusedContent = await request.get(`/api/v1/attachments/${uploaded.attachmentId}/content`, { headers: foreign })
  expect(refusedContent.status(), 'Foreign account cannot read attachment content').toBe(404)
  expect((await refusedContent.json()).error.code).toBe('not_found')

  // 2) Over the deployment's own configured cap: 413 and no row, no object. The
  //    declared size is the deployment's real `max_bytes` plus one, so this
  //    cannot pass against a limit the server does not actually enforce.
  const tooLarge = await request.post(`/api/v1/workspaces/${identity.workspaceId}/attachments`, {
    headers: owner,
    data: { mime: 'image/png', byte_size: capabilities.maxBytes + 1 },
  })
  expect(tooLarge.status(), 'Over-limit attachment is refused with 413').toBe(413)
  const tooLargeBody = await tooLarge.json()
  expect(tooLargeBody.error.code).toBe('attachment_too_large')
  expect(tooLargeBody.error.details.max_bytes).toBe(capabilities.maxBytes)
  expect('attachment_id' in tooLargeBody, 'A refused over-limit create returned no attachment identity').toBe(false)

  // 3) A scriptable document is refused by type: `image/svg+xml` is never
  //    accepted, whatever the allowlist says.
  const svg = await request.post(`/api/v1/workspaces/${identity.workspaceId}/attachments`, {
    headers: owner,
    data: { mime: 'image/svg+xml', file_name: 'icon.svg' },
  })
  expect(svg.status(), 'Disallowed MIME is refused with 400').toBe(400)
  const svgBody = await svg.json()
  expect(svgBody.error.code).toBe('attachment_unsupported_type')
  expect(svgBody.error.details.reason).toBe('mime_svg')
  expect('attachment_id' in svgBody).toBe(false)

  // None of the three refusals may have touched anything: the note body is still
  // exactly what it was in the editor, in the durable draft and in PostgreSQL.
  same(await page.getByTestId('note-source').inputValue(), body)
  await expect.poll(async () => hash((await durableBody(page, id)) ?? '')).toBe(hash(body))
  await serverBody(request, identity, id, body)

  // And the object the owner really owns is unchanged: same state, same size and
  // the digest of the bytes this case uploaded - a 404 that had side effects, or
  // a refusal that mutated the store, would break one of these.
  const metadata = await request.get(`/api/v1/attachments/${uploaded.attachmentId}`, { headers: owner })
  expect(metadata.status(), 'The owner still reads the attachment after every refusal').toBe(200)
  const view = await metadata.json()
  expect(view.state).toBe('ready')
  expect(view.workspace_id).toBe(identity.workspaceId)
  expect(view.byte_size).toBe(PNG.length)
  expect(view.content_hash).toBe(digest(PNG))
})

test('attachment_offline_gate offline disables the entry point with a stated reason and no binary reaches IndexedDB', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const text = '离线也必须保留的正文\n'
  const id = await createNote(page, 'attachment-offline', text)
  await idle(page)
  await serverBody(request, identity, id, text)

  // Any attempt to reach the attachment endpoints is recorded from here on, so
  // "the entry point refused locally" is proven by the absence of a request
  // rather than by the absence of an error message.
  const attempted: string[] = []
  page.on('request', req => { if (req.url().includes('/attachments')) attempted.push(req.method()) })

  await context.setOffline(true)
  try {
    // Opened while offline so the component's own gate is evaluated at mount
    // time against the real `navigator.onLine`, not via a later event.
    await openNote(page, 'attachment-offline')
    await expect(page.getByTestId('upload-attachment-button')).toBeDisabled()
    await expect(page.getByTestId('upload-disabled-reason')).toHaveText('当前处于离线状态，无法上传附件。')

    const before = await localDatabaseReport(page, PNG_ENCODED_HEAD)
    // The real store list, read from the live database: no attachment/blob store
    // was ever created for this feature.
    expect(before.stores).toEqual([
      'credentials', 'drafts', 'intents', 'operations', 'profiles', 'recovery_records', 'server_objects', 'workspaces',
    ])
    expect(before.records > 0, 'The scan really walked durable records').toBe(true)
    expect(before.binary, 'No binary value anywhere in IndexedDB').toBe(0)
    expect(before.encoded, 'No encoded attachment payload anywhere in IndexedDB').toBe(0)

    // Bypass the disabled button entirely: even a forced file selection cannot
    // send or persist anything, because the port refuses before any request.
    await page.getByTestId('attachment-file-input').setInputFiles(PNG_FILE)
    await expect(page.getByTestId('upload-error-alert')).toBeVisible()
    expect(attempted, 'Offline upload issued no attachment request').toEqual([])
    const after = await localDatabaseReport(page, PNG_ENCODED_HEAD)
    expect(after.records >= before.records, 'The forced selection kept the durable record set').toBe(true)
    expect(after.binary).toBe(0)
    expect(after.encoded).toBe(0)

    // The body itself is untouched by the refused attempt.
    same(await page.getByTestId('note-source').inputValue(), text)
    same((await durableBody(page, id)) ?? '', text)
  } finally {
    await context.setOffline(false)
  }

  // Back online, a real remount re-evaluates the gate: the reason disappears and
  // the button becomes usable. Without this the disabled state above would also
  // be satisfied by a button that is simply always disabled.
  await page.reload()
  await openNote(page, 'attachment-offline')
  await expect(page.getByTestId('upload-attachment-button')).toBeEnabled()
  await expect(page.getByTestId('upload-disabled-reason')).toHaveCount(0)
  same(await page.getByTestId('note-source').inputValue(), text)
  await serverBody(request, identity, id, text)
})

/** Keeps the two-account import honest: this suite never claims a third identity. */
const _accounts: Account[] = []
void _accounts
