import type { WireOperation } from '../src/api/contracts'
import {
  test, expect, account, control, crashPage, createNote, edit, ensureOwnedApi, enterLogin,
  expectTakeoverComplete, expectWorkspaceReady, expectWriterLockFree, followerReady, idle, localState,
  login, openNote, retry, same, serverBody, signal, stats,
} from './fixtures'

test('api_restart_recovery actual API process stops while Vite serves reload and durable draft', async ({ page, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'api-restart', 'before stop')
  await idle(page)
  try {
    // Inside the try: even a failed stop attempt must be repaired before the
    // case exits, or the next case would register against a dead port.
    await control(request, { action: 'stop' })
    await edit(page, 'saved while API process absent')
    await page.reload()
    await openNote(page, 'api-restart')
    same(await page.getByTestId('note-source').inputValue(), 'saved while API process absent')
    expect((await localState(page)).intents.some(intent => intent.state !== 'done')).toBe(true)
  } finally {
    // Unconditional: a throw above must never leave the owned API stopped for
    // the rest of the suite.
    await ensureOwnedApi(request)
  }
  await page.getByRole('button', { name: '重新登录', exact: true }).click()
  await enterLogin(page, identity)
  // This workspace models a plain API outage, not a capped snapshot, so the
  // cached bootstrap is still valid and editing must become possible again.
  await expectWorkspaceReady(page)
  await retry(page)
  await serverBody(request, identity, id, 'saved while API process absent')
})

test('offline_recovery loaded page edits fully offline and synchronizes after network resumes', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'network-offline', 'before offline')
  await idle(page)
  await context.setOffline(true)
  try { await edit(page, 'local offline draft') }
  finally { await context.setOffline(false) }
  await retry(page)
  await serverBody(request, identity, id, 'local offline draft')
})

test('lost_response_replay committed response is discarded then reload resends identical command exactly once in PG', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'lost-response', 'base')
  await idle(page)
  let frozen: WireOperation | undefined
  let dropped = false
  let replayed = false
  const committed = signal()
  await context.route('**/sync/push', async route => {
    const op: WireOperation = route.request().postDataJSON().ops[0]
    if (!dropped && op.entity_id === id && op.op === 'update') {
      frozen = op
      const response = await route.fetch()
      expect(response.ok(), 'Real push replied after its transaction committed').toBe(true)
      const counts = await stats(request, identity, id, op.op_id)
      expect(counts.events === 1 && counts.ledger === 1, 'Separate PG connection sees committed write before response loss').toBe(true)
      dropped = true
      await route.abort('connectionreset')
      committed.release()
    } else if (frozen && op.op_id === frozen.op_id) {
      expect(JSON.stringify(op) === JSON.stringify(frozen), 'Retry uses the exact immutable original command').toBe(true)
      const response = await route.fetch()
      replayed = (await response.json()).results[0].status === 'replayed'
      await route.fulfill({ response })
    } else await route.continue()
  })
  await edit(page, 'committed without reply')
  await committed.reached
  await page.reload()
  await openNote(page, 'lost-response')
  await idle(page)
  expect(replayed, 'Reload confirms original command through real replay response').toBe(true)
  const counts = await stats(request, identity, id, frozen!.op_id)
  expect(counts.events === 1 && counts.ledger === 1).toBe(true)
  await serverBody(request, identity, id, 'committed without reply')
})

test('ack_preserves_later_edit acknowledgement of A cannot clean or replace durable newer edit B', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'later-edit', 'base')
  await idle(page)
  const committedA = signal()
  const releaseA = signal()
  let held = false
  let blockB = true
  await context.route('**/sync/push', async route => {
    const op = route.request().postDataJSON().ops[0]
    if (!held && op.entity_id === id && op.payload.body_md === 'edit-A') {
      held = true
      const response = await route.fetch()
      expect((await stats(request, identity, id, op.op_id)).events).toBe(1)
      committedA.release()
      await releaseA.reached
      await route.fulfill({ response })
    } else if (held && blockB) await route.abort('connectionrefused')
    else await route.continue()
  })
  await edit(page, 'edit-A')
  await committedA.reached
  await edit(page, 'edit-B')
  releaseA.release()
  await expect.poll(async () => {
    const state = await localState(page)
    const draft = state.drafts[`note:${id}`]
    return draft.bodyMd === 'edit-B' && draft.dirty && state.operations.some(op => op.wire.payload.body_md === 'edit-A' && op.state === 'acknowledged')
  }).toBe(true)
  blockB = false
  await retry(page)
  await serverBody(request, identity, id, 'edit-B')
  same(await page.getByTestId('note-source').inputValue(), 'edit-B')
})

test('single_writer_takeover crashed writer retains uncertain command and follower takes over without duplicate event', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'takeover-note', 'base')
  await idle(page)
  // The follower is a real same-origin tab in the same context: it must share
  // the origin-wide Web Lock and IndexedDB, which a separate BrowserContext
  // would not. Its lifecycle is therefore independent of the writer page object.
  const follower = await context.newPage()
  await follower.goto('/')
  await followerReady(follower)
  await openNote(follower, 'takeover-note')
  await expect(follower.getByTestId('note-source')).toHaveAttribute('readonly', '')
  const committed = signal()
  const release = signal()
  let frozen: WireOperation | undefined
  let oldPage = true
  await context.route('**/sync/push', async route => {
    const op = route.request().postDataJSON().ops[0]
    if (oldPage && op.entity_id === id && op.op === 'update') {
      frozen = op
      const response = await route.fetch()
      expect((await stats(request, identity, id, op.op_id)).ledger).toBe(1)
      committed.release()
      await release.reached
      try { await route.fulfill({ response }) } catch { /* Closed writer cannot receive a late callback. */ }
    } else {
      if (frozen && op.op_id === frozen.op_id) expect(JSON.stringify(op) === JSON.stringify(frozen), 'Takeover never invents a replacement command').toBe(true)
      await route.continue()
    }
  })
  await edit(page, 'writer committed before crash')
  await committed.reached
  // Crash the writer renderer (not a graceful close) so its in-flight command is
  // left durable and uncertain rather than cleanly acknowledged.
  await crashPage(context, page)
  oldPage = false
  release.release()
  // Takeover is only valid once the crashed renderer's exclusive lock is gone.
  await expectWriterLockFree(follower)
  // The follower already rendered the read-only state above; the button is its
  // only writer transition, so a click here must succeed on a live page.
  await follower.getByRole('button', { name: '接管写入', exact: true }).click()
  await expect(follower.getByText('另一个标签页持有写入权限，此页只读。关闭原标签页后可接管。')).toHaveCount(0)
  // The banner leaving only proves the lock was taken; until the successor
  // runtime is committed App renders an explicit restoring state instead of the
  // superseded tree, so the workspace columns (and their note buttons) are simply
  // not mounted in that window. Both waits below are readiness, not delays.
  await expectTakeoverComplete(follower)
  await openNote(follower, 'takeover-note')
  await expect(follower.getByTestId('note-source')).not.toHaveAttribute('readonly', '')
  await retry(follower)
  await idle(follower)
  const counts = await stats(request, identity, id, frozen!.op_id)
  expect(counts.events === 1 && counts.ledger === 1).toBe(true)
  await edit(follower, 'successor owns writes')
  await serverBody(request, identity, id, 'successor owns writes')
  // The crashed writer's uncertain command settled as one row; the successor's
  // edit is a separate command, and every op still maps to exactly one event.
  const after = await stats(request, identity, id)
  expect(after.events === after.ledger).toBe(true)
  expect(after.noteRev).not.toBe(null)
})
