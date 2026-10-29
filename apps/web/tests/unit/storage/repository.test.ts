import 'fake-indexeddb/auto'
import { IDBFactory, IDBObjectStore } from 'fake-indexeddb'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Draft, FrozenOperation, WriteLease } from '../../../src/domain/contracts'
import { entityKey, type Partition } from '../../../src/domain/ids'
import { createRepository, type IndexedDbRepository } from '../../../src/storage/repository'
import { openDatabase, storageError } from '../../../src/storage/database'

const partition: Partition = { deployment: 'deployment-a', userId: 'user-a', workspaceId: 'workspace-a' }
const lease: WriteLease = { generation: 1, assertCurrent() {} }
const entityId = '11111111-1111-4111-8111-111111111111'
const key = entityKey('note', entityId)

function draft(bodyMd = 'local A', entityType: 'note' | 'folder' = 'note'): Draft {
  return { key: entityKey(entityType, entityId), entityType, entityId, version: '1', acknowledgedVersion: '0', title: 'Title', bodyMd, parentId: null, base: null, dirty: true, deleted: false }
}

function operation(): FrozenOperation {
  return { id: 'operation-a', wire: { op_id: 'operation-a', entity_type: 'note', entity_id: entityId, op: 'create', payload: { title: 'Title', body_md: 'local A' } }, intentIds: ['intent-a'], coveredVersion: '1', state: 'sending', attempts: 1 }
}

