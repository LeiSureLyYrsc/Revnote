import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../../src/api/client'
import type { PushStatus, WireOperation } from '../../../src/api/contracts'
import type { SyncTransport } from '../../../src/api/sync'
import { SyncEngine } from '../../../src/sync/engine'
import { freezeNext, objectDraft, saveLocal } from '../../../src/sync/operations'
import { reconcileObject } from '../../../src/sync/reconcile'
import { MemoryRepository, lease, note, page, partition } from './fixtures'

const input = { entityType: 'note' as const, entityId: 'note', title: 'local', bodyMd: 'local', parentId: null }
function setup() {
  const repo = new MemoryRepository()
  repo.state.bootstrapped = true
  repo.state.drafts['note:note'] = objectDraft(note())
  const transport: SyncTransport = {
    push: vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'applied', rev: '2', seq: '2' }] })),
    pull: vi.fn(async (_workspace, cursor) => page([], cursor)),
    snapshot: vi.fn(async () => ({ objects: [], as_of_seq: '0', min_retained_seq: '0' })),
  }
  const wait = vi.fn(async () => {})
  const engine = new SyncEngine(repo, partition, lease, transport, { wait })
  return { repo, transport, engine, wait }
}

describe('durable single-sender engine', () => {
  it.each(['applied', 'replayed'] as const)('acknowledges %s without moving pull cursor', async status => {
    const { repo, transport, engine } = setup()
    transport.push = vi.fn(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status, seq: '2', rev: '2' }] }))
    await engine.save(input)
    await engine.sync()
    expect(repo.state.operations[0].state).toBe('acknowledged')
    expect(repo.state.drafts['note:note'].dirty).toBe(false)
    expect(repo.state.cursor).toBe('0')
  })

  it.each(['conflict', 'rejected'] as const)('preserves content and blocks successors on %s', async status => {
    const { repo, transport, engine } = setup()
    transport.push = vi.fn(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status, error: { code: status, message: '', request_id: 'req', retryable: false, details: {} } }] }))
    await engine.save(input)
    await engine.sync()
    expect(repo.state.operations[0].state).toBe(status)
    expect(repo.state.drafts['note:note']).toMatchObject({ dirty: true, bodyMd: 'local', blocked: status })
    expect(repo.state.recoveryRecords).toHaveLength(1)
  })

  it.each(['unknown', 'not_attempted'] as const)('retains identical command for %s with bounded retry budget', async (status: PushStatus) => {
    const { repo, transport, engine, wait } = setup()
    const sent: WireOperation[] = []
    transport.push = vi.fn(async (_workspace, ops) => {
      sent.push(structuredClone(ops[0]))
      return { results: [{ op_id: ops[0].op_id, status }] }
    })
    await engine.save(input)
    await engine.sync()
    expect(sent).toHaveLength(5)
    for (const command of sent) expect(command).toEqual(sent[0])
    expect(repo.state).toMatchObject({ status: 'paused', failures: 5 })
    expect(repo.state.operations[0]).toMatchObject({ state: 'uncertain', attempts: 5 })
    expect(wait.mock.calls).toEqual([[1000], [2000], [4000], [8000]])
    await engine.sync()
    expect(sent).toHaveLength(5)
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'replayed', rev: '2', seq: '2' }] }))
    await engine.retry()
    expect(repo.state.operations[0].wire).toEqual(sent[0])
    expect(repo.state.operations[0].state).toBe('acknowledged')
  })

  it.each([400, 401, 404, 413])('retains command and content on whole request HTTP %s', async status => {
    const { repo, transport, engine } = setup()
    transport.push = vi.fn(async () => { throw new ApiError('request_error', status) })
    await engine.save(input)
    await engine.sync()
    expect(repo.state.status).toBe('paused')
    expect(repo.state.operations[0].state).toBe('uncertain')
    expect(repo.state.drafts['note:note'].bodyMd).toBe('local')
    expect(transport.push).toHaveBeenCalledTimes(1)
    expect(repo.state.drafts['note:note'].deleted).toBe(false)
  })

  it('retains original wire on dropped response and malformed result', async () => {
    const { repo, transport, engine } = setup()
    const sent: WireOperation[] = []
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => {
      sent.push(structuredClone(ops[0]))
      if (sent.length === 1) throw new ApiError('network_error', 0, false, true)
      if (sent.length === 2) return { results: [] }
      return { results: [{ op_id: ops[0].op_id, status: 'replayed', rev: '2', seq: '2' }] }
    })
    await engine.save(input)
    await engine.sync()
    expect(sent).toHaveLength(3)
    expect(sent[1]).toEqual(sent[0])
    expect(sent[2]).toEqual(sent[0])
    expect(repo.state.operations).toHaveLength(1)
  })

  it('serializes sync calls and persists B while A response is delayed', async () => {
    const { repo, transport, engine } = setup()
    let release!: () => void
    let started!: () => void
    const start = new Promise<void>(resolve => { started = resolve })
    const held = new Promise<void>(resolve => { release = resolve })
    let calls = 0
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => {
      calls++
      if (calls === 1) { started(); await held }
      return { results: [{ op_id: ops[0].op_id, status: 'applied', rev: String(calls + 1), seq: String(calls + 1) }] }
    })
    await engine.save(input)
    const syncing = engine.sync()
    expect(engine.sync()).toBe(syncing)
    await start
    await engine.save({ ...input, bodyMd: 'B' })
    expect(repo.state.drafts['note:note'].bodyMd).toBe('B')
    release()
    await syncing
    expect(transport.push).toHaveBeenCalledTimes(2)
    expect(repo.state.operations[1].wire).toMatchObject({ base_rev: '2', payload: { body_md: 'B' } })
    expect(repo.state.drafts['note:note']).toMatchObject({ bodyMd: 'B', dirty: false })
  })

  it('confirms inherited uncertain operations before rebuild snapshot', async () => {
    const { repo, transport, engine } = setup()
    saveLocal(repo.state, input)
    const original = freezeNext(repo.state)!
    original.state = 'uncertain'
    repo.state.status = 'rebuilding'
    const calls: string[] = []
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => { calls.push('push'); return { results: [{ op_id: ops[0].op_id, status: 'replayed', rev: '2', seq: '2' }] } })
    transport.snapshot = vi.fn(async () => { calls.push('snapshot'); return { objects: [note({ rev: '2', updated_seq: '2', body_md: 'local' })], as_of_seq: '2', min_retained_seq: '0' } })
    await engine.sync()
    expect(calls).toEqual(['push', 'snapshot'])
    expect(repo.state.operations[0].id).toBe(original.id)
  })

  it('pauses rebuild with unresolved commands instead of treating snapshot as proof', async () => {
    const { repo, transport, engine } = setup()
    saveLocal(repo.state, input)
    freezeNext(repo.state)!.state = 'uncertain'
    repo.state.status = 'rebuilding'
    transport.push = vi.fn(async () => { throw new ApiError('network_error', 0, false, true) })
    await engine.sync()
    expect(transport.snapshot).not.toHaveBeenCalled()
    expect(repo.state).toMatchObject({ status: 'paused', failures: 5 })
    expect(repo.state.drafts['note:note'].bodyMd).toBe('local')
  })

  it('preserves all local state on snapshot limits and rebuilds after 410', async () => {
    const { repo, transport, engine } = setup()
    let calls = 0
    transport.pull = vi.fn(async (_workspace, cursor) => { if (++calls === 1) throw new ApiError('cursor_expired', 410); return page([], cursor) })
    transport.snapshot = vi.fn(async () => { throw new ApiError('snapshot_too_large', 409) })
    await engine.sync()
    expect(repo.state.status).toBe('paused')
    expect(repo.state.error).toBe('rebuild_required:snapshot_too_large')
    expect(repo.state.drafts['note:note'].bodyMd).toBe('original body')
    transport.snapshot = vi.fn(async () => ({ objects: [note()], as_of_seq: '1', min_retained_seq: '0' }))
    await engine.retry()
    expect(repo.state).toMatchObject({ status: 'idle', cursor: '1', generation: '1' })
  })

  it('requires determinate old command before manual resolution and keeps recovery', async () => {
    const { repo, transport, engine } = setup()
    await engine.save(input)
    const original = freezeNext(repo.state)!
    original.state = 'uncertain'
    repo.state.drafts['note:note'].blocked = 'remote_changed'
    await expect(engine.resolve({ ...input, action: 'remote' })).rejects.toThrow('original pending')
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'conflict' }] }))
    await engine.sync()
    reconcileObject(repo.state, note({ rev: '7', updated_seq: '7', body_md: 'remote' }))
    await engine.resolve({ ...input, action: 'local' })
    const next = freezeNext(repo.state)!
    expect(next.wire).toMatchObject({ base_rev: '7', payload: { body_md: 'local' } })
    expect(next.id).not.toBe(original.id)
    expect(repo.state.recoveryRecords.length).toBeGreaterThan(0)
    expect(repo.state.recoveryRecords.some(record => record.local?.bodyMd === 'local')).toBe(true)
  })

  it('copies remote-deleted content to a new ID and never resurrects old ID', async () => {
    const { repo, engine } = setup()
    await engine.save(input)
    reconcileObject(repo.state, note({ rev: '2', updated_seq: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' }))
    const copy = await engine.resolve({ ...input, action: 'copy' })
    expect(copy.entityId).not.toBe('note')
    expect(repo.state.drafts[`note:${copy.entityId}`].bodyMd).toBe('local')
    expect(repo.state.drafts['note:note'].deleted).toBe(true)
    expect(repo.state.intents.filter(intent => intent.state === 'pending')).toMatchObject([{ kind: 'create', entityId: copy.entityId }])
  })

  it('returns no successful receipt on local commit failure and sends nothing', async () => {
    const { repo, transport, engine } = setup()
    repo.failNext = true
    await expect(engine.save(input)).rejects.toThrow('aborted')
    expect(repo.state.intents).toHaveLength(0)
    expect(transport.push).not.toHaveBeenCalled()
  })

  it('serializes separate workspace engines sharing the origin write lease', async () => {
    const { repo, transport, engine } = setup()
    const separatelyCapturedLease = { generation: lease.generation, assertCurrent: () => lease.assertCurrent() }
    const other = new SyncEngine(repo, partition, separatelyCapturedLease, transport, { wait: async () => {} })
    let release!: () => void
    let start!: () => void
    const started = new Promise<void>(resolve => { start = resolve })
    const hold = new Promise<void>(resolve => { release = resolve })
    let active = 0
    let maximum = 0
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => {
      active++; maximum = Math.max(maximum, active)
      start(); await hold; active--
      return { results: [{ op_id: ops[0].op_id, status: 'applied', rev: '2', seq: '2' }] }
    })
    await engine.save(input)
    const first = engine.sync()
    await started
    const second = other.sync()
    release()
    await Promise.all([first, second])
    expect(maximum).toBe(1)
    expect(transport.push).toHaveBeenCalledTimes(1)
  })

  it('rejects late responses after generation loss without acknowledging them', async () => {
    const { repo, transport } = setup()
    let current = true
    const ownership = { generation: 1, assertCurrent() { if (!current) throw new Error('Ownership changed') } }
    const engine = new SyncEngine(repo, partition, ownership, transport)
    let release!: () => void
    let start!: () => void
    const started = new Promise<void>(resolve => { start = resolve })
    const hold = new Promise<void>(resolve => { release = resolve })
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => {
      start(); await hold
      return { results: [{ op_id: ops[0].op_id, status: 'applied', rev: '2', seq: '2' }] }
    })
    await engine.save(input)
    const syncing = engine.sync()
    await started
    current = false
    release()
    await expect(syncing).rejects.toThrow('Ownership changed')
    expect(repo.state.operations[0].state).toBe('sending')
    expect(repo.state.drafts['note:note'].dirty).toBe(true)
  })
})

