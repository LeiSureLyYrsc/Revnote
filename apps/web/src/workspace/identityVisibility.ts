import type { ClientRuntime } from './runtime'

export class ProfileHiddenError extends Error {
  constructor() { super('Workspace identity is no longer visible'); this.name = 'ProfileHiddenError' }
}

export interface VisibilitySnapshot { checking: boolean; failed: boolean; generation: number }
export function createIdentityVisibility(runtime: ClientRuntime) {
  let snapshot: VisibilitySnapshot = { checking: false, failed: false, generation: 0 }
  const listeners = new Set<() => void>()
  let ticket = 0
  let disposed = false
  const publish = (change: Partial<VisibilitySnapshot>) => {
    snapshot = { ...snapshot, ...change }
    for (const listener of listeners) listener()
  }
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    watch() {
      disposed = false
      const unsubscribe = runtime.coordinator.subscribe(() => {
        // Writer mutations must not restore/rotate the writer's own session.
        if (runtime.coordinator.isWriter) return
        const captured = ++ticket
        // Hide cached content before starting any asynchronous profile read.
        publish({ checking: true, failed: false, generation: snapshot.generation + 1 })
        void runtime.session.restore().then(() => {
          if (!disposed && captured === ticket) publish({ checking: false })
        }, () => {
          if (!disposed && captured === ticket) publish({ checking: false, failed: true })
        })
      })
      return () => { disposed = true; ++ticket; unsubscribe() }
    },
  }
}

export type IdentityVisibility = ReturnType<typeof createIdentityVisibility>
