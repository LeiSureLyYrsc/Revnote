import type { PullResponse, ServerObject, SnapshotResponse } from '../api/contracts'
import type { WorkspaceState } from '../domain/contracts'
import { compareDecimal, decimal, incrementDecimal } from '../domain/decimal'
import { entityKey } from '../domain/ids'
import { confirmRemote, isDeleted, objectDraft, operationIsActive, preserve } from './operations'

export function reconcileObject(state: WorkspaceState, object: ServerObject) {
  if (object.workspace_id !== state.partition.workspaceId) throw new Error('Snapshot partition mismatch')
  const key = entityKey(object.entity_type, object.id)
  const previous = state.serverObjects[key]
  if (previous && (compareDecimal(object.rev, previous.rev) < 0 || compareDecimal(object.updated_seq, previous.updated_seq) < 0)) return
  state.serverObjects[key] = structuredClone(object)
  const draft = state.drafts[key]
  if (draft?.base && compareDecimal(object.rev, draft.base.rev) < 0) return
  if (!draft || !draft.dirty) {
    const replacement = objectDraft(object)
    if (draft) { replacement.version = draft.version; replacement.acknowledgedVersion = draft.version }
    state.drafts[key] = replacement
    return
  }
  const newer = !draft.base || compareDecimal(object.rev, draft.base.rev) > 0
  if (!newer) return
  // A current object cannot answer whether an outstanding command executed. Its
  // original command must be replayed before an explicit resolution is allowed.
  if (isDeleted(object)) {
    // The tombstone is authoritative for this entity: an earlier record made by
    // a push conflict must stop presenting a possibly stale pre-delete mirror.
    confirmRemote(state, draft.entityType, draft.entityId, object)
    preserve(state, draft, 'remote_deleted', object)
    draft.blocked = 'remote_deleted'
  } else {
    const inflight = state.operations.some(op => operationIsActive(state, op) && op.wire.entity_type === draft.entityType && op.wire.entity_id === draft.entityId && ['pending', 'sending', 'uncertain'].includes(op.state))
    confirmRemote(state, draft.entityType, draft.entityId, object)
    if (!inflight) {
      preserve(state, draft, 'remote_changed', object)
      draft.blocked = 'remote_changed'
    }
  }
}

/** Compare and commit happens in the same repository transaction as the page. */
export function applyPull(state: WorkspaceState, page: PullResponse, expectedCursor: string, expectedGeneration: string): boolean {
  if (state.cursor !== expectedCursor || state.generation !== expectedGeneration) return false
  decimal(page.next_cursor)
  decimal(page.last_seq)
  decimal(page.min_retained_seq)
  let last = expectedCursor
  for (const event of page.events) {
    decimal(event.rev)
    if (event.seq !== incrementDecimal(last)) throw new Error('cursor_invalid')
    last = event.seq
    if (!page.objects.some(object => object.entity_type === event.entity_type && object.id === event.entity_id && compareDecimal(object.rev, event.rev) >= 0 && compareDecimal(object.updated_seq, event.seq) >= 0)) throw new Error('cursor_invalid')
  }
  if (last !== page.next_cursor || compareDecimal(page.last_seq, page.next_cursor) < 0 || (page.has_more && page.events.length === 0)) throw new Error('cursor_invalid')
  for (const object of page.objects) reconcileObject(state, object)
  state.cursor = page.next_cursor
  return true
}

/** A complete in-memory staging image is activated with one atomic IDB commit. */
export function activateSnapshot(state: WorkspaceState, snapshot: SnapshotResponse, expectedGeneration: string): boolean {
  if (state.generation !== expectedGeneration) return false
  decimal(snapshot.as_of_seq)
  decimal(snapshot.min_retained_seq)
  if (compareDecimal(snapshot.as_of_seq, state.cursor) < 0) throw new Error('cursor_invalid')
  for (const draft of Object.values(state.drafts)) {
    if (draft.base && compareDecimal(snapshot.as_of_seq, draft.base.updated_seq) < 0) throw new Error('cursor_invalid')
  }
  const staged: Record<string, ServerObject> = {}
  for (const object of snapshot.objects) {
    decimal(object.rev)
    decimal(object.updated_seq)
    if (object.workspace_id !== state.partition.workspaceId || compareDecimal(object.updated_seq, snapshot.as_of_seq) > 0 || isDeleted(object)) throw new Error('Invalid complete snapshot')
    const key = entityKey(object.entity_type, object.id)
    if (staged[key]) throw new Error('Duplicate snapshot object')
    const previous = state.serverObjects[key]
    if (previous && (compareDecimal(object.rev, previous.rev) < 0 || compareDecimal(object.updated_seq, previous.updated_seq) < 0)) throw new Error('cursor_invalid')
    staged[key] = structuredClone(object)
  }
  // Keep local material even when the active-only snapshot omits a deleted ID.
  for (const draft of Object.values(state.drafts)) {
    const remote = staged[draft.key]
    if (!remote && draft.base) {
      if (draft.dirty) {
        // A complete active-only snapshot is proof the entity no longer exists.
        confirmRemote(state, draft.entityType, draft.entityId, null)
        preserve(state, draft, 'snapshot_missing', null)
        draft.blocked = 'remote_deleted'
      } else {
        draft.deleted = true
      }
    }
  }
  state.serverObjects = {}
  for (const object of Object.values(staged)) reconcileObject(state, object)
  state.generation = incrementDecimal(state.generation)
  state.cursor = snapshot.as_of_seq
  state.bootstrapped = true
  return true
}
