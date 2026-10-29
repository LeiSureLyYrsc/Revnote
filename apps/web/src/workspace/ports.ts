import type { DraftInput, RecoveryRecord, WorkspaceState } from '../domain/contracts'
import type { EntityType, Partition } from '../domain/ids'
import type { ResolutionAction } from '../components/conflicts/ConflictPanel'

/** UI has no authority to manufacture ACKs, queue commands, or rotate sessions. */
export interface WorkspacePort {
  partition: Partition
  ownershipGeneration: number
  readOnly: boolean
  read(): Promise<WorkspaceState>
  exportLocal(): Promise<void>
  subscribe(listener: () => void): () => void
  save(input: DraftInput): Promise<void>
  create(type: EntityType, title: string, parentId: string | null): Promise<string>
  move(type: EntityType, id: string, parentId: string | null): Promise<void>
  remove(type: EntityType, id: string): Promise<void>
  preserveSource(id: string, title: string, bodyMd: string): Promise<void>
  resolve(record: RecoveryRecord, action: ResolutionAction): Promise<void>
  retry(): Promise<void>
  synchronize?(): Promise<void>
}
