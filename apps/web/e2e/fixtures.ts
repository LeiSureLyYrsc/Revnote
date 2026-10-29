import { createHash, randomUUID } from 'node:crypto'
import { test as base, expect, type APIRequestContext, type BrowserContext, type Page, type Route } from '@playwright/test'
import type { WorkspaceState } from '../src/domain/contracts'
import type { ServerObject, WireOperation } from '../src/api/contracts'
import { STAGE_ANNOTATION, stageName, type StageName } from '../scripts/check-test-reports'

/**
 * Bounded failure-stage evidence.
 *
 * A case can fail where Playwright reports no in-file assertion location: a
 * helper's own `expect.poll` times out, a renderer never emits its crash event,
 * a guarded `new Error` is rejected inside a fixture. The report then said
 * `assertion=unknown`, which is true but useless. These markers record which
 * helper phase the case actually reached, using a frozen vocabulary from
 * `scripts/check-test-reports.ts`, so an unroutable failure becomes a specific
 * phase instead of a guess.
 *
 * Two sinks, because a worker and the reporter live in different processes:
 * a module variable for the executing worker, and one `stage` annotation that
 * Playwright ships to the reporter in the test-end payload. Neither carries
 * text: `stageName` maps anything outside the vocabulary to `unknown`.
 */
let lastStage: StageName = 'unknown'

export function stage(name: string): void {
  lastStage = stageName(name)
  // Only the newest marker is kept: a stack of annotations would grow with
  // every helper call and annotate a passing case with stale phases.
  try {
    const annotations = test.info().annotations
    for (let index = annotations.length - 1; index >= 0; index -= 1) {
      if (annotations[index].type === STAGE_ANNOTATION) annotations.splice(index, 1)
    }
    annotations.push({ type: STAGE_ANNOTATION, description: lastStage })
  } catch {
    // Outside a running test there is no info object; the module variable above
    // is still the truthful answer for the current worker.
  }
}

/** Latest phase this worker reached (diagnostic only; never asserted on). */
export function currentStage(): StageName {
  return lastStage
}

export const test = base.extend<{ runtimeGuard: void; apiGuard: void }>({
  runtimeGuard: [async ({ context }, use) => {
    let errors = 0
    const watch = (page: Page) => page.on('pageerror', () => { errors += 1 })
    context.pages().forEach(watch)
    context.on('page', watch)
    await use()
    expect(errors === 0, 'Browser runtime error count must remain zero').toBe(true)
  }, { auto: true }],
  // A case that exits while the runner-owned API is down turns every later
  // registration into a false failure. Repair it first, then fail only the case
  // that leaked, never the cases after it. A lowered snapshot cap is repaired by
  // the case that set it (restartOwnedApi never reuses the capped process).
  apiGuard: [async ({ request }, use) => {
    await use()
    if (await apiReachable(request)) return
    try { await control(request, { action: 'start' }) } catch { /* The probe below decides. */ }
    const restarted = await apiReachable(request)
    expect(restarted, 'Case exited with the runner-owned API stopped; it was restarted so later cases cannot inherit that state').toBe(true)
  }, { auto: true }],
})
export { expect }

export interface Account { email: string; secret_password: string; workspaceId: string; userId: string }
export interface Stats {
  events: number; ledger: number; noteRev: string | null; noteDeleted: boolean | null
  bodyHash: string | null; refreshTotal: number; refreshUsed: number
}

export function hash(value: string): string { return createHash('sha256').update(value).digest('hex') }
export function same(actual: string | null | undefined, expected: string): void {
  expect(typeof actual === 'string' && hash(actual) === hash(expected), 'Exact source bytes match').toBe(true)
}

export async function account(request: APIRequestContext): Promise<Account> {
  const email = `phase4-${randomUUID()}@example.test`
  const secret_password = `secret_${randomUUID()}`
  const response = await request.post('/api/v1/auth/register', { data: { email, password: secret_password } })
  expect(response.status() === 201, 'Real API registration succeeded').toBe(true)
  const result = await response.json()
  return { email, secret_password, userId: result.user.id, workspaceId: result.workspace.id }
}

export async function login(page: Page, identity: Account, remember = true): Promise<void> {
  await page.goto('/')
  await enterLogin(page, identity, remember)
}

/**
 * Authentication only. A successful credential exchange is proven by the header
 * showing this account's workspace and the login form being gone; it is a
 * separate fact from the workspace having completed bootstrap. A case that was
 * deliberately left unbootstrapped (e.g. a real snapshot cap that can never
 * succeed) is still correctly authenticated, and the sidebar must keep creation
 * disabled there, so this helper must not assert editability.
 */
