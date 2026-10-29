import { ApiError } from '../api/client'
import type { SyncTransport } from '../api/sync'
import type { DraftInput, FrozenOperation, LocalRepository, WorkspaceState, WriteLease } from '../domain/contracts'
import { incrementDecimal } from '../domain/decimal'
import { entityKey, newId, type Partition } from '../domain/ids'
import { appendIntent, createLocal, deleteLocal, freezeNext, getDraft, isDeleted, moveLocal, objectDraft, operationIsActive, preserve, receipt, saveLocal, type EntityInput, type MoveInput } from './operations'
import { activateSnapshot, applyPull } from './reconcile'
import { applyPushResult, IndeterminateResult } from './reducer'

export interface ResolveInput extends EntityInput { action: 'remote' | 'local' | 'copy' }
export interface EngineOptions { wait?: (milliseconds: number) => Promise<void> }

// Identity leases may be separately captured wrappers around the same Web Lock.
// Serialize every engine in this tab; the Web Lock excludes all other tabs.
let sender: Promise<void> = Promise.resolve()

/** All writes use short repository transactions, all HTTP is outside them. */
export class SyncEngine {
  private running?: Promise<void>
  private readonly wait: (milliseconds: number) => Promise<void>

  constructor(
    private readonly repo: LocalRepository,
    private readonly partition: Partition,
    private readonly lease: WriteLease,
    private readonly transport: SyncTransport,
    options: EngineOptions = {},
  ) { this.wait = options.wait ?? (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))) }

  subscribe(listener: () => void) { return this.repo.subscribe(listener) }
  read() { return this.repo.read(this.partition) }
  private mutate<T>(change: (state: WorkspaceState) => T) {
    this.lease.assertCurrent()
    return this.repo.mutate(this.partition, this.lease, change)
  }
  create(input: DraftInput) { return this.mutate(state => createLocal(state, input)) }
  save(input: DraftInput) { return this.mutate(state => saveLocal(state, input)) }
  move(input: MoveInput) { return this.mutate(state => moveLocal(state, input)) }
  delete(input: EntityInput) { return this.mutate(state => deleteLocal(state, input)) }

  resolve(input: ResolveInput) {
    return this.mutate(state => {
      const draft = getDraft(state, input)
      const operations = state.operations.filter(op => operationIsActive(state, op) && op.wire.entity_type === input.entityType && op.wire.entity_id === input.entityId)
      if (operations.some(op => ['pending', 'sending', 'uncertain'].includes(op.state))) throw new Error('Confirm the original pending command before resolving')
      const remote = state.serverObjects[draft.key] ?? null
      if (!draft.blocked && !operations.length) throw new Error('No conflict to resolve')
      if (input.action === 'local' && (!remote || isDeleted(remote) || draft.blocked === 'remote_deleted')) throw new Error('Deleted entities require a new ID')
      preserve(state, draft, `manual_${input.action}`, remote)
      // An explicit action settles only determinate failed commands and their
      // successors. No old ID is ever resurrected by the copy action.
      for (const intent of state.intents) if (intent.entityType === input.entityType && intent.entityId === input.entityId) intent.state = 'done'
      for (const record of state.recoveryRecords) if (record.entityType === input.entityType && record.entityId === input.entityId) record.resolved = true
      if (input.action === 'copy') {
        const copied = createLocal(state, {
          entityType: 'note', entityId: newId(), title: draft.title, bodyMd: draft.bodyMd,
          parentId: draft.parentId && state.serverObjects[entityKey('folder', draft.parentId)] && !isDeleted(state.serverObjects[entityKey('folder', draft.parentId)]) ? draft.parentId : null,
        })
        if (remote) state.drafts[draft.key] = objectDraft(remote)
        else { draft.dirty = false; draft.deleted = true; draft.blocked = undefined }
        return copied
      }
      if (input.action === 'remote') {
        if (remote) state.drafts[draft.key] = objectDraft(remote)
        else { draft.dirty = false; draft.deleted = true; draft.blocked = undefined }
        return receipt(state.drafts[draft.key])
      }
      draft.base = structuredClone(remote)
      draft.blocked = undefined
      draft.version = incrementDecimal(draft.version)
      draft.dirty = true
      // Keep a confirmed delete or move intent as an explicit action, not a
      // content-only update. Re-submit content then position only when needed.
      if (draft.deleted) appendIntent(state, draft, 'delete')
      else {
        appendIntent(state, draft, 'update')
        if (draft.parentId !== (remote!.parent_id ?? remote!.folder_id ?? null)) appendIntent(state, draft, 'move')
      }
      return receipt(draft)
    })
  }

  async retry() {
    await this.mutate(state => {
      const rebuilding = state.status === 'rebuilding' || state.error?.startsWith('rebuild_required:')
      state.failures = 0; state.error = undefined; state.status = rebuilding ? 'rebuilding' : 'idle'
    })
    return this.sync()
  }

  sync(): Promise<void> {
    if (this.running) return this.running
    const scheduled = sender.catch(() => undefined).then(() => this.run())
    sender = scheduled
    this.running = scheduled.finally(() => {
      this.running = undefined
    })
    return this.running
  }

  private async send(operationId: string) {
    const operation = await this.mutate(state => {
      const op = state.operations.find(item => item.id === operationId)!
      op.state = 'sending'
      op.attempts += 1
      return structuredClone(op)
    })
    try {
      const response = await this.transport.push(this.partition.workspaceId, [structuredClone(operation.wire)])
      this.lease.assertCurrent()
      if (response.results.length !== 1) throw new IndeterminateResult()
      const indeterminate = await this.mutate(state => {
        const op = state.operations.find(item => item.id === operation.id)!
        try { applyPushResult(state, op, response.results[0]); return false }
        catch (error) { if (error instanceof IndeterminateResult) return true; throw error }
      })
      if (indeterminate) throw new IndeterminateResult()
    } catch (error) {
      this.lease.assertCurrent()
      await this.mutate(state => {
        const op = state.operations.find(item => item.id === operation.id)!
        if (op.state === 'sending') { op.state = 'uncertain'; op.error = error instanceof ApiError ? error.code : 'unconfirmed_response' }
      })
      throw error
    }
  }

  private async snapshot() {
    const before = await this.read()
    const response = await this.transport.snapshot(this.partition.workspaceId)
    this.lease.assertCurrent()
    await this.mutate(state => {
      if (activateSnapshot(state, response, before.generation)) { state.status = 'syncing'; state.error = undefined }
    })
  }

  private async pull() {
    for (let pages = 0; pages < 1000; pages++) {
      const before = await this.read()
      const page = await this.transport.pull(this.partition.workspaceId, before.cursor)
      this.lease.assertCurrent()
      const applied = await this.mutate(state => applyPull(state, page, before.cursor, before.generation))
      if (!applied) return
      if (!page.has_more) return
    }
    throw new ApiError('pull_page_budget', 0, false)
  }

  private async pass() {
    let state = await this.read()
    // On reload/takeover, settle every command that may already have reached the
    // server before any bootstrap/rebuild. A snapshot never proves execution.
    for (const op of state.operations.filter(op => operationIsActive(state, op) && ['sending', 'uncertain'].includes(op.state))) await this.send(op.id)
    state = await this.read()
    if (!state.bootstrapped || state.status === 'rebuilding') await this.snapshot()
    await this.mutate(current => { current.status = 'syncing' })
    for (let count = 0; count < 1000; count++) {
      const next = await this.mutate(current => {
        const existing = current.operations.find(op => operationIsActive(current, op) && ['pending', 'sending', 'uncertain'].includes(op.state))
        return structuredClone(existing ?? freezeNext(current)) as FrozenOperation | undefined
      })
      if (!next) break
      await this.send(next.id)
      if (count === 999) throw new ApiError('push_operation_budget', 0, false)
    }
    await this.pull()
    await this.mutate(current => {
      current.status = 'idle'; current.failures = 0; current.error = undefined
    })
  }

  private async run() {
    this.lease.assertCurrent()
    const initial = await this.read()
    if (initial.status === 'paused') return
    for (;;) {
      try { await this.pass(); return }
      catch (error) {
        this.lease.assertCurrent()
        const code = error instanceof ApiError ? error.code : error instanceof IndeterminateResult ? 'unconfirmed_response' : error instanceof Error && error.message === 'cursor_invalid' ? 'cursor_invalid' : 'sync_failed'
        const rebuild = code === 'cursor_invalid' || code === 'cursor_ahead' || (error instanceof ApiError && error.status === 410)
        const temporary = error instanceof IndeterminateResult || (error instanceof ApiError && (error.uncertain || error.retryable))
        const result = await this.mutate(state => {
          state.failures += 1
          const wasRebuilding = state.status === 'rebuilding' || state.error?.startsWith('rebuild_required:')
          state.error = rebuild || wasRebuilding ? `rebuild_required:${code}` : code
          // Store the rebuilding requirement separately in status; failures must
          // never accidentally fall through to an ordinary snapshot-free retry.
          if (rebuild) state.status = 'rebuilding'
          else if (state.status !== 'rebuilding') state.status = temporary ? 'offline' : 'paused'
          if (state.failures >= 5 || (!temporary && !rebuild)) state.status = 'paused'
          return { failures: state.failures, paused: state.status === 'paused' }
        })
        if (result.paused) return
        await this.wait(Math.min(1000 * 2 ** (result.failures - 1), 16_000))
      }
    }
  }
}
