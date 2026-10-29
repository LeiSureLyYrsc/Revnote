import type { MeResponse, OperationKind, ServerObject, WireOperation, PushResult } from '../api/contracts'
import type { Decimal } from './decimal'
import type { EntityType, Partition } from './ids'

/** A captured lease is checked again at every local commit and late callback. */
export interface WriteLease { generation: number; assertCurrent(): void }

export interface Profile {
  key: string
  deployment: string
  userId: string
  email: string
  active: boolean
  locked: boolean
  workspaces: MeResponse['workspaces']
}
export interface Credential {
  key: string
  userId: string
  generation: number
  remember: boolean
  refreshToken?: string
  refreshInflight: boolean
  sessionId?: string
}
export interface Draft {
  key: string
  entityType: EntityType
  entityId: string
  version: Decimal
  acknowledgedVersion: Decimal
  title: string
  bodyMd: string
  parentId: string | null
  base: ServerObject | null
  dirty: boolean
  deleted: boolean
  blocked?: string
}
export interface Intent {
  id: string
  entityType: EntityType
  entityId: string
  kind: OperationKind
  draftVersion: Decimal
  /** Lifecycle payload at intent creation; later edits never replace creates. */
  payload: Record<string, string | null>
  state: 'pending' | 'frozen' | 'done' | 'blocked'
  operationId?: string
}
export interface FrozenOperation {
  id: string
  wire: WireOperation
  intentIds: string[]
  coveredVersion: Decimal
  state: 'pending' | 'sending' | 'uncertain' | 'acknowledged' | 'conflict' | 'rejected'
  attempts: number
  result?: PushResult
  error?: string
}
export interface RecoveryRecord {
  id: string
  entityType: EntityType
  entityId: string
  reason: string
  base: ServerObject | null
  local: Draft | null
  remote: ServerObject | null
  resolved: boolean
  createdAt: string
}
export interface WorkspaceState {
  partition: Partition
  generation: string
  cursor: Decimal
  bootstrapped: boolean
  status: 'idle' | 'syncing' | 'offline' | 'paused' | 'rebuilding'
  error?: string
  failures: number
  serverObjects: Record<string, ServerObject>
  drafts: Record<string, Draft>
  intents: Intent[]
  operations: FrozenOperation[]
  recoveryRecords: RecoveryRecord[]
}

/** The mutator is synchronous: no HTTP or external awaits inside IDB tx. */
export interface LocalRepository {
  read(partition: Partition): Promise<WorkspaceState>
  mutate<T>(partition: Partition, lease: WriteLease, change: (state: WorkspaceState) => T): Promise<T>
  subscribe(listener: () => void): () => void
  listProfiles(): Promise<Profile[]>
  putProfile(profile: Profile, lease: WriteLease): Promise<void>
  getCredential(key: string): Promise<Credential | undefined>
  putCredential(credential: Credential, lease: WriteLease): Promise<void>
  deleteCredential(key: string, lease: WriteLease): Promise<void>
}

export interface DraftInput {
  entityType: EntityType
  entityId: string
  title: string
  bodyMd: string
  parentId: string | null
}

export function emptyWorkspace(partition: Partition): WorkspaceState {
  return {
    partition, generation: '0', cursor: '0', bootstrapped: false,
    status: 'idle', failures: 0, serverObjects: {}, drafts: {}, intents: [],
    operations: [], recoveryRecords: [],
  }
}
