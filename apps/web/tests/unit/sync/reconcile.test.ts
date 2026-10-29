import { describe, expect, it } from 'vitest'
import { emptyWorkspace } from '../../../src/domain/contracts'
import { objectDraft, preserve, saveLocal } from '../../../src/sync/operations'
import { activateSnapshot, applyPull, reconcileObject } from '../../../src/sync/reconcile'
import { MemoryRepository, lease, note, page, partition } from './fixtures'

describe('atomic pull and snapshot reconciliation', () => {
  it('consumes continuous pages without using ahead objects or last_seq as cursor', () => {
    const state = emptyWorkspace(partition)
    state.cursor = '9007199254740992'
    const ahead = note({ rev: '9007199254740999', updated_seq: '9007199254740999' })
    const response = page([ahead], '9007199254740993')
    response.events[0].seq = '9007199254740993'
    response.events[0].rev = '2'
    response.last_seq = '9007199254740999'
    response.has_more = true
    expect(applyPull(state, response, state.cursor, state.generation)).toBe(true)
    expect(state.cursor).toBe('9007199254740993')
    expect(state.serverObjects['note:note'].rev).toBe('9007199254740999')
    reconcileObject(state, note({ rev: '7', updated_seq: '9007199254740994' }))
    expect(state.serverObjects['note:note'].rev).toBe('9007199254740999')
  })

  it('ignores stale cursor/generation responses and refuses broken event cursors', () => {
    const state = emptyWorkspace(partition)
    expect(applyPull(state, page([note()]), '8', '0')).toBe(false)
    expect(applyPull(state, page([note()]), '0', 'old')).toBe(false)
    expect(() => applyPull(state, page([note()], '9'), '0', '0')).toThrow('cursor_invalid')
    expect(state.cursor).toBe('0')
  })

  it('rolls back objects recovery and cursor together if the page transaction fails', async () => {
    const repo = new MemoryRepository()
    repo.state.drafts['note:note'] = objectDraft(note())
    saveLocal(repo.state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    repo.failNext = true
    const response = page([note({ rev: '2', updated_seq: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' })], '2')
    repo.state.cursor = '1'
    await expect(repo.mutate(partition, lease, state => applyPull(state, response, '1', '0'))).rejects.toThrow('aborted')
    expect(repo.state.cursor).toBe('1')
    expect(repo.state.serverObjects).toEqual({})
    expect(repo.state.recoveryRecords).toHaveLength(0)
    await repo.mutate(partition, lease, state => applyPull(state, response, '1', '0'))
    expect(repo.state.cursor).toBe('2')
    expect(repo.state.recoveryRecords[0].local?.bodyMd).toBe('local')
  })

  it('stages a complete image and atomically activates without clearing local data', async () => {
    const repo = new MemoryRepository()
    repo.state.drafts['note:note'] = objectDraft(note())
    saveLocal(repo.state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    const before = structuredClone(repo.state)
    repo.failNext = true
    await expect(repo.mutate(partition, lease, state => activateSnapshot(state, { objects: [], as_of_seq: '8', min_retained_seq: '0' }, '0'))).rejects.toThrow()
    expect(repo.state).toEqual(before)
    await repo.mutate(partition, lease, state => activateSnapshot(state, { objects: [], as_of_seq: '8', min_retained_seq: '0' }, '0'))
    expect(repo.state).toMatchObject({ cursor: '8', generation: '1', bootstrapped: true })
    expect(repo.state.drafts['note:note']).toMatchObject({ bodyMd: 'local', blocked: 'remote_deleted' })
    expect(repo.state.intents).toHaveLength(1)
    expect(repo.state.recoveryRecords).toHaveLength(1)
  })

  it('does not rebase dirty drafts when a remote object advances', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:note'] = objectDraft(note())
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    reconcileObject(state, note({ rev: '7', updated_seq: '7' }))
    expect(state.drafts['note:note']).toMatchObject({ bodyMd: 'local', base: { rev: '1' }, blocked: 'remote_changed' })
    expect(state.recoveryRecords[0].remote?.rev).toBe('7')
  })

  it('records the latest authoritative version as remote for a remote update', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:note'] = objectDraft(note())
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    reconcileObject(state, note({ rev: '4', updated_seq: '4', body_md: 'remote four' }))
    reconcileObject(state, note({ rev: '7', updated_seq: '7', body_md: 'remote seven' }))
    expect(state.recoveryRecords).toHaveLength(1)
    expect(state.recoveryRecords[0]).toMatchObject({ reason: 'remote_changed', remote: { rev: '7', body_md: 'remote seven' } })
    // The base stays the acknowledged revision; only remote may advance.
    expect(state.recoveryRecords[0].base?.rev).toBe('1')
    expect(state.recoveryRecords[0].local?.bodyMd).toBe('local')
    expect(state.drafts['note:note']).toMatchObject({ bodyMd: 'local', base: { rev: '1' }, blocked: 'remote_changed' })
  })

  it('replaces a push-conflict record with the authoritative tombstone for the same local version', () => {
    const state = emptyWorkspace(partition)
    state.serverObjects['note:note'] = note()
    state.drafts['note:note'] = objectDraft(note())
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    preserve(state, state.drafts['note:note'], 'conflict', state.serverObjects['note:note'])
    expect(state.recoveryRecords).toHaveLength(1)
    expect(state.recoveryRecords[0].remote?.rev).toBe('1')
    reconcileObject(state, note({ rev: '2', updated_seq: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' }))
    expect(state.recoveryRecords).toHaveLength(1)
    expect(state.recoveryRecords[0]).toMatchObject({ reason: 'remote_deleted', remote: { rev: '2', deleted: true } })
    expect(state.recoveryRecords[0].local?.bodyMd).toBe('local')
    expect(state.drafts['note:note']).toMatchObject({ bodyMd: 'local', blocked: 'remote_deleted' })
  })

  it('keeps a real local edit apart from a later version and never leaves two contradictory records', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:note'] = objectDraft(note())
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'one', bodyMd: 'one', parentId: null })
    reconcileObject(state, note({ rev: '2', updated_seq: '2', body_md: 'remote two' }))
    expect(state.recoveryRecords).toHaveLength(1)
    // A genuinely new edit is a new local version, so it earns its own record.
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'two', bodyMd: 'two', parentId: null })
    reconcileObject(state, note({ rev: '3', updated_seq: '3', body_md: 'remote three' }))
    expect(state.recoveryRecords).toHaveLength(2)
    expect(state.recoveryRecords.map(record => record.local?.bodyMd)).toEqual(['one', 'two'])
    expect(state.recoveryRecords.every(record => !record.resolved)).toBe(true)
    expect(state.recoveryRecords[1].remote).toMatchObject({ rev: '3', body_md: 'remote three' })
  })

  it('never rewrites the record an explicit resolution was taken from', () => {
    const state = emptyWorkspace(partition)
    state.serverObjects['note:note'] = note()
    state.drafts['note:note'] = objectDraft(note())
    saveLocal(state, { entityType: 'note', entityId: 'note', title: 'local', bodyMd: 'local', parentId: null })
    reconcileObject(state, note({ rev: '2', updated_seq: '2', body_md: 'remote two' }))
    const conflict = state.recoveryRecords[0]
    preserve(state, state.drafts['note:note'], 'manual_copy', note({ rev: '2', updated_seq: '2', body_md: 'remote two' }))
    expect(state.recoveryRecords).toHaveLength(2)
    expect(state.recoveryRecords[0]).toBe(conflict)
    expect(conflict).toMatchObject({ reason: 'remote_changed', resolved: false })
    expect(state.recoveryRecords[1]).toMatchObject({ reason: 'manual_copy', resolved: false })
  })
})
