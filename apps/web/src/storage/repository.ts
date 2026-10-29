import type { IDBPDatabase } from 'idb'
import { emptyWorkspace, type Credential, type LocalRepository, type Profile, type WorkspaceState, type WriteLease } from '../domain/contracts'
import { decimal } from '../domain/decimal'
import { entityKey, partitionKey, type Partition } from '../domain/ids'
import { openDatabase, StorageError, storageError } from './database'
import { recoveryExport, type RecoveryExport } from './recovery'
import { FACT_STORES, type FactRecord, type FactStore, type LocalSchema } from './schema'

const WORKSPACE_STORES = ['workspaces', ...FACT_STORES] as const

function assertLease(lease: WriteLease): void {
  try { lease.assertCurrent() } catch (cause) {
    throw new StorageError('lease', 'This tab no longer owns write access. Changes were not saved.', { cause })
  }
}

function invalid(message: string): never { throw new StorageError('invalid_mutation', message) }

function validatePartition(partition: Partition): void {
  if ([partition.deployment, partition.userId, partition.workspaceId].some(value => typeof value !== 'string' || value.length === 0)) invalid('Deployment, user and workspace identity are required.')
}

function validateEntity(type: string, id: string): void {
  if (!['note', 'folder'].includes(type) || typeof id !== 'string' || !id) invalid('Entity type and identity are required.')
}

