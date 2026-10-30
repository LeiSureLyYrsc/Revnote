import {
  test, expect, account, blockSync, createNote, edit, expectWorkspaceReady, idle, localState, login,
  openNote, retry, same, serverBody, stats,
} from './fixtures'

test('manual_conflict independent browser clients preserve base local remote and explicit local resolution remains CAS protected', async ({ page, browser, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'cas-note', 'shared-base')
  await idle(page)
  const other = await browser.newContext({ baseURL: process.env.WEB_E2E_BASE_URL })
  try {
    const rival = await other.newPage()
    await login(rival, identity)
    await expectWorkspaceReady(rival)
    await openNote(rival, 'cas-note')
    const unblock = await blockSync(page.context())
    await edit(page, 'retained-local')
    await edit(rival, 'new-remote')
    await idle(rival)
    await unblock()
    await retry(page)
    await page.getByRole('button', { name: /^冲突与恢复/ }).click()
    const panel = page.getByTestId('conflict-panel')
    await expect(panel.getByRole('button', { name: '以本地内容提交', exact: true }).first()).toBeVisible()
    // Scope to the record that carries this local version rather than reading
    // ``.last()`` across whatever records happen to be rendered. A second recovery
    // record can appear while the panel is open (that is what the rest of this case
    // tests), and ``.last()`` would then read - or click - the wrong one. The
    // assertions are unchanged in what they require of base/local/remote; they are
    // now about *one* record instead of whichever happened to be last.
    const record = panel.locator('article').filter({ hasText: 'retained-local' })
    await expect(record).toHaveCount(1)
    await expect(record.getByLabel('基础版本', { exact: true })).toHaveText('shared-base')
    await expect(record.getByLabel('本地版本', { exact: true })).toHaveText('retained-local')
    await expect(record.getByLabel('远端版本', { exact: true })).toHaveText('new-remote')
    await record.getByRole('button', { name: '以本地内容提交', exact: true }).click()
    // A third version commits after the resolution UI was opened, before the
    // explicit local CAS is submitted. It must produce another preserved conflict.
    await edit(rival, 'remote-advanced-again')
    await idle(rival)
    await panel.getByRole('button', { name: '确认以本地内容提交', exact: true }).click()
    await expect.poll(async () => {
      const state = await localState(page)
      return state.recoveryRecords.some(record => !record.resolved && record.local?.bodyMd === 'retained-local' && record.remote?.body_md === 'remote-advanced-again')
    }).toBe(true)
    // The newest record is the one whose remote version advanced; identify it by that
    // text instead of by position, for the same reason as above.
    const advanced = panel.locator('article').filter({ hasText: 'remote-advanced-again' })
    await expect(advanced).toHaveCount(1)
    await advanced.getByRole('button', { name: '以本地内容提交', exact: true }).click()
    await advanced.getByRole('button', { name: '确认以本地内容提交', exact: true }).click()
    await serverBody(request, identity, id, 'retained-local')
    await idle(page)
    expect((await localState(page)).recoveryRecords.some(record => record.resolved && record.local?.bodyMd === 'retained-local')).toBe(true)
  } finally { await other.close() }
})

test('remote_delete_recovery dirty local text survives remote deletion and UI saves only a new identity', async ({ page, browser, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const id = await createNote(page, 'deleted-note', 'base-to-delete')
  await idle(page)
  const other = await browser.newContext({ baseURL: process.env.WEB_E2E_BASE_URL })
  try {
    const rival = await other.newPage()
    await login(rival, identity)
    await expectWorkspaceReady(rival)
    const unblock = await blockSync(page.context())
    await edit(page, 'recovery-must-survive')
    await rival.getByRole('button', { name: '管理笔记 deleted-note', exact: true }).click()
    await rival.getByRole('button', { name: '删除笔记', exact: true }).click()
    await rival.getByRole('button', { name: '确认删除笔记', exact: true }).click()
    await idle(rival)
    expect((await stats(request, identity, id)).noteDeleted).toBe(true)
    await unblock()
    await retry(page)
    await page.getByRole('button', { name: /^冲突与恢复/ }).click()
    const panel = page.getByTestId('conflict-panel')
    await expect(panel.getByRole('button', { name: '另存为新笔记', exact: true }).first()).toBeVisible()
    same(await panel.getByLabel('本地版本', { exact: true }).first().textContent(), 'recovery-must-survive')
    await expect(panel.getByRole('button', { name: '以本地内容提交', exact: true })).toHaveCount(0)
    await panel.getByRole('button', { name: '另存为新笔记', exact: true }).first().click()
    await panel.getByRole('button', { name: '确认另存为新笔记', exact: true }).click()
    await idle(page)
    const copy = Object.values((await localState(page)).drafts).find(draft => draft.entityId !== id && draft.bodyMd === 'recovery-must-survive')
    expect(!!copy && copy.entityId !== id, 'Recovery creates a new entity identity').toBe(true)
    await serverBody(request, identity, copy!.entityId, 'recovery-must-survive')
    expect((await stats(request, identity, id)).noteDeleted).toBe(true)
    await page.reload()
    await page.getByRole('button', { name: /^冲突与恢复/ }).click()
    expect((await localState(page)).recoveryRecords.some(record => record.local?.bodyMd === 'recovery-must-survive')).toBe(true)
  } finally { await other.close() }
})