export async function enterLogin(page: Page, identity: Account, remember = true): Promise<void> {
  stage('login_view')
  await page.getByLabel('登录邮箱', { exact: true }).fill(identity.email)
  await page.getByLabel('登录密码', { exact: true }).fill(identity.secret_password)
  await page.getByLabel('记住登录', { exact: true }).setChecked(remember)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('combobox', { name: '选择工作区' })).toHaveValue(identity.workspaceId)
  await expect(page.getByLabel('登录邮箱', { exact: true })).toHaveCount(0)
  stage('login_ok')
}

export async function reauthenticate(page: Page, identity: Account): Promise<void> {
  const reopen = page.getByRole('button', { name: '重新登录', exact: true })
  if (await reopen.isVisible()) await reopen.click()
  await enterLogin(page, identity)
}

/**
 * The workspace is usable for real editing. Separate from `enterLogin` on
 * purpose: `笔记列表` disables creation until the snapshot bootstrap finishes,
 * so callers that create or edit must wait for this instead of for login.
 *
 * Only for a writable tab. A read-only follower disables creation for an
 * approved and different reason (`port.readOnly`), so never call this on the
 * follower page of a takeover case.
 */
export async function expectWorkspaceReady(page: Page): Promise<void> {
  await expect(page.getByRole('button', { name: '新建笔记', exact: true })).toBeEnabled()
}

export async function createNote(page: Page, title: string, body = ''): Promise<string> {
  // Self-sufficient: creating a note is only legal once the workspace is loaded,
  // so a caller that just authenticated never races the bootstrap.
  await expectWorkspaceReady(page)
  stage('note_create_enter')
  await page.getByRole('button', { name: '新建笔记', exact: true }).click()
  const region = page.getByRole('region', { name: '新建笔记', exact: true })
  await region.getByLabel('笔记名称', { exact: true }).fill(title)
  await region.getByRole('button', { name: '创建笔记', exact: true }).click()
  await expect(page.getByTestId('note-title')).toHaveValue(title)
  if (body) await edit(page, body)
  const state = await localState(page)
  const note = Object.values(state.drafts).find(item => item.entityType === 'note' && item.title === title)
  expect(!!note, 'UI-created note has a durable local identity').toBe(true)
  stage('note_created')
  return note!.entityId
}

/**
 * Creates a folder under the parent currently selected in the sidebar, exactly
 * like the rendered form. Note that creating a folder also *selects* it, so a
 * following create nests inside it; callers that want another root must click
 * `全部笔记` in between and assert the resulting parent.
 */
export async function createFolder(page: Page, title: string): Promise<string> {
  // `新建文件夹` is gated by the same `bootstrapped` flag as note creation.
  await expectWorkspaceReady(page)
  await page.getByRole('button', { name: '新建文件夹', exact: true }).click()
  const region = page.getByRole('region', { name: '新建文件夹', exact: true })
  await region.getByLabel('文件夹名称', { exact: true }).fill(title)
  await region.getByRole('button', { name: '创建文件夹', exact: true }).click()
  await expect(page.getByRole('button', { name: `管理文件夹 ${title}`, exact: true })).toBeVisible()
  const state = await localState(page)
  const folder = Object.values(state.drafts).find(item => item.entityType === 'folder' && item.title === title)
  expect(!!folder, 'UI-created folder has a durable local identity').toBe(true)
  return folder!.entityId
}

export async function edit(page: Page, body: string): Promise<void> {
  stage('edit_enter')
  await page.getByTestId('note-source').fill(body)
  await expect(page.getByTestId('local-save-status')).toHaveText('已写入本地')
  stage('edit_filled')
  // Footer may have still represented the previous version before React flushed.
  await expect.poll(async () => {
    const state = await localState(page)
    return Object.values(state.drafts).some(draft => hash(draft.bodyMd) === hash(body))
  }).toBe(true)
  stage('edit_durable')
}

export async function openNote(page: Page, title: string): Promise<void> {
  stage('note_open')
  await page.getByRole('button', { name: `打开笔记 ${title}`, exact: true }).click()
  await expect(page.getByTestId('note-source')).toBeVisible()
}

export async function retry(page: Page): Promise<void> {
  // `重试同步` is gated on a loaded workspace state (not on a completed
  // bootstrap), and Playwright's click already waits for it to be actionable, so
  // a case may deliberately retry a workspace that can never finish bootstrap.
  stage('retry_enter')
  await page.getByRole('button', { name: '重试同步', exact: true }).click()
  stage('retry_clicked')
}

