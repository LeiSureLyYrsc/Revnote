import type { ServerObject } from '../api/contracts'
import type { Draft, WorkspaceState } from '../domain/contracts'

function object(value: ServerObject | null) {
  if (!value) return null
  return {
    entity_type: value.entity_type, id: value.id, workspace_id: value.workspace_id,
    rev: value.rev, updated_seq: value.updated_seq, created_at: value.created_at,
    updated_at: value.updated_at, deleted_at: value.deleted_at, deleted: value.deleted,
    name: value.name, parent_id: value.parent_id, title: value.title,
    body_md: value.body_md, folder_id: value.folder_id,
  }
}
function draft(value: Draft | null) {
  if (!value) return null
  return {
    key: value.key, entityType: value.entityType, entityId: value.entityId,
    version: value.version, acknowledgedVersion: value.acknowledgedVersion,
    title: value.title, bodyMd: value.bodyMd, parentId: value.parentId,
    base: object(value.base), dirty: value.dirty, deleted: value.deleted, blocked: value.blocked,
  }
}
function payload(value: Record<string, string | null>) {
  return Object.fromEntries(['name', 'title', 'body_md', 'parent_id', 'folder_id'].filter(key => key in value).map(key => [key, value[key]]))
}

/** Project known recovery fields only. Never serialize a repository or identity record. */
export function localArchive(state: WorkspaceState) {
  return {
    format: 'revnote-recovery-v1', exportedAt: new Date().toISOString(),
    workspaces: [{
      partition: { deployment: state.partition.deployment, userId: state.partition.userId, workspaceId: state.partition.workspaceId },
      generation: state.generation, cursor: state.cursor, bootstrapped: state.bootstrapped,
      status: state.status, error: state.error, failures: state.failures,
      serverObjects: Object.fromEntries(Object.entries(state.serverObjects).map(([key, value]) => [key, object(value)])),
      drafts: Object.fromEntries(Object.entries(state.drafts).map(([key, value]) => [key, draft(value)])),
      intents: state.intents.map(value => ({ id: value.id, entityType: value.entityType, entityId: value.entityId, kind: value.kind, draftVersion: value.draftVersion, payload: payload(value.payload), state: value.state, operationId: value.operationId })),
      operations: state.operations.map(value => ({
        id: value.id, wire: { op_id: value.wire.op_id, entity_type: value.wire.entity_type, entity_id: value.wire.entity_id, op: value.wire.op, base_rev: value.wire.base_rev, payload: payload(value.wire.payload) },
        intentIds: [...value.intentIds], coveredVersion: value.coveredVersion, state: value.state, attempts: value.attempts, error: value.error,
        result: value.result ? { op_id: value.result.op_id, status: value.result.status, seq: value.result.seq, rev: value.result.rev, error: value.result.error ? { code: value.result.error.code, request_id: value.result.error.request_id, retryable: value.result.error.retryable } : null } : undefined,
      })),
      recoveryRecords: state.recoveryRecords.map(value => ({ id: value.id, entityType: value.entityType, entityId: value.entityId, reason: value.reason, base: object(value.base), local: draft(value.local), remote: object(value.remote), resolved: value.resolved, createdAt: value.createdAt })),
    }],
  }
}

export function downloadLocalArchive(state: WorkspaceState): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(localArchive(state), null, 2)], { type: 'application/json;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'revnote-local-recovery.json'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
