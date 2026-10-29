import { createSyncTransport } from '../api/sync'
import { ApiError, apiClient } from '../api/client'
import { SyncEngine } from '../sync/engine'
import { entityKey, newId, partitionKey, type Partition } from '../domain/ids'
import type { WorkspacePort } from './ports'
import type { ClientRuntime } from './runtime'
import { flushNoteWrites } from './useNoteDraft'
import { ProfileHiddenError } from './identityVisibility'
import { downloadLocalArchive } from './exportLocal'

const cachedPorts = new WeakMap<ClientRuntime, Map<string, WorkspacePort>>()
const portFences = new WeakMap<WorkspacePort, () => void>()
let bindingGeneration = 0
const activeSync = new WeakMap<WorkspacePort, Promise<void>>()
/** Lets quiescing end a run's retry schedule; see quiesceWorkspacePorts. */
const backoffControl = new WeakMap<WorkspacePort, { stopped: boolean; wake?: () => void }>()

/** Internal: quiescing ended the retry schedule, not a synchronization result. */
class SyncStopped extends Error { constructor() { super('Synchronization stopped for quiescing'); this.name = 'SyncStopped' } }

/**
 * Quiescing has two obligations, and they must fail differently.
 *
 * A synchronization that ended in an error is already recorded in durable state
 * (`status`, `error`, `failures`, frozen commands), which is the engine's own place
 * to report it. Re-throwing it from the exit path made logout impossible whenever
 * the network was unreachable, and contradicted the UI's own promise that a
 * definite auth failure never clears the note queue. So a failed run is awaited and
 * its outcome discarded here.
 *
 * Uncommitted editor input is the opposite: losing it is real data loss, so it
 * still blocks a handoff and a logout (see flushNoteWrites). That is approved
 * behaviour and is never relaxed.
 *
 * Waiting also has to mean *stopping*. The engine retries a temporary failure with
 * exponential backoff (1s, 2s, 4s, 8s, about fifteen seconds before it pauses), so
 * a plain await would keep local content open for that whole schedule — the
 * observed E2E timeout — without learning anything new from the retries. Quiescing
 * therefore ends the backoff first: the run unwinds through its normal failure
 * accounting, which has already persisted the failure, and any request in flight is
 * still awaited.
 */
export async function quiesceWorkspacePorts(runtime: ClientRuntime): Promise<void> {
  const ports = [...(cachedPorts.get(runtime)?.values() ?? [])]
  await Promise.all(ports.map(port => flushNoteWrites(port)))
  await Promise.all(ports.map(port => settleSync(port)))
}

/** Wait for every synchronization already started; the outcome is not ours. */
async function settleSync(port: WorkspacePort): Promise<void> {
  const control = backoffControl.get(port)
  if (control && !control.stopped) {
    // End the retry schedule now, after the run's own failure accounting.
    control.stopped = true
    const wake = control.wake
    control.wake = undefined
    wake?.()
  }
  try {
    for (let task = activeSync.get(port); task;) {
      // Both outcomes mean "this pass is over". A rejection was already persisted
      // by the engine, so the exit path must not inherit it.
      await task.then(() => undefined, () => undefined)
      const current = activeSync.get(port)
      // A different entry is a newer pass that started while we waited; await it
      // too. A settled run removes its own entry (see track).
      task = current === task ? undefined : current
    }
  } finally {
    // A later sync (retry after re-login, takeover) retries normally again.
    if (control) control.stopped = false
  }
}

