import type { OperationKind, ServerObject } from '../api/contracts'
import type { Draft, DraftInput, FrozenOperation, Intent, RecoveryRecord, WorkspaceState } from '../domain/contracts'
import { compareDecimal, incrementDecimal } from '../domain/decimal'
import { entityKey, newId, type EntityType } from '../domain/ids'

export interface EntityInput { entityType: EntityType; entityId: string }
export interface MoveInput extends EntityInput { parentId: string | null }
export interface LocalReceipt extends EntityInput { version: string }

export function isDeleted(object: ServerObject): boolean { return object.deleted === true || object.deleted_at !== null }

export function objectDraft(object: ServerObject): Draft {
  return {
    key: entityKey(object.entity_type, object.id), entityType: object.entity_type, entityId: object.id,
    version: '0', acknowledgedVersion: '0', title: object.name ?? object.title ?? '',
    bodyMd: object.body_md ?? '', parentId: object.parent_id ?? object.folder_id ?? null,
    base: structuredClone(object), dirty: false, deleted: isDeleted(object),
  }
}

export function getDraft(state: WorkspaceState, input: EntityInput): Draft {
  const key = entityKey(input.entityType, input.entityId)
  const current = state.drafts[key]
  if (current) return current
  const remote = state.serverObjects[key]
  if (!remote || isDeleted(remote)) throw new Error('Entity is not available locally')
  const draft = objectDraft(remote)
  state.drafts[key] = draft
  return draft
}

export function payloadFor(draft: Draft, kind: OperationKind): Record<string, string | null> {
  if (kind === 'delete') return {}
  if (kind === 'move') return draft.entityType === 'folder' ? { parent_id: draft.parentId } : { folder_id: draft.parentId }
  const content: Record<string, string | null> = draft.entityType === 'folder' ? { name: draft.title } : { title: draft.title, body_md: draft.bodyMd }
  return kind === 'create' ? { ...content, ...payloadFor(draft, 'move') } : content
}

export function appendIntent(state: WorkspaceState, draft: Draft, kind: OperationKind): Intent {
  const intent: Intent = {
    id: newId(), entityType: draft.entityType, entityId: draft.entityId, kind,
    draftVersion: draft.version, payload: payloadFor(draft, kind), state: 'pending',
  }
  state.intents.push(intent)
  return intent
}

function validateInput(input: DraftInput) {
  if ([input.title, input.bodyMd].some(value => value.includes('\0'))) throw new Error('NUL is not supported')
  if (input.entityType === 'folder' && !input.title.trim()) throw new Error('Folder name must not be blank')
  if (input.entityType === 'folder' && input.parentId === input.entityId) throw new Error('A folder cannot contain itself')
}

export function createLocal(state: WorkspaceState, input: DraftInput): LocalReceipt {
  validateInput(input)
  const key = entityKey(input.entityType, input.entityId)
  if (state.drafts[key] || state.serverObjects[key] || state.operations.some(op => op.wire.entity_type === input.entityType && op.wire.entity_id === input.entityId)) {
    throw new Error('Entity ID was already used; create with a new ID')
  }
  const draft: Draft = {
    ...input, key, version: '1', acknowledgedVersion: '0', base: null, dirty: true, deleted: false,
  }
  state.drafts[key] = draft
  appendIntent(state, draft, 'create')
  return receipt(draft)
}

export function saveLocal(state: WorkspaceState, input: DraftInput): LocalReceipt {
  validateInput(input)
  const draft = getDraft(state, input)
  // Content and position are independent protocol operations. Never smuggle a move into update.
  if (draft.title === input.title && draft.bodyMd === input.bodyMd) return receipt(draft)
  draft.version = incrementDecimal(draft.version)
  draft.title = input.title
  draft.bodyMd = input.bodyMd
  draft.dirty = true
  appendIntent(state, draft, 'update')
  return receipt(draft)
}

export function moveLocal(state: WorkspaceState, input: MoveInput): LocalReceipt {
  const draft = getDraft(state, input)
  if (draft.entityType === 'folder' && input.parentId === draft.entityId) throw new Error('A folder cannot contain itself')
  if (draft.deleted) throw new Error('Deleted entities cannot be moved')
  if (draft.parentId === input.parentId) return receipt(draft)
  draft.version = incrementDecimal(draft.version)
  draft.parentId = input.parentId
  draft.dirty = true
  appendIntent(state, draft, 'move')
  return receipt(draft)
}

export function deleteLocal(state: WorkspaceState, input: EntityInput): LocalReceipt {
  const draft = getDraft(state, input)
  if (draft.deleted) return receipt(draft)
  draft.version = incrementDecimal(draft.version)
  draft.deleted = true
  draft.dirty = true
  appendIntent(state, draft, 'delete')
  return receipt(draft)
}

export function receipt(draft: Draft): LocalReceipt {
  return { entityType: draft.entityType, entityId: draft.entityId, version: draft.version }
}

/** Records that describe the same local edit of the same entity. */
function sameSnapshot(record: RecoveryRecord, draft: Draft): boolean {
  const local = record.local
  return !!local && local.version === draft.version && local.title === draft.title
    && local.bodyMd === draft.bodyMd && local.parentId === draft.parentId && local.deleted === draft.deleted
}

