import type { PushResult, ServerObject } from '../api/contracts'
import type { FrozenOperation, WorkspaceState } from '../domain/contracts'
import { compareDecimal, decimal } from '../domain/decimal'
import { entityKey } from '../domain/ids'
import { isDeleted, objectDraft, preserve } from './operations'

export class IndeterminateResult extends Error {
  constructor() { super('Push response did not confirm the original command') }
}

/** Construct only the acknowledged historical base, never claim it is current. */
function acknowledgedBase(state: WorkspaceState, op: FrozenOperation, result: PushResult): ServerObject {
  const key = entityKey(op.wire.entity_type, op.wire.entity_id)
  const draft = state.drafts[key]
  if (!result.rev || !result.seq) throw new IndeterminateResult()
  decimal(result.rev)
  decimal(result.seq)
  const now = new Date().toISOString()
  const base: ServerObject = {
    entity_type: op.wire.entity_type, id: op.wire.entity_id, workspace_id: state.partition.workspaceId,
    created_at: now, updated_at: now, deleted_at: null, ...draft?.base,
    rev: result.rev, updated_seq: result.seq,
  }
  const payload = op.wire.payload
  if (typeof payload.name === 'string') base.name = payload.name
  if (typeof payload.title === 'string') base.title = payload.title
  if (typeof payload.body_md === 'string') base.body_md = payload.body_md
  if ('parent_id' in payload) base.parent_id = payload.parent_id
  if ('folder_id' in payload) base.folder_id = payload.folder_id
  if (op.wire.op === 'delete') { base.deleted = true; base.deleted_at = now }
  return base
}

/** ACK only its captured version and intent IDs, never later local edits/actions. */
export function applyPushResult(state: WorkspaceState, op: FrozenOperation, result: PushResult) {
  if (result.op_id !== op.id) throw new IndeterminateResult()
  const draft = state.drafts[entityKey(op.wire.entity_type, op.wire.entity_id)]
  if (result.status === 'applied' || result.status === 'replayed') {
    const base = acknowledgedBase(state, op, result)
    op.result = structuredClone(result)
    op.state = 'acknowledged'
    op.error = undefined
    for (const intent of state.intents) if (op.intentIds.includes(intent.id)) intent.state = 'done'
    if (draft) {
      if (!draft.base || compareDecimal(base.rev, draft.base.rev) >= 0) draft.base = base
      if (compareDecimal(op.coveredVersion, draft.acknowledgedVersion) > 0) draft.acknowledgedVersion = op.coveredVersion
      draft.dirty = compareDecimal(draft.version, draft.acknowledgedVersion) > 0 || state.intents.some(intent => intent.entityType === draft.entityType && intent.entityId === draft.entityId && intent.state !== 'done')
      if (op.wire.op === 'delete' && draft.dirty) {
        preserve(state, draft, 'edit_after_delete', base)
        draft.blocked = 'remote_deleted'
      }
      // An own ACK may advance our own successor basis; an external newer rev
      // must still stop those successors rather than silently rebasing them.
      const remote = state.serverObjects[draft.key]
      if (draft.dirty && remote && compareDecimal(remote.rev, base.rev) > 0) {
        preserve(state, draft, 'remote_changed', remote)
        draft.blocked = remote.deleted || remote.deleted_at ? 'remote_deleted' : 'remote_changed'
      } else if (!draft.dirty && remote && compareDecimal(remote.rev, base.rev) > 0) {
        const replacement = objectDraft(remote)
        replacement.version = draft.version
        replacement.acknowledgedVersion = draft.version
        state.drafts[draft.key] = replacement
      } else if (!draft.dirty && draft.blocked === 'remote_deleted' && op.wire.op === 'delete') {
        draft.blocked = undefined
      }
    }
    // Never advance cursor here. Result rev/seq are historical and contain no body.
    return
  }
  op.result = structuredClone(result)
  if (result.status === 'unknown' || result.status === 'not_attempted') {
    op.state = 'uncertain'
    op.error = result.error?.code ?? result.status
    throw new IndeterminateResult()
  }
  if (result.status === 'rejected' && result.error?.retryable) {
    op.state = 'uncertain'
    op.error = result.error.code
    throw new IndeterminateResult()
  }
  if (result.status !== 'conflict' && result.status !== 'rejected') throw new IndeterminateResult()
  op.state = result.status
  op.error = result.error?.code ?? result.status
  for (const intent of state.intents) if (op.intentIds.includes(intent.id)) intent.state = 'blocked'
  if (draft) {
    const cached = state.serverObjects[draft.key]
    // `entity_deleted` proves the entity is gone. A cached live mirror is then
    // known false and is dropped so no action can resurrect the old identity;
    // the draft, its base and its text stay untouched. A cached tombstone is
    // already the authoritative answer and stays. `not_found` is deliberately
    // not read this way: it can also mean the workspace itself is gone, which
    // must never be treated as "this object was deleted".
    const absent = op.error === 'entity_deleted'
    const confirmed = absent && cached && !isDeleted(cached) ? null : cached ?? null
    if (absent && cached && !isDeleted(cached)) delete state.serverObjects[draft.key]
    draft.blocked = absent ? 'remote_deleted' : op.error!
    preserve(state, draft, absent ? 'remote_deleted' : result.status, confirmed)
  }
}
