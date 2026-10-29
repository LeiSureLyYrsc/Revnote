import type { WriteLease } from '../domain/contracts'

export interface InvalidationChannel {
  postMessage(message: unknown): void
  addEventListener(type: 'message', listener: EventListener): void
  removeEventListener(type: 'message', listener: EventListener): void
  close?(): void
}

export interface CoordinatorOptions {
  locks?: LockManager | null
  channel?: InvalidationChannel | null
}

/** The lock is origin-wide, including credentials, drafts and all workspaces. */
export function createCoordinator(options: CoordinatorOptions = {}) {
  const locks = options.locks === undefined ? globalThis.navigator?.locks : options.locks
  const channel = options.channel === undefined
    ? (typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined'
      ? new BroadcastChannel('revnote-invalidation-v1') : null)
    : options.channel
  const listeners = new Set<() => void>()
  let generation = 0
  let active: WriteLease | null = null
  let unlock: (() => void) | undefined
  let lockTask: Promise<unknown> | undefined
  let acquisition: Promise<WriteLease | null> | undefined
  let closed = false
  const emit = () => {
    for (const listener of listeners) { try { listener() } catch { /* Invalidation observers cannot change lock authority. */ } }
  }
  const invalidated: EventListener = () => emit()
  channel?.addEventListener('message', invalidated)

  const coordinator = {
    get supported() { return Boolean(locks) },
    get isWriter() { return active !== null },
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    notify() {
      try { channel?.postMessage({ type: 'invalidate' }) } catch { /* Broadcast is optional, never authority. */ }
      emit()
    },
    acquire(): Promise<WriteLease | null> {
      if (closed || !locks) return Promise.resolve(null)
      if (active) return Promise.resolve(active)
      if (acquisition) return acquisition
      const requestedGeneration = ++generation
      acquisition = new Promise<WriteLease | null>((resolve, reject) => {
        lockTask = locks.request('revnote-writer-v1', { mode: 'exclusive', ifAvailable: true }, async lock => {
          if (!lock || closed || generation !== requestedGeneration) { resolve(null); return }
          const held = new Promise<void>(done => { unlock = done })
          const lease: WriteLease = {
            generation: requestedGeneration,
            assertCurrent() {
              if (closed || active !== lease || generation !== requestedGeneration) {
                throw new Error('Write ownership is no longer current')
              }
            },
          }
          active = lease
          resolve(lease)
          coordinator.notify()
          await held
        }).catch(error => {
          if (generation === requestedGeneration) { ++generation; active = null; unlock = undefined; emit() }
          reject(error)
        })
      }).finally(() => { acquisition = undefined })
      return acquisition
    },
    async release() {
      // Fence callbacks synchronously, before relinquishing the actual browser lock.
      ++generation
      active = null
      unlock?.()
      unlock = undefined
      await lockTask
      coordinator.notify()
    },
    async close() {
      closed = true
      await coordinator.release()
      channel?.removeEventListener('message', invalidated)
      channel?.close?.()
      listeners.clear()
    },
  }
  return coordinator
}

export type WriteCoordinator = ReturnType<typeof createCoordinator>
