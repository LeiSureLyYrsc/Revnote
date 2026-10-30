import { randomUUID } from 'node:crypto'
import {
  test, expect, account, expectWorkspaceReady, localState, login, operation, push,
  restartOwnedApi, secretAccess,
} from './fixtures'

/**
 * A workspace past one page must still reach an editable state.
 *
 * The owned API is restarted with `SYNC_SNAPSHOT_PAGE_MAX_OBJECTS=2`, so the
 * browser's capability probe sees a real two-object page limit. Seven notes
 * therefore require four snapshot requests; a client that still asks for the
 * legacy single snapshot would receive a 409 (the default full cap is
 * unchanged) and never render an editable workspace.
 */
test('snapshot_pagination oversized workspace bootstraps across real pages and stays editable', async ({ page, request }) => {
  test.setTimeout(120_000)
  const identity = await account(request)
  const access = await secretAccess(request, identity)
  const notes = Array.from({ length: 7 }, (_, index) => ({
    id: randomUUID(),
    title: `paged-${index}`,
    body_md: `body ${index}`,
  }))
  for (const note of notes) {
    await push(request, identity, access, operation(note.id, 'create', null, {
      title: note.title, body_md: note.body_md, folder_id: null,
    }))
  }

  const snapshotRequests: string[] = []
  page.on('request', event => {
    if (event.url().includes('/sync/snapshot')) snapshotRequests.push(event.url())
  })

  try {
    await restartOwnedApi(request, { snapshotPageLimit: 2 })
    await login(page, identity)
    await expectWorkspaceReady(page)

    const state = await localState(page)
    const serverNotes = Object.values(state.serverObjects)
      .filter(object => object.entity_type === 'note' && !object.deleted)
    expect(serverNotes.map(object => object.id).sort()).toEqual(notes.map(note => note.id).sort())
    expect(state.bootstrapped).toBe(true)
    expect(state.cursor).toBe('7')

    // Real page requests, all bounded by the advertised capability, with at
    // least one continuation cursor. A legacy no-limit request is not present.
    expect(snapshotRequests.length).toBeGreaterThanOrEqual(4)
    for (const url of snapshotRequests) {
      expect(new URL(url).searchParams.get('limit')).toBe('2')
    }
    expect(snapshotRequests.some(url => new URL(url).searchParams.get('cursor') !== null)).toBe(true)
  } finally {
    await restartOwnedApi(request)
  }
})
