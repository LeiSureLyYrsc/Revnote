import { randomUUID } from 'node:crypto'
import {
  test, expect, account, createFolder, createNote, edit, expectWorkspaceReady, hash, idle, localState,
  login, openNote, operation, push, same, secretAccess, serverBody, snapshot, stats,
} from './fixtures'

test('workspace_flow proxy auth snapshot and all eight operations commit to PostgreSQL', async ({ page, request }) => {
  const identity = await account(request)
  const observed = new Set<string>()
  page.on('request', req => {
    if (req.url().endsWith('/sync/push')) {
      const data = req.postDataJSON()
      for (const op of data.ops) observed.add(`${op.entity_type}:${op.op}`)
    }
  })
  await login(page, identity)
  // The eight-operation flow starts by creating a folder, so the workspace must
  // be past bootstrap before the first mutation; assert that explicitly instead
  // of relying on login to imply it.
  await expectWorkspaceReady(page)
  const secret_access = await secretAccess(request, identity)
  // Creating a folder also selects it, so every additional root object needs an
  // explicit return to `全部笔记` first. Each move below then changes the parent
  // for real: an unchanged parent is a legal no-op that emits no operation, so a
  // move that does not actually relocate proves nothing.
  const parent = await createFolder(page, 'parent')
  await idle(page)
  await page.getByRole('button', { name: '全部笔记', exact: true }).click()
  const child = await createFolder(page, 'child')
  await idle(page)
  expect((await localState(page)).drafts[`folder:${child}`].parentId, 'Second root folder is not nested under the first').toBe(null)
  await page.getByRole('button', { name: '管理文件夹 child', exact: true }).click()
  let region = page.getByRole('region', { name: '管理文件夹', exact: true })
  await region.getByLabel('文件夹名称', { exact: true }).fill('renamed')
  await region.getByRole('button', { name: '保存文件夹名称', exact: true }).click()
  await idle(page)
  await page.getByRole('button', { name: '管理文件夹 renamed', exact: true }).click()
  region = page.getByRole('region', { name: '管理文件夹', exact: true })
  await region.getByLabel('目标文件夹').selectOption(parent)
  await region.getByRole('button', { name: '移动文件夹', exact: true }).click()
  await idle(page)
  expect((await localState(page)).drafts[`folder:${child}`].parentId, 'Folder move really relocated the folder').toBe(parent)
  await page.getByRole('button', { name: '全部笔记', exact: true }).click()
  const note = await createNote(page, 'eight-ops', 'source-one')
  await idle(page)
  await edit(page, 'source-two')
  await idle(page)
  await page.getByRole('button', { name: '管理笔记 eight-ops', exact: true }).click()
  region = page.getByRole('region', { name: '管理笔记', exact: true })
  await region.getByLabel('目标文件夹').selectOption(parent)
  await region.getByRole('button', { name: '移动笔记', exact: true }).click()
  await idle(page)
  expect((await localState(page)).drafts[`note:${note}`].parentId, 'Note move really relocated the note').toBe(parent)
  await page.getByRole('button', { name: '管理笔记 eight-ops', exact: true }).click()
  await page.getByRole('button', { name: '删除笔记', exact: true }).click()
  await page.getByRole('button', { name: '确认删除笔记', exact: true }).click()
  await idle(page)
  await page.getByRole('button', { name: '管理文件夹 renamed', exact: true }).click()
  await page.getByRole('button', { name: '删除文件夹', exact: true }).click()
  await page.getByRole('button', { name: '确认删除文件夹', exact: true }).click()
  await idle(page)
  expect([...observed].sort()).toEqual([
    'folder:create', 'folder:delete', 'folder:move', 'folder:update',
    'note:create', 'note:delete', 'note:move', 'note:update',
  ])
  const database = await stats(request, identity, note)
  expect(database.events > 0 && database.events === database.ledger).toBe(true)
  expect(database.noteDeleted).toBe(true)
  expect((await snapshot(request, identity, secret_access)).some(object => object.id === note)).toBe(false)
})

