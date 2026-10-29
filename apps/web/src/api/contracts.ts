import type { Decimal } from '../domain/decimal'
import type { EntityType } from '../domain/ids'

export interface ApiErrorBody {
  code: string
  message: string
  request_id: string
  retryable: boolean
  details: Record<string, unknown>
}

export interface User { id: string; email: string; created_at: string }
export interface Workspace { id: string; role: string }
export interface MeResponse { user: User; workspaces: Workspace[] }
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: 'Bearer'
  expires_in: number
  session_id?: string
  session_expires_at?: string
}

export interface ServerObject {
  entity_type: EntityType
  id: string
  workspace_id: string
  rev: Decimal
  updated_seq: Decimal
  created_at: string
  updated_at: string
  deleted_at: string | null
  deleted?: boolean
  name?: string
  parent_id?: string | null
  title?: string
  body_md?: string
  folder_id?: string | null
}

export type OperationKind = 'create' | 'update' | 'move' | 'delete'
export interface WireOperation {
  op_id: string
  entity_type: EntityType
  entity_id: string
  op: OperationKind
  base_rev?: Decimal | null
  payload: Record<string, string | null>
}
export type PushStatus = 'applied' | 'replayed' | 'conflict' | 'rejected' | 'unknown' | 'not_attempted'
export interface PushResult {
  op_id: string
  status: PushStatus
  seq?: Decimal | null
  rev?: Decimal | null
  error?: ApiErrorBody | null
  [key: string]: unknown
}
export interface PushResponse { results: PushResult[] }
export interface SyncEvent {
  seq: Decimal
  entity_type: EntityType
  entity_id: string
  op: OperationKind
  rev: Decimal
  op_id: string
  created_at: string
}
export interface PullResponse {
  events: SyncEvent[]
  objects: ServerObject[]
  next_cursor: Decimal
  has_more: boolean
  last_seq: Decimal
  min_retained_seq: Decimal
}
export interface SnapshotResponse {
  as_of_seq: Decimal
  min_retained_seq: Decimal
  objects: ServerObject[]
}