/** A client that saw the note alive, keeps editing, and only then retries. */
function staleMirrorSetup() {
  const repo = new MemoryRepository()
  repo.state.bootstrapped = true
  repo.state.cursor = '1'
  repo.state.serverObjects['note:note'] = note()
  repo.state.drafts['note:note'] = objectDraft(note())
  const transport: SyncTransport = {
    push: vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'conflict', error: { code: 'conflict', message: '', request_id: 'req', retryable: false, details: {} } }] })),
    pull: vi.fn(async (_workspace, cursor) => page([], cursor)),
    snapshot: vi.fn(async () => ({ objects: [note()], as_of_seq: '1', min_retained_seq: '0' })),
  }
  const engine = new SyncEngine(repo, partition, lease, transport, { wait: async () => {} })
  return { repo, transport, engine }
}

describe('one authoritative answer per preserved local version', () => {
  it('collapses a push conflict and the following tombstone into one recovery with the tombstone as remote', async () => {
    const { repo, transport, engine } = staleMirrorSetup()
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'conflict', error: { code: 'entity_deleted', message: '', request_id: 'req', retryable: false, details: {} } }] }))
    transport.pull = vi.fn(async (_workspace, cursor) => cursor === '1'
      ? page([note({ rev: '2', updated_seq: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' })], '2')
      : page([], cursor))
    await engine.save(input)
    await engine.sync()
    const active = repo.state.recoveryRecords.filter(record => !record.resolved)
    expect(active).toHaveLength(1)
    expect(active[0]).toMatchObject({ entityId: 'note', reason: 'remote_deleted', remote: { rev: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' } })
    expect(active[0].local?.bodyMd).toBe('local')
    expect(repo.state.drafts['note:note']).toMatchObject({ bodyMd: 'local', blocked: 'remote_deleted' })
    // A deleted identity accepts only a new one; the old ID is never revived.
    await expect(engine.resolve({ ...input, action: 'local' })).rejects.toThrow('Deleted entities require a new ID')
    expect(repo.state.recoveryRecords.filter(record => !record.resolved)).toHaveLength(1)
    const copy = await engine.resolve({ ...input, action: 'copy' })
    expect(copy.entityId).not.toBe('note')
    expect(repo.state.drafts[`note:${copy.entityId}`].bodyMd).toBe('local')
    // Explicit retirement only: the old identity is retired locally and unsent.
    expect(repo.state.drafts['note:note']).toMatchObject({ deleted: true, dirty: false })
    expect(repo.state.drafts['note:note'].blocked).toBeUndefined()
    expect(repo.state.recoveryRecords.some(record => record.local?.bodyMd === 'local')).toBe(true)
    expect(freezeNext(repo.state)?.wire).toMatchObject({ op: 'create', entity_id: copy.entityId })
  })

  it('never presents the pre-delete mirror as the remote when a command proves the entity gone', async () => {
    const { repo, transport, engine } = staleMirrorSetup()
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'conflict', error: { code: 'entity_deleted', message: '', request_id: 'req', retryable: false, details: {} } }] }))
    transport.pull = vi.fn(async () => { throw new ApiError('network_error', 0, false, true) })
    await engine.save(input)
    await engine.sync()
    let active = repo.state.recoveryRecords.filter(record => !record.resolved)
    expect(active).toHaveLength(1)
    // The unconfirmed pre-delete mirror is never presented as an authoritative remote.
    expect(active[0].remote).toBeNull()
    expect(active[0].local?.bodyMd).toBe('local')
    await expect(engine.resolve({ ...input, action: 'local' })).rejects.toThrow('Deleted entities require a new ID')
    // The tombstone fills in the same record rather than adding a second one.
    transport.pull = vi.fn(async (_workspace, cursor) => cursor === '1'
      ? page([note({ rev: '2', updated_seq: '2', deleted: true, deleted_at: '2026-01-02T00:00:00Z' })], '2')
      : page([], cursor))
    await engine.retry()
    active = repo.state.recoveryRecords.filter(record => !record.resolved)
    expect(active).toHaveLength(1)
    expect(active[0]).toMatchObject({ reason: 'remote_deleted', remote: { rev: '2', deleted: true } })
    expect(active[0].local?.bodyMd).toBe('local')
  })

  it('keeps the newest authoritative version as remote when the note was updated rather than deleted', async () => {
    const { repo, transport, engine } = staleMirrorSetup()
    transport.push = vi.fn<SyncTransport['push']>(async (_workspace, ops) => ({ results: [{ op_id: ops[0].op_id, status: 'conflict', error: { code: 'rev_conflict', message: '', request_id: 'req', retryable: false, details: {} } }] }))
    transport.pull = vi.fn(async (_workspace, cursor) => cursor === '1'
      ? page([note({ rev: '2', updated_seq: '2', body_md: 'remote two' })], '2')
      : page([], cursor))
    await engine.save(input)
    await engine.sync()
    const active = repo.state.recoveryRecords.filter(record => !record.resolved)
    expect(active).toHaveLength(1)
    expect(active[0]).toMatchObject({ reason: 'remote_changed', remote: { rev: '2', body_md: 'remote two', deleted: false } })
    expect(active[0].base?.body_md).toBe('original body')
    expect(active[0].local?.bodyMd).toBe('local')
    // An advanced live note stays resolvable against the new confirmed revision.
    await engine.resolve({ ...input, action: 'local' })
    const next = freezeNext(repo.state)!
    expect(next.wire).toMatchObject({ base_rev: '2', payload: { body_md: 'local' } })
  })
})
