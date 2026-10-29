import type { PullResponse, ServerObject } from '../../../src/api/contracts'
import type { LocalRepository, WorkspaceState, WriteLease } from '../../../src/domain/contracts'
import { emptyWorkspace } from '../../../src/domain/contracts'
import type { Partition } from '../../../src/domain/ids'

export const partition: Partition = { deployment: 'test', userId: 'user', workspaceId: 'workspace' }
export const lease: WriteLease = { generation: 1, assertCurrent() {} }
export function note(overrides: Partial<ServerObject> = {}): ServerObject {
  return {
    entity_type: 'note', id: 'note', workspace_id: partition.workspaceId, rev: '1', updated_seq: '1',
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', deleted_at: null,
    deleted: false, title: 'Original', body_md: 'original body', folder_id: null, ...overrides,
  }
}
export function folder(overrides: Partial<ServerObject> = {}): ServerObject {
  return note({ entity_type: 'folder', id: 'folder', name: 'Folder', parent_id: null, ...overrides })
}
export function page(objects: ServerObject[] = [], cursor = '1'): PullResponse {
  return {
    objects, events: objects.map(object => ({
      seq: object.updated_seq, entity_type: object.entity_type, entity_id: object.id,
      op: 'update', rev: object.rev, op_id: 'remote', created_at: object.updated_at,
    })), next_cursor: cursor, has_more: false, last_seq: cursor, min_retained_seq: '0',
  }
}
export class MemoryRepository implements LocalRepository {
  state: WorkspaceState = emptyWorkspace(partition)
  failNext = false
  listeners = new Set<() => void>()
  async read() { return structuredClone(this.state) }
  async mutate<T>(_partition: Partition, ownership: WriteLease, change: (state: WorkspaceState) => T): Promise<T> {
    ownership.assertCurrent()
    const transaction = structuredClone(this.state)
    const result = change(transaction)
    if (this.failNext) { this.failNext = false; throw new Error('Transaction aborted') }
    ownership.assertCurrent()
    this.state = transaction
    for (const listener of this.listeners) listener()
    return result
  }
  subscribe(listener: () => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  async listProfiles() { return [] }
  async putProfile() {}
  async getCredential() { return undefined }
  async putCredential() {}
  async deleteCredential() {}
}
