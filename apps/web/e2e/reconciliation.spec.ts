import { randomUUID } from 'node:crypto'
import type { Route } from '@playwright/test'
import {
  test, expect, account, apiReachable, control, createNote, edit, expectWorkspaceReady, idle,
  localState, login, openNote, operation, push, reauthenticate, requireBootstrap, restartOwnedApi,
  retry, secretAccess, serverBody, setCursor, signal, snapshot,
} from './fixtures'

test('resync_preservation real snapshot cap retains dirty draft and original queue then real 410 recovery and cursor-ahead retention without erasure', async ({ page, context, request }) => {
  test.setTimeout(180_000)
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'resync-local', 'base')
  await idle(page)
  const secret_access = await secretAccess(request, identity)
  await push(request, identity, secret_access, operation(randomUUID(), 'create', null, { title: 'second-object', body_md: '', folder_id: null }))
  // The owned API is stopped below, so every exit path — including a throw
  // between the stop and the guarded block — must restore it. Otherwise every
  // later case would register against a dead port and fail for the wrong reason.
  try {
    await control(request, { action: 'stop' })
    await page.reload()
    await expect(page.getByRole('button', { name: '重新登录', exact: true })).toBeVisible()
    await openNote(page, 'resync-local')
    await edit(page, 'dirty-before-snapshot-cap')
    const before = await localState(page)
    await requireBootstrap(page)
    await control(request, { action: 'start', snapshotLimit: 1 })
    await reauthenticate(page, identity)
    await retry(page)
    await expect.poll(async () => (await localState(page)).status, { timeout: 45_000 }).toBe('paused')
    const blocked = await localState(page)
    expect(blocked.drafts[`note:${id}`].bodyMd === 'dirty-before-snapshot-cap').toBe(true)
    expect(blocked.intents.length >= before.intents.length && blocked.operations.length >= before.operations.length).toBe(true)
    expect(blocked.error?.includes('snapshot') || blocked.error?.includes('large')).toBe(true)
    await expect(page.getByTestId('workspace-sync-status')).toContainText('超过当前可处理范围')
  } finally {
    // A lowered snapshot cap must not leak either: restart stops whatever is
    // running (or is already stopped) and starts one default-configured process.
    await restartOwnedApi(request)
    expect(await apiReachable(request), 'Owned API is reachable after the finally block').toBe(true)
  }
  await retry(page)
  await idle(page)
  // Recovery, not merely authentication: the same page that could not bootstrap
  // under the cap must become editable again once the default API is back.
  await expectWorkspaceReady(page)
  await serverBody(request, identity, id, 'dirty-before-snapshot-cap')

  // Reauthentication rotates the ownership generation, which intentionally
  // remounts the workspace and resets the editor selection. Reopen the note
  // before editing again instead of assuming a selection survived.
  await openNote(page, 'resync-local')

  // The test DB retention floor is advanced only by runner IPC. Pull itself is
  // a real request; edit while that request is held so rebuild must preserve it.
  const extra = randomUUID()
  await push(request, identity, secret_access, operation(extra, 'create', null, { title: 'retention-anchor', body_md: '', folder_id: null }))
  await control(request, { action: 'floor', workspaceId: identity.workspaceId })
  await setCursor(page, '0')
  const pullReached = signal()
  const releasePull = signal()
  let seen410 = false
  let gated = false
  const holdPull = async (route: Route) => {
    if (gated) { await route.continue(); return }
    gated = true
    pullReached.release()
    await releasePull.reached
    const response = await route.fetch()
    seen410 = response.status() === 410
    await route.fulfill({ response })
  }
  await context.route('**/sync/pull?**', holdPull)
  await retry(page)
  await pullReached.reached
  await edit(page, 'draft-during-real-410')
  releasePull.release()
  await expect.poll(() => seen410).toBe(true)
  await serverBody(request, identity, id, 'draft-during-real-410')
  await idle(page)
  await context.unroute('**/sync/pull?**', holdPull)
  // A durable cursor ahead of the server is a separate real API error. The
  // corrupt cursor must not become an empty workspace or erase cached content.
  const retained = (await localState(page)).drafts[`note:${id}`].bodyMd
  await setCursor(page, '9223372036854775807')
  let ahead = false
  const inspectAhead = async (route: Route) => {
    const response = await route.fetch()
    if (!response.ok()) ahead = (await response.json()).error?.code === 'cursor_ahead'
    await route.fulfill({ response })
  }
  await context.route('**/sync/pull?**', inspectAhead)
  await retry(page)
  await expect.poll(() => ahead).toBe(true)
  expect((await localState(page)).drafts[`note:${id}`].bodyMd === retained).toBe(true)
  expect(Object.keys((await localState(page)).drafts).length >= 2).toBe(true)
})

test('cursor_integrity real leading objects paged events and aborted IndexedDB page transaction never skip or regress', async ({ page, context, request }) => {
  test.setTimeout(120_000)
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'cursor-note', 'v1')
  await idle(page)
  const initial = await localState(page)
  const secret_access = await secretAccess(request, identity)
  const current = (await snapshot(request, identity, secret_access)).find(object => object.id === id)!
  await push(request, identity, secret_access, operation(id, 'update', current.rev, { title: 'cursor-note', body_md: 'v2' }))
  await push(request, identity, secret_access, operation(id, 'update', String(BigInt(current.rev) + 1n), { title: 'cursor-note', body_md: 'v3-leading' }))
  // Fault only the browser transaction commit. No production hooks and no fake
  // persistence implementation: abort the real IDB tx after it stages the page.
  await page.evaluate(cursor => {
    const original = IDBObjectStore.prototype.put
    let armed = true
    IDBObjectStore.prototype.put = function (value: any, key?: IDBValidKey) {
      const result = key === undefined ? original.call(this, value) : original.call(this, value, key)
      if (armed && this.name === 'workspaces' && value.cursor !== cursor && value.serverObjects) {
        armed = false
        this.transaction.abort()
        ;(window as any).__phase4Aborted = true
      }
      return result
    }
  }, initial.cursor)
  const cursors: string[] = []
  let leading = false
  const pageGate = signal()
  const release = signal()
  let first = true
  const pages = async (route: Route) => {
    const url = new URL(route.request().url())
    cursors.push(url.searchParams.get('since')!)
    url.searchParams.set('limit', '1')
    const response = await route.fetch({ url: url.toString() })
    const body = await response.json()
    if (body.events?.length && body.objects?.length) {
      leading ||= BigInt(body.objects[0].updated_seq) > BigInt(body.events[0].seq)
    }
    if (!first) { pageGate.release(); await release.reached }
    first = false
    await route.fulfill({ response })
  }
  await context.route('**/sync/pull?**', pages)
  await retry(page)
  await expect.poll(() => page.evaluate(() => (window as any).__phase4Aborted === true)).toBe(true)
  expect((await localState(page)).cursor).toBe(initial.cursor)
  await retry(page)
  await pageGate.reached
  expect(cursors[0] === initial.cursor && cursors[1] === initial.cursor).toBe(true)
  release.release()
  await idle(page)
  const final = await localState(page)
  expect(leading, 'Real API object leads the one-event page').toBe(true)
  expect(final.cursor === String(BigInt(initial.cursor) + 2n)).toBe(true)
  expect(final.serverObjects[`note:${id}`].body_md === 'v3-leading').toBe(true)
  expect(final.serverObjects[`note:${id}`].rev === String(BigInt(current.rev) + 2n)).toBe(true)
  await serverBody(request, identity, id, 'v3-leading')
})