export async function idle(page: Page): Promise<void> {
  stage('idle_enter')
  await expect.poll(async () => {
    const state = await localState(page)
    return state.bootstrapped && state.status === 'idle'
      && state.intents.every(intent => intent.state === 'done')
      && Object.values(state.drafts).every(draft => !draft.dirty)
  }, { timeout: 45_000 }).toBe(true)
  stage('idle_ok')
}

export async function localState(page: Page): Promise<WorkspaceState> {
  return page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('revnote-local')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(new Error('Local database read failed'))
    })
    try {
      return await new Promise<WorkspaceState>((resolve, reject) => {
        const request = database.transaction('workspaces').objectStore('workspaces').getAll()
        request.onsuccess = () => {
          const workspaceId = (document.querySelector('[aria-label="选择工作区"]') as HTMLSelectElement | null)?.value
          resolve(request.result.find(state => state.partition.workspaceId === workspaceId) ?? request.result[0])
        }
        request.onerror = () => reject(new Error('Local workspace read failed'))
      })
    } finally { database.close() }
  })
}

// Deterministic corrupt-cursor preparation only. All edits, saves, recovery
// actions and network operations in the scenarios still go through the real UI.
export async function setCursor(page: Page, cursor: string): Promise<void> {
  await writeWorkspace(page, { cursor })
}

export async function requireBootstrap(page: Page): Promise<void> {
  await writeWorkspace(page, { bootstrapped: false })
}

/**
 * The `workspaces` store is keyless (`createObjectStore('workspaces')` without a
 * keyPath), so `put(value)` throws DataError and aborts the transaction. The key
 * must be read from the store and passed back explicitly, exactly as the
 * application's repository does with `partitionKey`. `getAll` and `getAllKeys`
 * return rows in the same key order inside one transaction.
 */
async function writeWorkspace(page: Page, patch: { cursor?: string; bootstrapped?: boolean }): Promise<void> {
  await page.evaluate(async (change) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('revnote-local')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(new Error('Local database unavailable'))
    })
    try {
      let reason = 'unknown'
      await new Promise<void>((resolve, reject) => {
        const tx = database.transaction('workspaces', 'readwrite')
        const store = tx.objectStore('workspaces')
        const values = store.getAll()
        const keys = store.getAllKeys()
        tx.oncomplete = () => resolve()
        tx.onabort = () => reject(new Error(`Workspace write aborted (${reason}): ${tx.error?.name ?? 'unknown'}`))
        values.onsuccess = () => {
          keys.onsuccess = () => {
            // Patch the row the UI currently shows, not an arbitrary partition.
            const selected = (document.querySelector('[aria-label="选择工作区"]') as HTMLSelectElement | null)?.value
            const index = values.result.findIndex(state => state.partition.workspaceId === selected)
            const position = index === -1 ? 0 : index
            const state = values.result[position]
            const key = keys.result[position]
            // Never guess a key: an absent row must fail loudly rather than
            // write the aggregate under a fabricated partition.
            if (state === undefined || key === undefined) { reason = 'no workspace row'; tx.abort(); return }
            if (change.cursor !== undefined) state.cursor = change.cursor
            if (change.bootstrapped !== undefined) state.bootstrapped = change.bootstrapped
            store.put(state, key)
          }
        }
      })
    } finally { database.close() }
  }, patch)
}

export async function apiReachable(request: APIRequestContext): Promise<boolean> {
  // The proxy rewrites only /api/health to the liveness route; the versioned
  // prefix is never rewritten. This proves the owned API process answers.
  try {
    const response = await request.get('/api/health', { timeout: 5_000 })
    return response.ok()
  } catch { return false }
}

/** Bring the owned API back only if it is actually down; safe on every exit path. */
export async function ensureOwnedApi(request: APIRequestContext): Promise<void> {
  if (await apiReachable(request)) return
  await control(request, { action: 'start' })
  expect(await apiReachable(request), 'Owned API answers after being restarted').toBe(true)
}

/**
 * Restore a default-configured owned API. A case may leave the process stopped
 * (fault injection) or restarted with a lowered snapshot cap; both would corrupt
 * later cases, so this always ends with a fresh default process. The runner
 * rejects a start while the previous process is alive, so a reachable API is
 * stopped first; a process that died in between is not an error.
 */