test('draft_reload local acknowledgement survives reload and fresh context reads server bytes', async ({ page, browser, request }) => {
  const identity = await account(request)
  await login(page, identity)
  const body = '# Persisted\n\nlocal and server agree\n'
  const id = await createNote(page, 'reload-note', body)
  await idle(page)
  await page.reload()
  await openNote(page, 'reload-note')
  same(await page.getByTestId('note-source').inputValue(), body)
  await page.close()
  const other = await browser.newContext({ baseURL: process.env.WEB_E2E_BASE_URL })
  try {
    const fresh = await other.newPage()
    await login(fresh, identity)
    // The note list is only populated after the real snapshot bootstrap; wait for
    // that explicitly rather than letting `openNote` fail as a missing button.
    await expectWorkspaceReady(fresh)
    await openNote(fresh, 'reload-note')
    same(await fresh.getByTestId('note-source').inputValue(), body)
    await serverBody(request, identity, id, body)
  } finally { await other.close() }
})

test('markdown_preservation source truth keeps extended syntax CRLF and trailing bytes through view and title changes', async ({ page, request }) => {
  const identity = await account(request)
  const secret_access = await secretAccess(request, identity)
  const id = randomUUID()
  const body = '# heading  \r\n\r\n<div data-kind="raw">HTML</div>  \r\n\r\n$$x^2 + \\alpha$$\r\n\r\n:::unknown block\r\n  body\t \r\n:::\r\n\r\n- [x] task\r\n\r\n[ref]: <https://example.test/a> "title"\r\n'
  await push(request, identity, secret_access, operation(id, 'create', null, { title: 'byte-safe', body_md: body, folder_id: null }))
  await login(page, identity)
  // The note was created over the API from a different client, so it only exists
  // here after a real snapshot bootstrap completes.
  await expectWorkspaceReady(page)
  await openNote(page, 'byte-safe')
  // HTML textarea normalizes displayed line endings; the durable source does not.
  same((await localState(page)).drafts[`note:${id}`].bodyMd, body)
  await page.getByRole('button', { name: '查看原文', exact: true }).click()
  same(await page.getByLabel('Markdown 原文预览').textContent(), body)
  await page.getByRole('button', { name: '查看原文', exact: true }).click()
  await page.getByRole('button', { name: 'Markdown 源码', exact: true }).click()
  await page.getByTestId('note-title').fill('byte-safe-renamed')
  await idle(page)
  expect((await stats(request, identity, id)).bodyHash === hash(body)).toBe(true)
  await page.reload()
  await openNote(page, 'byte-safe-renamed')
  same((await localState(page)).drafts[`note:${id}`].bodyMd, body)
  await page.getByRole('button', { name: '转换为富文本', exact: true }).click()
  await page.getByRole('button', { name: '取消转换', exact: true }).click()
  await serverBody(request, identity, id, body)
})

test('mobile_layout 390px keyboard panels read-only restrictions and runtime guard', async ({ page, context, request }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const identity = await account(request)
  await login(page, identity)
  await createNote(page, 'narrow-note', 'mobile source')
  await idle(page)
  await page.getByRole('button', { name: '笔记列表', exact: true }).click()
  await expect(page.getByRole('button', { name: '打开笔记 narrow-note', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '打开笔记 narrow-note', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('note-source')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  const follower = await context.newPage()
  await follower.setViewportSize({ width: 390, height: 844 })
  await follower.goto('/')
  await expect(follower.getByText('另一个标签页持有写入权限，此页只读。关闭原标签页后可接管。')).toBeVisible()
  await expect(follower.getByRole('button', { name: '新建笔记', exact: true })).toBeDisabled()
  await openNote(follower, 'narrow-note')
  await expect(follower.getByTestId('note-source')).toHaveAttribute('readonly', '')
  await expect(follower.getByRole('button', { name: '退出登录', exact: true })).toBeDisabled()
})
