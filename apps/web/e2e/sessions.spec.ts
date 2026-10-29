import {
  test, expect, account, blockSync, createNote, edit, enterLogin, expectWorkspaceReady, idle,
  login, openNote, reauthenticate, retry, same, serverBody, stats,
} from './fixtures'

test('refresh_rotation remembered reload rotates once while same-origin follower never refreshes', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  await createNote(page, 'remembered-note', 'remembered-source')
  await idle(page)
  const before = await stats(request, identity)
  let refreshes = 0
  context.on('request', req => { if (req.url().endsWith('/auth/refresh')) refreshes += 1 })
  await page.reload()
  await openNote(page, 'remembered-note')
  await idle(page)
  const follower = await context.newPage()
  await follower.goto('/')
  await openNote(follower, 'remembered-note')
  await expect(follower.getByTestId('note-source')).toHaveAttribute('readonly', '')
  const after = await stats(request, identity)
  expect(refreshes === 1 && after.refreshUsed === before.refreshUsed + 1 && after.refreshTotal === before.refreshTotal + 1).toBe(true)
  // Only booleans cross the browser boundary; never extract credential values.
  expect(await page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>(resolve => { const req = indexedDB.open('revnote-local'); req.onsuccess = () => resolve(req.result) })
    try {
      return await new Promise<boolean>(resolve => {
        const req = db.transaction('credentials').objectStore('credentials').getAll()
        req.onsuccess = () => resolve(req.result.length === 1 && !!req.result[0].refreshToken && !req.result[0].refreshInflight)
      })
    } finally { db.close() }
  })).toBe(true)
})

test('refresh_uncertainty processed rotation with lost response fences old refresh across another reload and requires real login', async ({ page, context, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'refresh-lost', 'before refresh')
  await idle(page)
  let attempts = 0
  await context.route('**/auth/refresh', async route => {
    attempts += 1
    const before = await stats(request, identity)
    const response = await route.fetch()
    expect(response.ok(), 'Real refresh rotation completed before response loss').toBe(true)
    const after = await stats(request, identity)
    expect(after.refreshUsed === before.refreshUsed + 1).toBe(true)
    await route.abort('connectionreset')
  })
  await page.reload()
  await expect(page.getByRole('button', { name: '重新登录', exact: true })).toBeVisible()
  await openNote(page, 'refresh-lost')
  await edit(page, 'offline after refresh uncertainty')
  await page.reload()
  await openNote(page, 'refresh-lost')
  expect(attempts).toBe(1)
  same(await page.getByTestId('note-source').inputValue(), 'offline after refresh uncertainty')
  await context.unroute('**/auth/refresh')
  await reauthenticate(page, identity)
  // Refresh rotation is the fault here; the workspace snapshot itself is valid,
  // so the retained draft must still be editable after logging back in.
  await expectWorkspaceReady(page)
  await retry(page)
  await serverBody(request, identity, id, 'offline after refresh uncertainty')
})

test('session_identity_isolation another login cannot display send or adopt the old account durable queue', async ({ page, context, request }) => {
  const original = await account(request)
  const other = await account(request)
  await login(page, original, false)
  const id = await createNote(page, 'private-profile-note', 'original-server')
  await idle(page)
  const unblock = await blockSync(context)
  await edit(page, 'retained-original-draft')
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByLabel('登录邮箱', { exact: true })).toBeVisible()
  await unblock()
  const sentWorkspaces: string[] = []
  context.on('request', req => {
    if (req.url().endsWith('/sync/push')) sentWorkspaces.push(req.url().split('/workspaces/')[1].split('/')[0])
  })
  await enterLogin(page, other)
  // The absence assertion below is only meaningful once the second workspace has
  // finished loading; an unbootstrapped list is empty regardless of identity.
  await expectWorkspaceReady(page)
  await expect(page.getByRole('button', { name: '打开笔记 private-profile-note', exact: true })).toHaveCount(0)
  await createNote(page, 'other-account-note', 'other-account-source')
  await idle(page)
  // An empty list would satisfy `every` vacuously: require real traffic.
  expect(sentWorkspaces.length > 0, 'The second account actually pushed through the proxy').toBe(true)
  expect(sentWorkspaces.every(workspace => workspace === other.workspaceId)).toBe(true)
  const originalState = await page.evaluate(async userId => {
    const db = await new Promise<IDBDatabase>(resolve => { const req = indexedDB.open('revnote-local'); req.onsuccess = () => resolve(req.result) })
    try {
      return await new Promise<boolean>(resolve => {
        const req = db.transaction('workspaces').objectStore('workspaces').getAll()
        req.onsuccess = () => resolve(req.result.some(state => state.partition.userId === userId && Object.values(state.drafts).some((draft: any) => draft.bodyMd === 'retained-original-draft' && draft.dirty)))
      })
    } finally { db.close() }
  }, original.userId)
  expect(originalState).toBe(true)
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await enterLogin(page, original)
  await expectWorkspaceReady(page)
  await openNote(page, 'private-profile-note')
  await retry(page)
  await serverBody(request, original, id, 'retained-original-draft')
})