/**
 * A newer answer replaces an older claim. A tombstone always wins: it proves the
 * object is gone, so no cached live mirror may keep standing in for it. Ties keep
 * the incoming answer, which is the one just read from the server.
 */
function adoptRemote(known: ServerObject | null, incoming: ServerObject | null): ServerObject | null {
  if (!incoming) return known ? structuredClone(known) : null
  if (!known) return structuredClone(incoming)
  if (incoming.deleted === true || incoming.deleted_at !== null) return structuredClone(incoming)
  if (known.deleted === true || known.deleted_at !== null) return structuredClone(known)
  return compareDecimal(incoming.rev, known.rev) >= 0 ? structuredClone(incoming) : structuredClone(known)
}

/** Push failure labels are provisional; a pulled object names the cause better. */
const PROVISIONAL_REASONS = new Set(['conflict', 'rejected'])
/** Causes that prove the entity is gone; they outrank a mere version conflict. */
const DELETED_REASONS = new Set(['remote_deleted', 'snapshot_missing', 'edit_after_delete'])

/**
 * One unresolved record per entity and local version. A later, authoritative
 * answer for that same version updates its `remote` and cause instead of adding
 * a contradictory twin. Local content is only ever copied, never replaced.
 */
export function preserve(state: WorkspaceState, draft: Draft, reason: string, remote: ServerObject | null): RecoveryRecord {
  // An explicit resolution is archived as its own entry: it must never rewrite
  // the conflict record the user acted on.
  const existing = reason.startsWith('manual_') ? undefined : state.recoveryRecords.find(record =>
    !record.resolved && record.entityType === draft.entityType && record.entityId === draft.entityId && sameSnapshot(record, draft))
  if (existing) {
    if (reason !== existing.reason && (PROVISIONAL_REASONS.has(existing.reason) || DELETED_REASONS.has(reason))) existing.reason = reason
    existing.remote = adoptRemote(existing.remote, remote)
    return existing
  }
  const record: RecoveryRecord = {
    id: newId(), entityType: draft.entityType, entityId: draft.entityId, reason,
    base: structuredClone(draft.base), local: structuredClone(draft), remote: adoptRemote(null, remote),
    resolved: false, createdAt: new Date().toISOString(),
  }
  state.recoveryRecords.push(record)
  return record
}

/**
 * Adopt the authoritative remote knowledge for one entity on every unresolved
 * record of that entity; `null` means a complete snapshot proved it absent.
 * Records about the same entity can therefore never disagree.
 */
export function confirmRemote(state: WorkspaceState, entityType: EntityType, entityId: string, remote: ServerObject | null) {
  for (const record of state.recoveryRecords) {
    if (record.resolved || record.entityType !== entityType || record.entityId !== entityId) continue
    record.remote = remote ? adoptRemote(record.remote, remote) : null
  }
}

export function operationIsActive(state: WorkspaceState, op: FrozenOperation): boolean {
  return op.state !== 'acknowledged' && op.intentIds.some(id => state.intents.some(intent => intent.id === id && intent.state !== 'done'))
}

/** Freeze exactly one complete immutable command, only against a confirmed base. */
export function freezeNext(state: WorkspaceState): FrozenOperation | undefined {
  for (const intent of state.intents) {
    if (intent.state !== 'pending') continue
    const key = entityKey(intent.entityType, intent.entityId)
    const draft = state.drafts[key]
    if (!draft || draft.blocked) continue
    if (state.operations.some(op => operationIsActive(state, op) && op.wire.entity_type === intent.entityType && op.wire.entity_id === intent.entityId)) continue
    // Do not jump a predecessor, including a blocked lifecycle action.
    if (state.intents.some(previous => previous !== intent && previous.entityType === intent.entityType && previous.entityId === intent.entityId && previous.state !== 'done' && state.intents.indexOf(previous) < state.intents.indexOf(intent))) continue
    const parent = intent.payload.parent_id ?? intent.payload.folder_id
    if (typeof parent === 'string') {
      const parentDraft = state.drafts[entityKey('folder', parent)]
      const parentRemote = state.serverObjects[entityKey('folder', parent)]
      const confirmed = parentDraft?.base ?? parentRemote
      if (!confirmed || isDeleted(confirmed) || parentDraft?.deleted) continue
    }
    const base = draft.base
    if (intent.kind !== 'create') {
      if (!base || isDeleted(base)) continue
      const remote = state.serverObjects[key]
      if (remote && compareDecimal(remote.rev, base.rev) > 0) {
        draft.blocked = 'remote_changed'
        preserve(state, draft, 'remote_changed', remote)
        continue
      }
    }
    const id = newId()
    const operation: FrozenOperation = {
      id, wire: {
        op_id: id, entity_type: intent.entityType, entity_id: intent.entityId, op: intent.kind,
        ...(intent.kind === 'create' ? {} : { base_rev: base!.rev }), payload: structuredClone(intent.payload),
      }, intentIds: [intent.id], coveredVersion: intent.draftVersion, state: 'pending', attempts: 0,
    }
    intent.state = 'frozen'
    intent.operationId = id
    state.operations.push(operation)
    return operation
  }
}