function canonical(value: unknown): string {
  if (value === undefined) return 'undefined'
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`
}

function validate(before: WorkspaceState, after: WorkspaceState): void {
  validatePartition(after.partition)
  if (partitionKey(before.partition) !== partitionKey(after.partition)) invalid('A mutation cannot move data between identity partitions.')
  decimal(after.cursor)
  const oldOperations = new Map(before.operations.map(op => [op.id, op]))
  const newOperations = new Map(after.operations.map(op => [op.id, op]))
  if (newOperations.size !== after.operations.length) invalid('Duplicate frozen operation identity.')
  for (const [id, old] of oldOperations) {
    const next = newOperations.get(id)
    if (!next || canonical(old.wire) !== canonical(next.wire) || old.coveredVersion !== next.coveredVersion || canonical(old.intentIds) !== canonical(next.intentIds)) {
      invalid('A frozen command and its covered versions are immutable; retain the original command for recovery.')
    }
  }
  const unresolved = new Set<string>()
  for (const op of after.operations) {
    validateEntity(op.wire.entity_type, op.wire.entity_id)
    if (op.id !== op.wire.op_id) invalid('Operation identity must match its frozen wire identity.')
    decimal(op.coveredVersion)
    if (op.wire.base_rev !== undefined && op.wire.base_rev !== null) decimal(op.wire.base_rev)
    if (['pending', 'sending', 'uncertain'].includes(op.state)) {
      const key = entityKey(op.wire.entity_type, op.wire.entity_id)
      if (unresolved.has(key)) invalid('An entity may have only one unresolved frozen command.')
      unresolved.add(key)
    }
  }
  for (const [key, draft] of Object.entries(after.drafts)) {
    validateEntity(draft.entityType, draft.entityId)
    if (key !== entityKey(draft.entityType, draft.entityId) || draft.key !== key) invalid('Draft keys must include entity type and identity.')
    decimal(draft.version)
    decimal(draft.acknowledgedVersion)
  }
  for (const [key, object] of Object.entries(after.serverObjects)) {
    validateEntity(object.entity_type, object.id)
    if (key !== entityKey(object.entity_type, object.id) || object.workspace_id !== after.partition.workspaceId) invalid('Remote object is outside its entity/workspace partition.')
    decimal(object.rev)
    decimal(object.updated_seq)
  }
  for (const intent of after.intents) {
    validateEntity(intent.entityType, intent.entityId)
    decimal(intent.draftVersion)
  }
  if (new Set(after.intents.map(intent => intent.id)).size !== after.intents.length) invalid('Duplicate intent identity.')
  for (const record of after.recoveryRecords) validateEntity(record.entityType, record.entityId)
  if (new Set(after.recoveryRecords.map(record => record.id)).size !== after.recoveryRecords.length) invalid('Duplicate recovery identity.')
}

function projections(state: WorkspaceState): Record<FactStore, Map<string, FactRecord>> {
  const partition = partitionKey(state.partition)
  const entries = (values: [string, unknown][]) => new Map(values.map(([id, value]) => {
    const key = JSON.stringify([partition, id])
    return [key, { key, partition, value }] as const
  }))
  return {
    server_objects: entries(Object.entries(state.serverObjects)),
    drafts: entries(Object.entries(state.drafts)),
    intents: entries(state.intents.map(value => [JSON.stringify([value.entityType, value.entityId, value.id]), value])),
    operations: entries(state.operations.map(value => [JSON.stringify([value.wire.entity_type, value.wire.entity_id, value.id]), value])),
    recovery_records: entries(state.recoveryRecords.map(value => [JSON.stringify([value.entityType, value.entityId, value.id]), value])),
  }
}

export class IndexedDbRepository implements LocalRepository {
  private readonly listeners = new Set<() => void>()
  private lifecycleError?: StorageError

  constructor(private readonly db: IDBPDatabase<LocalSchema>) {}

  /** Used by openDatabase lifecycle handlers, not as a data-recovery shortcut. */
  invalidate(error: StorageError): void { this.lifecycleError = error }
  private assertOpen(): void { if (this.lifecycleError) throw this.lifecycleError }
  private notify(): void {
    // A view observer must not turn a committed transaction into a save failure.
    for (const listener of this.listeners) { try { listener() } catch { /* observers are isolated */ } }
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  async read(partition: Partition): Promise<WorkspaceState> {
    try {
      this.assertOpen()
      validatePartition(partition)
      return structuredClone(await this.db.get('workspaces', partitionKey(partition)) ?? emptyWorkspace(partition))
    } catch (error) { throw storageError(error) }
  }

  async mutate<T>(partition: Partition, lease: WriteLease, change: (state: WorkspaceState) => T): Promise<T> {
    this.assertOpen()
    validatePartition(partition)
    assertLease(lease)
    const tx = this.db.transaction(WORKSPACE_STORES, 'readwrite')
    // Attach an abort handler immediately, including for synchronous mutator errors.
    const done = tx.done
    void done.catch(() => {})
    try {
      assertLease(lease)
      const key = partitionKey(partition)
      const before = await tx.objectStore('workspaces').get(key) ?? emptyWorkspace(structuredClone(partition))
      this.assertOpen()
      assertLease(lease)
      const next = structuredClone(before)
      const result = change(next)
      if (result !== null && (typeof result === 'object' || typeof result === 'function') && 'then' in result) {
        // Reject async callbacks without leaking a later rejection to the app.
        void Promise.resolve(result).catch(() => {})
        invalid('Repository mutators must be synchronous; external awaits are forbidden.')
      }
      validate(before, next)
      assertLease(lease)
      const clonedResult = structuredClone(result)
      // Detach all callback-owned references before issuing IDB requests.
      const saved = structuredClone(next)
      const previousFacts = projections(before)
      const nextFacts = projections(saved)
      const writes: Promise<unknown>[] = []
      const issue = (request: Promise<unknown>) => {
        // A subsequent request may throw synchronously (e.g. quota/clone).
        // Earlier queued requests then abort and must already have handlers.
        void request.catch(() => {})
        writes.push(request)
      }
      issue(tx.objectStore('workspaces').put(saved, key))
      for (const store of FACT_STORES) {
        for (const oldKey of previousFacts[store].keys()) {
          if (!nextFacts[store].has(oldKey)) issue(tx.objectStore(store).delete(oldKey))
        }
        for (const [factKey, record] of nextFacts[store]) {
          if (canonical(previousFacts[store].get(factKey)) !== canonical(record)) issue(tx.objectStore(store).put(record))
        }
      }
      await Promise.all(writes)
      this.assertOpen()
      assertLease(lease)
      await done
      assertLease(lease)
      this.notify()
      return clonedResult
    } catch (error) {
      try { tx.abort() } catch { /* already aborted or committed */ }
      await done.catch(() => {})
      throw storageError(error)
    }
  }

  async listProfiles(): Promise<Profile[]> {
    try { this.assertOpen(); return structuredClone(await this.db.getAll('profiles')) } catch (error) { throw storageError(error) }
  }

  async getCredential(key: string): Promise<Credential | undefined> {
    try { this.assertOpen(); return structuredClone(await this.db.get('credentials', key)) } catch (error) { throw storageError(error) }
  }

  private async identityWrite(store: 'profiles' | 'credentials', value: Profile | Credential | string, lease: WriteLease): Promise<void> {
    this.assertOpen()
    assertLease(lease)
    const tx = this.db.transaction(['profiles', 'credentials'], 'readwrite')
    const done = tx.done
    void done.catch(() => {})
    try {
      assertLease(lease)
      if (typeof value === 'string') await tx.objectStore('credentials').delete(value)
      else if (store === 'profiles') await tx.objectStore('profiles').put(structuredClone(value as Profile))
      else await tx.objectStore('credentials').put(structuredClone(value as Credential))
      this.assertOpen()
      assertLease(lease)
      await done
      assertLease(lease)
      this.notify()
    } catch (error) {
      try { tx.abort() } catch { /* already finished */ }
      await done.catch(() => {})
      throw storageError(error)
    }
  }

  putProfile(profile: Profile, lease: WriteLease): Promise<void> { return this.identityWrite('profiles', profile, lease) }
  putCredential(credential: Credential, lease: WriteLease): Promise<void> {
    // Remember=false never persists a refresh token, even for an incorrect caller.
    const saved = structuredClone(credential)
    if (!saved.remember) delete saved.refreshToken
    return this.identityWrite('credentials', saved, lease)
  }
  deleteCredential(key: string, lease: WriteLease): Promise<void> { return this.identityWrite('credentials', key, lease) }

  async exportRecovery(partition?: Partition): Promise<RecoveryExport> {
    try {
      this.assertOpen()
      const states = partition ? [await this.read(partition)] : await this.db.getAll('workspaces')
      return recoveryExport(states)
    } catch (error) { throw storageError(error) }
  }

  close(): void {
    this.db.close()
    this.invalidate(new StorageError('unavailable', 'Local database is closed. Reopen before writing.'))
    this.listeners.clear()
  }
}

export async function createRepository(databaseName?: string): Promise<IndexedDbRepository> {
  const holder: { repository?: IndexedDbRepository } = {}
  let lifecycleError: StorageError | undefined
  const db = await openDatabase(databaseName, { onLifecycleError(error) {
    lifecycleError = error
    holder.repository?.invalidate(error)
  } })
  const repository = new IndexedDbRepository(db)
  holder.repository = repository
  if (lifecycleError) repository.invalidate(lifecycleError)
  return repository
}