export function createWorkspacePort(runtime: ClientRuntime, partition: Partition): WorkspacePort {
  const ports = cachedPorts.get(runtime) ?? new Map<string, WorkspacePort>()
  cachedPorts.set(runtime, ports)
  const existing = ports.get(partitionKey(partition))
  if (existing) {
    try { portFences.get(existing)?.(); return existing }
    catch { ports.delete(partitionKey(partition)) }
  }
  const { repository, coordinator, session } = runtime
  const readonlyBinding = !coordinator.isWriter || session.snapshot.readonly
  const capturedProfile = session.snapshot.profile
  const capturedVisibility = runtime.visibility?.getSnapshot().generation
  const assertDisplay = () => {
    const current = session.snapshot.profile
    const visibility = runtime.visibility?.getSnapshot()
    if (!current || current.userId !== partition.userId || !current.active || current.locked || visibility?.checking || visibility?.failed
      || (readonlyBinding && (current !== capturedProfile || visibility?.generation !== capturedVisibility))) throw new ProfileHiddenError()
  }
  const assertStoredVisibility = async () => {
    assertDisplay()
    const profiles = await repository.listProfiles()
    assertDisplay()
    if (!profiles.some(profile => profile.deployment === partition.deployment && profile.userId === partition.userId && profile.active && !profile.locked)) throw new ProfileHiddenError()
  }
  const identityLease = coordinator.isWriter && !session.snapshot.readonly ? session.captureIdentityLease() : runtime.lease
  const lease = { generation: identityLease.generation, assertCurrent() {
    identityLease.assertCurrent()
    runtime.lease.assertCurrent()
    const profile = session.snapshot.profile
    if (!profile || profile.userId !== partition.userId || profile.locked || !profile.active) throw new Error('Identity changed')
  } }
  const transport = createSyncTransport(() => session.getAccessToken(), apiClient, lease)
  const request = async <T,>(send: (api: ReturnType<typeof createSyncTransport>) => Promise<T>): Promise<T> => {
    try { return await send(transport) }
    catch (error) {
      // Do not resend writes here. The engine retains the frozen command and
      // pauses; SessionController alone coordinates one refresh for the next
      // explicit retry. Check identity before invalidating a later login.
      if (error instanceof ApiError && error.status === 401) {
        lease.assertCurrent()
        session.invalidateAccessToken()
        await session.getAccessToken()
        lease.assertCurrent()
      }
      throw error
    }
  }
  // A stop signal unwinds the retry loop as soon as quiescing starts. The failure
  // itself is already in durable state from the loop's own accounting, so this only
  // cancels the remaining *retries*: it must not be recorded as a new failure.
  const control: { stopped: boolean; wake?: () => void } = { stopped: false }
  const backoff = (milliseconds: number) => {
    if (control.stopped) return Promise.reject(new SyncStopped())
    return new Promise<void>((resolve, reject) => {
      const settle = () => {
        clearTimeout(timer)
        if (control.wake === settle) control.wake = undefined
        if (control.stopped) reject(new SyncStopped())
        else resolve()
      }
      const timer = setTimeout(settle, milliseconds)
      control.wake = settle
    })
  }
  const engine = new SyncEngine(repository, partition, lease, {
    push: (workspace, operations) => request(api => api.push(workspace, operations)),
    pull: (workspace, cursor) => request(api => api.pull(workspace, cursor)),
    snapshot: workspace => request(api => api.snapshot(workspace)),
  }, { wait: backoff })
  const track = (task: Promise<void>) => {
    activeSync.set(port, task)
    // Never keep a settled promise here: a later quiesce must await work that is
    // actually in flight, not replay an old rejection.
    const forget = () => { if (activeSync.get(port) === task) activeSync.delete(port) }
    void task.then(forget, forget)
    return task
  }
  const schedule = () => { if (!runtime.writesPaused && coordinator.isWriter && session.snapshot.status === 'authenticated') void track(engine.sync()).catch(() => undefined) }
  const port: WorkspacePort = {
    partition, ownershipGeneration: ++bindingGeneration,
    get readOnly() { return !!runtime.writesPaused || !coordinator.isWriter || session.snapshot.readonly },
    read: async () => {
      await assertStoredVisibility()
      const state = await engine.read()
      await assertStoredVisibility()
      if (partitionKey(state.partition) !== partitionKey(partition)) throw new ProfileHiddenError()
      return state
    },
    exportLocal: async () => {
      const state = await port.read()
      assertDisplay()
      downloadLocalArchive(state)
    },
    subscribe: listener => { const local = repository.subscribe(listener); const crossTab = coordinator.subscribe(listener); return () => { local(); crossTab() } },
    save: async input => { await engine.save(input); schedule() },
    create: async (entityType, title, parentId) => { const entityId = newId(); await engine.create({ entityType, entityId, title, bodyMd: '', parentId }); schedule(); return entityId },
    move: async (entityType, entityId, parentId) => { await engine.move({ entityType, entityId, parentId }); schedule() },
    remove: async (entityType, entityId) => { await engine.delete({ entityType, entityId }); schedule() },
    preserveSource: async (entityId, title, bodyMd) => {
      await repository.mutate(partition, lease, state => {
        const current = state.drafts[entityKey('note', entityId)]
        if (!current || current.title !== title || current.bodyMd !== bodyMd) throw new Error('Source changed before recovery point')
        state.recoveryRecords.push({ id: newId(), entityType: 'note', entityId, reason: 'before_rich_conversion', base: structuredClone(current.base), local: structuredClone(current), remote: structuredClone(state.serverObjects[current.key] ?? null), resolved: true, createdAt: new Date().toISOString() })
      })
    },
    resolve: async (record, action) => { await engine.resolve({ entityType: record.entityType, entityId: record.entityId, action }); schedule() },
    retry: () => runtime.writesPaused ? Promise.resolve() : track(engine.retry()),
    synchronize: () => runtime.writesPaused ? Promise.resolve() : track(engine.sync()),
  }
  ports.set(partitionKey(partition), port)
  backoffControl.set(port, control)
  portFences.set(port, () => { assertDisplay(); if (!readonlyBinding) lease.assertCurrent() })
  return port
}
