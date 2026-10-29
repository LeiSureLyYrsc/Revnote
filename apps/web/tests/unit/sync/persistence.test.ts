import { describe, expect, it } from 'vitest'
import type { SyncTransport } from '../../../src/api/sync'
import { createRepository } from '../../../src/storage/repository'
import { SyncEngine } from '../../../src/sync/engine'
import { objectDraft } from '../../../src/sync/operations'
import { lease, note, page, partition } from './fixtures'

describe('sync integration with durable repository', () => {
  it('restores original command after reopen and retries without replacing its ID', async () => {
    const name = `sync-${crypto.randomUUID()}`
    let repo = await createRepository(name)
    const transport: SyncTransport = {
      async push() { throw new Error('No response') },
      async pull(_workspace, cursor) { return page([], cursor) },
      async snapshot() { return { objects: [note()], as_of_seq: '1', min_retained_seq: '0' } },
    }
    try {
      await repo.mutate(partition, lease, state => {
        state.bootstrapped = true
        state.cursor = '1'
        state.drafts['note:note'] = objectDraft(note())
      })
      const first = new SyncEngine(repo, partition, lease, transport, { wait: async () => {} })
      await first.save({ entityType: 'note', entityId: 'note', title: 'Saved', bodyMd: 'exact\r\n  ', parentId: null })
      await first.sync()
      const command = (await repo.read(partition)).operations[0].wire
      repo.close()
      repo = await createRepository(name)
      const restored = await repo.read(partition)
      expect(restored.drafts['note:note'].bodyMd).toBe('exact\r\n  ')
      expect(restored.operations[0].wire).toEqual(command)
      transport.push = async (_workspace, ops) => {
        expect(ops).toEqual([command])
        return { results: [{ op_id: ops[0].op_id, status: 'replayed', rev: '2', seq: '2' }] }
      }
      const second = new SyncEngine(repo, partition, lease, transport)
      await second.retry()
      const confirmed = await repo.read(partition)
      expect(confirmed.operations).toHaveLength(1)
      expect(confirmed.operations[0].state).toBe('acknowledged')
      expect(confirmed.cursor).toBe('1')
      expect(confirmed.drafts['note:note']).toMatchObject({ dirty: false, bodyMd: 'exact\r\n  ' })
    } finally {
      repo.close()
      await new Promise<void>((resolve, reject) => {
        const deletion = indexedDB.deleteDatabase(name)
        deletion.onsuccess = () => resolve()
        deletion.onerror = () => reject(new Error('Database cleanup failed'))
      })
    }
  })
})