export async function restartOwnedApi(request: APIRequestContext, snapshotLimit?: 1): Promise<void> {
  if (await apiReachable(request)) {
    try { await control(request, { action: 'stop' }) } catch { /* Exiting on its own is fine. */ }
  }
  await control(request, { action: 'start', ...(snapshotLimit === 1 ? { snapshotLimit: 1 } : {}) })
  expect(await apiReachable(request), 'Owned API answers after being restarted').toBe(true)
}

export async function control(request: APIRequestContext, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const response = await request.post(process.env.WEB_E2E_CONTROL_URL!, {
    headers: { 'X-Revnote-Control': process.env.WEB_E2E_CONTROL_KEY! }, data: payload,
  })
  expect(response.ok(), 'Runner-owned test control completed').toBe(true)
  return response.json()
}

export async function stats(request: APIRequestContext, identity: Account, entityId?: string, opId?: string): Promise<Stats> {
  return await control(request, { action: 'stats', workspaceId: identity.workspaceId, entityId, opId }) as unknown as Stats
}

export async function serverBody(request: APIRequestContext, identity: Account, id: string, body: string): Promise<void> {
  stage('server_body_enter')
  await expect.poll(async () => (await stats(request, identity, id)).bodyHash, { timeout: 45_000 }).toBe(hash(body))
  stage('server_body_ok')
}

export async function secretAccess(request: APIRequestContext, identity: Account): Promise<string> {
  const response = await request.post('/api/v1/auth/login', { data: { email: identity.email, password: identity.secret_password } })
  expect(response.ok(), 'Remote client real login succeeded').toBe(true)
  return (await response.json()).access_token
}

export async function snapshot(request: APIRequestContext, identity: Account, secret_access: string): Promise<ServerObject[]> {
  const response = await request.get(`/api/v1/workspaces/${identity.workspaceId}/sync/snapshot`, {
    headers: { Authorization: `Bearer ${secret_access}` },
  })
  expect(response.ok(), 'Real server snapshot succeeded').toBe(true)
  return (await response.json()).objects
}

export async function push(request: APIRequestContext, identity: Account, secret_access: string, operation: WireOperation): Promise<void> {
  const response = await request.post(`/api/v1/workspaces/${identity.workspaceId}/sync/push`, {
    headers: { Authorization: `Bearer ${secret_access}` }, data: { ops: [operation] },
  })
  expect(response.ok(), 'Remote client real push succeeded').toBe(true)
  expect((await response.json()).results[0].status === 'applied', 'Remote command committed').toBe(true)
}

export function operation(entityId: string, op: WireOperation['op'], base: string | null, payload: WireOperation['payload']): WireOperation {
  return { op_id: randomUUID(), entity_type: 'note', entity_id: entityId, op, base_rev: base, payload }
}

export function signal(): { reached: Promise<void>; release(): void } {
  let release!: () => void
  const reached = new Promise<void>(resolve => { release = resolve })
  return { reached, release }
}

export async function blockSync(context: BrowserContext): Promise<() => Promise<void>> {
  const handler = (route: Route) => route.abort('connectionrefused')
  await context.route('**/api/v1/workspaces/**/sync/**', handler)
  return () => context.unroute('**/api/v1/workspaces/**/sync/**', handler)
}

/** Wait until the page reports itself as a read-only follower, not merely rendering. */
export async function followerReady(page: Page): Promise<void> {
  stage('follower_enter')
  await expect(page.getByText('另一个标签页持有写入权限，此页只读。关闭原标签页后可接管。')).toBeVisible()
  await expect(page.getByRole('button', { name: '接管写入', exact: true })).toBeEnabled()
  stage('follower_ready')
}

/**
 * Kill the writer renderer for real, and wait for evidence that it is gone
 * rather than for one particular notification.
 *
 * `Page.crash` does tear the renderer down, but two platform details made the
 * old version of this helper assert something weaker than "the writer died":
 * the notification is a protocol event that has to reach the client, and the
 * command itself can be refused. Linux CI showed the first half of that: the
 * command was sent, the process tree was healthy, and no Playwright `crash`
 * event arrived within 20s. Waiting for that single event therefore tested a
 * transport detail rather than the property this scenario needs ("the writer
 * can no longer run code, so its Web Lock is released").
 *
 * Note that a rejected `page.evaluate` is *not* an independent witness either:
 * Playwright only marks a session crashed from that same `Inspector.targetCrashed`
 * event, so when the event is lost the page's calls hang instead of failing.
 * Liveness is therefore judged by "cannot complete a trivial evaluation", and a
 * page that still answers is escalated to a real renderer abort:
 *   1. CDP `Page.crash` -- a real renderer kill, the strongest form;
 *   2. if the page still answers, `chrome://crash` -- the browser aborts that
 *      renderer instead of closing it gracefully;
 *   3. the bounded `page.close({ runBeforeUnload: false })` afterwards destroys
 *      the target through the browser process: no unload handler, no flush, so
 *      the in-flight command still cannot be acknowledged.
 *
 * Confirmation is first-come: the page's `crash` event, `Inspector.targetCrashed`
 * on the target's own session, or two consecutive bounded probes that never
 * report `alive`. A live renderer answers in milliseconds, so two consecutive
 * non-answers cannot come from a healthy page.
 *
 * Crucially this helper is not the last word on death: `expectWriterLockFree`
 * still proves, from a separate live page, that the exclusive lock is actually
 * free before the follower takes over. A renderer that survived all three steps
 * would keep its lock and fail there -- the guarantee is observed, not assumed.
 */