describe('durable partitioned local repository', () => {
  let repository: IndexedDbRepository
  beforeEach(async () => {
    globalThis.indexedDB = new IDBFactory()
    repository = await createRepository('storage-unit')
  })
  afterEach(() => { vi.restoreAllMocks(); repository.close() })

  it('commits draft and intent atomically and notifies only after commit', async () => {
    const seen: string[] = []
    repository.subscribe(() => { seen.push('committed') })
    const result = await repository.mutate(partition, lease, state => {
      state.drafts[key] = draft()
      state.intents.push({ id: 'intent-a', entityType: 'note', entityId, kind: 'create', draftVersion: '1', payload: { title: 'Title' }, state: 'pending' })
      expect(seen).toEqual([])
      return state.drafts[key]
    })
    expect(seen).toEqual(['committed'])
    result.bodyMd = 'caller changed'
    const db = await openDatabase('storage-unit')
    expect(await db.count('drafts')).toBe(1)
    expect(await db.count('intents')).toBe(1)
    db.close()
    repository.close()
    repository = await createRepository('storage-unit')
    const restored = await repository.read(partition)
    expect(restored.drafts[key].bodyMd).toBe('local A')
    expect(restored.intents).toHaveLength(1)
  })

  it('rolls back a thrown draft/intents/pull mutation without notifying', async () => {
    const listener = vi.fn()
    repository.subscribe(listener)
    await expect(repository.mutate(partition, lease, state => {
      state.drafts[key] = draft()
      state.cursor = '9007199254740993'
      throw new Error('controlled mutation failure')
    })).rejects.toMatchObject({ code: 'unavailable' })
    expect((await repository.read(partition)).drafts).toEqual({})
    expect((await repository.read(partition)).cursor).toBe('0')
    expect(listener).not.toHaveBeenCalled()
  })

  it('rejects asynchronous mutators without persisting partial changes', async () => {
    await expect(repository.mutate(partition, lease, async state => {
      state.drafts[key] = draft()
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    expect((await repository.read(partition)).drafts).toEqual({})
  })

  it('isolates two users, deployments, workspaces and equal note/folder UUIDs', async () => {
    const partitions = [partition, { ...partition, userId: 'user-b' }, { ...partition, deployment: 'deployment-b' }, { ...partition, workspaceId: 'workspace-b' }]
    for (const [i, scope] of partitions.entries()) {
      await repository.mutate(scope, lease, state => {
        state.drafts[key] = draft(`note ${i}`)
        state.drafts[entityKey('folder', entityId)] = draft(`folder ${i}`, 'folder')
      })
    }
    for (const [i, scope] of partitions.entries()) {
      const state = await repository.read(scope)
      expect(state.drafts[key].bodyMd).toBe(`note ${i}`)
      expect(state.drafts[entityKey('folder', entityId)].bodyMd).toBe(`folder ${i}`)
    }
    const db = await openDatabase('storage-unit')
    expect(await db.count('drafts')).toBe(8)
    db.close()
  })

  it('checks ownership at entry and again immediately before commit', async () => {
    const stale: WriteLease = { generation: 0, assertCurrent() { throw new Error('stale owner') } }
    const callback = vi.fn()
    await expect(repository.mutate(partition, stale, callback)).rejects.toMatchObject({ code: 'lease' })
    expect(callback).not.toHaveBeenCalled()
    let current = true
    const changing: WriteLease = { generation: 1, assertCurrent() { if (!current) throw new Error('lease handed off') } }
    await expect(repository.mutate(partition, changing, state => {
      state.drafts[key] = draft()
      current = false
    })).rejects.toMatchObject({ code: 'lease' })
    expect((await repository.read(partition)).drafts).toEqual({})
  })

  it('rejects partition changes and incorrectly keyed entities without committing', async () => {
    await expect(repository.mutate(partition, lease, state => {
      state.partition.userId = 'different-user'
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    await expect(repository.mutate(partition, lease, state => {
      state.drafts[entityId] = draft()
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    await expect(repository.mutate(partition, lease, state => {
      state.drafts[key] = { ...draft(), key: 'wrong-key' }
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    expect((await repository.read(partition)).drafts).toEqual({})
  })

  it('can guard late pull callbacks against both expected cursor and mirror generation', async () => {
    await repository.mutate(partition, lease, state => { state.cursor = '5'; state.generation = 'mirror-2' })
    for (const [expectedCursor, expectedGeneration] of [['4', 'mirror-2'], ['5', 'mirror-1']]) {
      const applied = await repository.mutate(partition, lease, state => {
        if (state.cursor !== expectedCursor || state.generation !== expectedGeneration) return false
        state.cursor = '6'
        return true
      })
      expect(applied).toBe(false)
    }
    expect((await repository.read(partition)).cursor).toBe('5')
  })

  it('does not persist refresh credentials unless remember is explicit and exports none', async () => {
    await repository.putCredential({ key: 'identity-a', userId: partition.userId, generation: 1, remember: true, refreshToken: 'secret_refresh_a', refreshInflight: true }, lease)
    await repository.putCredential({ key: 'identity-b', userId: 'user-b', generation: 1, remember: false, refreshToken: 'secret_refresh_b', refreshInflight: false }, lease)
    await repository.mutate(partition, lease, state => { state.drafts[key] = draft() })
    expect((await repository.getCredential('identity-a'))?.refreshToken).toBe('secret_refresh_a')
    expect((await repository.getCredential('identity-b'))?.refreshToken).toBeUndefined()
    const exported = await repository.exportRecovery(partition)
    expect(JSON.stringify(exported)).not.toContain('secret_refresh')
    expect(Object.keys(exported).sort()).toEqual(['exportedAt', 'format', 'workspaces'])
    exported.workspaces[0].drafts[key].bodyMd = 'export copy'
    expect((await repository.read(partition)).drafts[key].bodyMd).toBe('local A')
  })

  it('rejects frozen payload/base/version mutation and duplicate unresolved entity commands', async () => {
    await repository.mutate(partition, lease, state => { state.operations.push(operation()) })
    await expect(repository.mutate(partition, lease, state => {
      state.operations[0].wire.payload.body_md = 'replaced command'
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    await expect(repository.mutate(partition, lease, state => {
      state.operations[0].wire.base_rev = '3'
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    await expect(repository.mutate(partition, lease, state => {
      state.operations[0].coveredVersion = '2'
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    await expect(repository.mutate(partition, lease, state => {
      const successor = operation()
      successor.id = successor.wire.op_id = 'operation-b'
      state.operations.push(successor)
    })).rejects.toMatchObject({ code: 'invalid_mutation' })
    expect((await repository.read(partition)).operations[0].wire.payload.body_md).toBe('local A')
  })

  it('retains successor draft when acknowledging an earlier frozen operation', async () => {
    await repository.mutate(partition, lease, state => {
      state.drafts[key] = draft()
      state.operations.push(operation())
    })
    await repository.mutate(partition, lease, state => {
      state.drafts[key].bodyMd = 'local B'
      state.drafts[key].version = '2'
    })
    await repository.mutate(partition, lease, state => {
      state.operations[0].state = 'acknowledged'
      state.operations[0].result = { op_id: 'operation-a', status: 'applied', rev: '1', seq: '1' }
      state.drafts[key].acknowledgedVersion = state.operations[0].coveredVersion
    })
    const state = await repository.read(partition)
    expect(state.drafts[key]).toMatchObject({ version: '2', acknowledgedVersion: '1', bodyMd: 'local B', dirty: true })
    expect(state.cursor).toBe('0')
    expect(state.operations[0].wire.payload.body_md).toBe('local A')
  })

  it('maps quota errors and rolls back all facts rather than evicting drafts', async () => {
    await repository.mutate(partition, lease, state => { state.drafts[key] = draft() })
    const original = IDBObjectStore.prototype.put
    vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementation(function (this: IDBObjectStore, ...args: Parameters<typeof original>) {
      if (this.name === 'drafts') throw new DOMException('controlled quota', 'QuotaExceededError')
      return original.apply(this, args)
    })
    const listener = vi.fn()
    repository.subscribe(listener)
    await expect(repository.mutate(partition, lease, state => {
      state.drafts[key].bodyMd = 'not committed'
      state.cursor = '99'
    })).rejects.toMatchObject({ code: 'quota' })
    expect((await repository.read(partition)).drafts[key].bodyMd).toBe('local A')
    expect((await repository.read(partition)).cursor).toBe('0')
    expect(listener).not.toHaveBeenCalled()
  })

  it('reports transaction abort distinctly without clearing committed records', async () => {
    await repository.mutate(partition, lease, state => { state.drafts[key] = draft() })
    await expect(repository.mutate(partition, lease, () => { throw new DOMException('controlled abort', 'AbortError') })).rejects.toMatchObject({ code: 'aborted' })
    expect((await repository.read(partition)).drafts[key].bodyMd).toBe('local A')
    expect(storageError(new DOMException('quota', 'QuotaExceededError')).code).toBe('quota')
  })

  it('persists server generations and recovery facts atomically with pull cursor', async () => {
    await repository.mutate(partition, lease, state => {
      state.generation = 'generation-1'
      state.bootstrapped = true
      state.cursor = '9007199254740993'
      state.serverObjects[key] = {
        entity_type: 'note', id: entityId, workspace_id: partition.workspaceId,
        rev: '9007199254740994', updated_seq: '9007199254740995',
        created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', deleted_at: null,
        title: 'Remote', body_md: 'Remote body', folder_id: null,
      }
      state.recoveryRecords.push({ id: 'recovery-a', entityType: 'note', entityId, reason: 'remote-delete', base: null, local: draft(), remote: null, resolved: false, createdAt: '2026-01-01T00:00:00Z' })
    })
    const db = await openDatabase('storage-unit')
    expect(await db.count('server_objects')).toBe(1)
    expect(await db.count('recovery_records')).toBe(1)
    db.close()
    await expect(repository.mutate(partition, lease, state => {
      state.generation = 'generation-2'
      state.cursor = '9007199254740999'
      state.serverObjects = {}
      throw new DOMException('staging activation failed', 'AbortError')
    })).rejects.toMatchObject({ code: 'aborted' })
    repository.close()
    repository = await createRepository('storage-unit')
    expect(await repository.read(partition)).toMatchObject({
      generation: 'generation-1', cursor: '9007199254740993', bootstrapped: true,
      serverObjects: { [key]: { rev: '9007199254740994' } },
      recoveryRecords: [{ id: 'recovery-a', local: { bodyMd: 'local A' } }],
    })
  })

  it('rolls back credential updates if ownership changes before commit', async () => {
    await repository.putCredential({ key: 'identity-a', userId: partition.userId, generation: 1, remember: true, refreshToken: 'secret_original', refreshInflight: true }, lease)
    let checks = 0
    const changing: WriteLease = { generation: 2, assertCurrent() { if (++checks === 3) throw new Error('ownership changed') } }
    await expect(repository.putCredential({ key: 'identity-a', userId: partition.userId, generation: 2, remember: true, refreshToken: 'secret_replacement', refreshInflight: false }, changing)).rejects.toMatchObject({ code: 'lease' })
    expect(await repository.getCredential('identity-a')).toMatchObject({ generation: 1, refreshToken: 'secret_original', refreshInflight: true })
    await repository.putProfile({ key: 'profile-a', deployment: partition.deployment, userId: partition.userId, email: 'user-a@example.invalid', active: true, locked: false, workspaces: [] }, lease)
    const profiles = await repository.listProfiles()
    profiles[0].locked = true
    expect((await repository.listProfiles())[0].locked).toBe(false)
    await repository.deleteCredential('identity-a', lease)
    expect(await repository.getCredential('identity-a')).toBeUndefined()
  })

  it('returns detached reads and does not report committed saves as failed if an observer throws', async () => {
    repository.subscribe(() => { throw new Error('broken observer') })
    await repository.mutate(partition, lease, state => { state.drafts[key] = draft() })
    const read = await repository.read(partition)
    read.drafts[key].bodyMd = 'detached'
    expect((await repository.read(partition)).drafts[key].bodyMd).toBe('local A')
  })
})

describe('versioned database lifecycle', () => {
  beforeEach(() => { globalThis.indexedDB = new IDBFactory() })
  afterEach(() => { vi.restoreAllMocks() })

  it('reports blocked upgrades explicitly and never deletes the old database', async () => {
    const request = indexedDB.open('blocked-db')
    const blocker = await new Promise<IDBDatabase>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
      request.onupgradeneeded = () => { request.result.createObjectStore('preserved') }
    })
    // Exercise a real fake-indexeddb blocked upgrade. Only the requested target
    // version is advanced, simulating the next release's schema version.
    const original = indexedDB.open.bind(indexedDB)
    vi.spyOn(indexedDB, 'open').mockImplementationOnce((name, version) => original(name, (version ?? 1) + 1))
    await expect(openDatabase('blocked-db')).rejects.toMatchObject({ code: 'blocked' })
    expect(blocker.objectStoreNames.contains('preserved')).toBe(true)
    blocker.close()
  })

  it('invalidates old repositories on versionchange rather than silently reopening', async () => {
    const repository = await createRepository('change-db')
    const request = indexedDB.open('change-db', 2)
    const upgraded = await new Promise<IDBDatabase>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    await expect(repository.read(partition)).rejects.toMatchObject({ code: 'versionchange' })
    upgraded.close()
    repository.close()
    await expect(openDatabase('change-db')).rejects.toMatchObject({ code: 'versionchange' })
  })
})
