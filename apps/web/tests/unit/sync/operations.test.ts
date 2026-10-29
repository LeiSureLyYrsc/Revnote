import { describe, expect, it } from 'vitest'
import { emptyWorkspace } from '../../../src/domain/contracts'
import { entityKey } from '../../../src/domain/ids'
import { createLocal, deleteLocal, freezeNext, moveLocal, objectDraft, saveLocal } from '../../../src/sync/operations'
import { applyPushResult } from '../../../src/sync/reducer'
import { folder, note, partition } from './fixtures'

const input = { entityType: 'note' as const, entityId: 'new', title: 'A', bodyMd: 'A\r\n ', parentId: null }

describe('immutable operation lifecycle', () => {
  it.each(['note', 'folder'] as const)('freezes all four %s wire shapes separately', entityType => {
    const state = emptyWorkspace(partition)
    const initial = { ...input, entityType, entityId: 'entity' }
    createLocal(state, initial)
    saveLocal(state, { ...initial, title: 'B', bodyMd: 'B' })
    state.serverObjects['folder:parent'] = folder({ id: 'parent' })
    moveLocal(state, { ...initial, parentId: 'parent' })
    deleteLocal(state, initial)
    const expected = entityType === 'note'
      ? [{ title: 'A', body_md: 'A\r\n ', folder_id: null }, { title: 'B', body_md: 'B' }, { folder_id: 'parent' }, {}]
      : [{ name: 'A', parent_id: null }, { name: 'B' }, { parent_id: 'parent' }, {}]
    for (const [index, kind] of ['create', 'update', 'move', 'delete'].entries()) {
      const op = freezeNext(state)!
      expect(op.wire.op).toBe(kind)
      expect(op.wire.payload).toEqual(expected[index])
      expect(op.wire.base_rev).toBe(index === 0 ? undefined : String(index))
      expect(freezeNext(state)).toBeUndefined()
      applyPushResult(state, op, { op_id: op.id, status: 'applied', rev: String(index + 1), seq: String(index + 1) })
    }
    expect(state.drafts[entityKey(entityType, 'entity')].dirty).toBe(false)
  })

  it('keeps create then delete and waits for the confirmed create revision', () => {
    const state = emptyWorkspace(partition)
    createLocal(state, input)
    const create = freezeNext(state)!
    const frozen = structuredClone(create.wire)
    deleteLocal(state, input)
    expect(create.wire).toEqual(frozen)
    expect(freezeNext(state)).toBeUndefined()
    applyPushResult(state, create, { op_id: create.id, status: 'applied', rev: '19', seq: '90' })
    const deletion = freezeNext(state)!
    expect(deletion.wire).toMatchObject({ op: 'delete', base_rev: '19', payload: {} })
  })

  it('keeps A frozen while B and a later move remain independently dirty', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:new'] = objectDraft(note({ id: 'new' }))
    saveLocal(state, input)
    const a = freezeNext(state)!
    const original = structuredClone(a.wire)
    saveLocal(state, { ...input, bodyMd: 'B' })
    moveLocal(state, { ...input, parentId: 'parent' })
    applyPushResult(state, a, { op_id: a.id, status: 'applied', rev: '2', seq: '99' })
    expect(a.wire).toEqual(original)
    expect(state.drafts['note:new']).toMatchObject({ bodyMd: 'B', parentId: 'parent', dirty: true, acknowledgedVersion: '1', version: '3' })
    const b = freezeNext(state)!
    expect(b.wire).toMatchObject({ op: 'update', base_rev: '2', payload: { body_md: 'B' } })
    expect(state.cursor).toBe('0')
  })

  it('waits for an unconfirmed parent create without predicting its revision', () => {
    const state = emptyWorkspace(partition)
    createLocal(state, { ...input, entityType: 'folder', entityId: 'parent' })
    createLocal(state, { ...input, parentId: 'parent' })
    const parent = freezeNext(state)!
    expect(parent.wire.entity_type).toBe('folder')
    expect(freezeNext(state)).toBeUndefined()
    applyPushResult(state, parent, { op_id: parent.id, status: 'applied', rev: '8', seq: '10' })
    expect(freezeNext(state)?.wire.payload.folder_id).toBe('parent')
  })

  it('never auto-rebases dirty content onto an externally advanced mirror', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:new'] = objectDraft(note({ id: 'new' }))
    saveLocal(state, input)
    state.serverObjects['note:new'] = note({ id: 'new', rev: '7', updated_seq: '7' })
    expect(freezeNext(state)).toBeUndefined()
    expect(state.drafts['note:new'].base?.rev).toBe('1')
    expect(state.drafts['note:new'].blocked).toBe('remote_changed')
  })

  it('preserves edits after a successful delete as recovery and does not recreate', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:new'] = objectDraft(note({ id: 'new' }))
    deleteLocal(state, input)
    const deletion = freezeNext(state)!
    saveLocal(state, { ...input, bodyMd: 'after deletion' })
    applyPushResult(state, deletion, { op_id: deletion.id, status: 'applied', rev: '2', seq: '2' })
    expect(state.recoveryRecords[0].local?.bodyMd).toBe('after deletion')
    expect(state.drafts['note:new'].blocked).toBe('remote_deleted')
    expect(freezeNext(state)).toBeUndefined()
    expect(() => createLocal(state, input)).toThrow('already used')
  })

  it('old replay ACK never regresses a newer remote object or swallows B', () => {
    const state = emptyWorkspace(partition)
    state.drafts['note:new'] = objectDraft(note({ id: 'new' }))
    saveLocal(state, input)
    const op = freezeNext(state)!
    saveLocal(state, { ...input, bodyMd: 'B' })
    state.serverObjects['note:new'] = note({ id: 'new', rev: '7', updated_seq: '90', body_md: 'remote seven' })
    applyPushResult(state, op, { op_id: op.id, status: 'replayed', rev: '2', seq: '2' })
    expect(state.serverObjects['note:new']).toMatchObject({ rev: '7', body_md: 'remote seven' })
    expect(state.drafts['note:new']).toMatchObject({ bodyMd: 'B', dirty: true, base: { rev: '2' }, blocked: 'remote_changed' })
    expect(freezeNext(state)).toBeUndefined()
    expect(state.cursor).toBe('0')
  })
})