export async function crashPage(context: BrowserContext, page: Page): Promise<void> {
  stage('crash_enter')
  const cdp = await context.newCDPSession(page)
  let signalled = false
  const mark = (): void => { signalled = true }
  page.on('crash', mark)
  cdp.on('Inspector.targetCrashed', mark)

  /**
   * One bounded liveness probe.
   * `alive`: the renderer ran our script. `dead`: the call was rejected, which
   * is how a crashed target answers. `silent`: no answer inside the bound -- a
   * renderer that cannot run script and therefore cannot be executing app code.
   */
  const probe = async (): Promise<'alive' | 'dead' | 'silent'> => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const bound = new Promise<'silent'>((resolve) => { timer = setTimeout(() => resolve('silent'), 1_500) })
    // Both outcomes are consumed, so a probe that loses the race cannot surface
    // later as an unhandled rejection.
    const answer = page.evaluate(() => 0).then(() => 'alive' as const, () => 'dead' as const)
    try { return await Promise.race([answer, bound]) } finally { clearTimeout(timer) }
  }

  /** Two bounded non-answers in a row; one alone could be a teardown artefact. */
  const unresponsive = async (): Promise<boolean> => {
    if (await probe() === 'alive') return false
    return await probe() !== 'alive'
  }

  void cdp.send('Page.crash').catch(() => undefined)
  stage('crash_sent')

  const started = Date.now()
  const deadline = started + 20_000
  const expired = (): boolean => Date.now() >= deadline
  let escalated = false
  let confirmed = false
  /** Abort this renderer without needing the page's cooperation; once only. */
  const escalate = (): void => {
    escalated = true
    void page.goto('chrome://crash').catch(() => undefined)
  }
  try {
    while (!signalled && !expired()) {
      if (await unresponsive()) {
        // Not executing app code. Throw the strongest available kill at it once
        // so a wedged renderer is really aborted, then accept the death.
        if (!escalated) { escalate(); await new Promise((resolve) => setTimeout(resolve, 250)); continue }
        confirmed = true
        break
      }
      // Still executing: `Page.crash` was refused or did not take effect. Give it
      // a short grace period, then force the renderer to abort.
      if (!escalated && Date.now() - started > 3_000) escalate()
      await new Promise((resolve) => setTimeout(resolve, 200))
    }
    if (signalled) confirmed = true
  } finally {
    // Never leave listeners behind: the runtime guard keeps observing this page
    // object for the rest of the case.
    page.off('crash', mark)
    try { cdp.off('Inspector.targetCrashed', mark) } catch { /* Session already disposed. */ }
  }
  expect(confirmed, 'Writer renderer did not report a crash').toBe(true)
  stage('crash_confirmed')
  await Promise.race([
    page.close({ runBeforeUnload: false }),
    new Promise<void>((_, reject) => setTimeout(() => reject(new Error('Crashed writer page did not close')), 20_000)),
  ]).catch(() => undefined)
  stage('crash_closed')
}

/** The exclusive origin-wide writer lock must be free before a takeover is real. */
export async function expectWriterLockFree(page: Page): Promise<void> {
  stage('lock_free_enter')
  try {
    await expect.poll(async () => page.evaluate(async () => {
      const snapshot = await navigator.locks.query()
      return (snapshot.held ?? []).filter(lock => lock.name === 'revnote-writer-v1').length
    }), { timeout: 20_000 }).toBe(0)
    stage('lock_free_ok')
  } catch (error) {
    // Record the phase before rethrowing the very same failure: the assertion
    // text and the spec's control flow stay untouched.
    stage('lock_free_timeout')
    throw error
  }
}
