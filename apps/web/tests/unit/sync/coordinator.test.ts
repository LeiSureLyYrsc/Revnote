import { describe, expect, it, vi } from 'vitest'
import { createCoordinator, type InvalidationChannel } from '../../../src/sync/coordinator'

function manager(): LockManager {
  let held = false
  return {
    request: vi.fn(async (_name: string, _options: unknown, callback: (lock: Lock | null) => Promise<void>) => {
      if (held) return callback(null)
      held = true
      try { return await callback({ name: 'revnote-writer-v1', mode: 'exclusive' }) }
      finally { held = false }
    }),
  } as unknown as LockManager
}

describe('origin single writer coordinator', () => {
  it('is read-only without Web Locks, with no storage or broadcast fallback', async () => {
    const coordinator = createCoordinator({ locks: null, channel: null })
    expect(coordinator.supported).toBe(false)
    expect(await coordinator.acquire()).toBeNull()
    expect(coordinator.isWriter).toBe(false)
    await coordinator.close()
  })

  it('holds one origin-wide writer and fences late callbacks before releasing', async () => {
    const locks = manager()
    const first = createCoordinator({ locks, channel: null })
    const second = createCoordinator({ locks, channel: null })
    const lease = (await first.acquire())!
    expect(first.isWriter).toBe(true)
    lease.assertCurrent()
    expect(await second.acquire()).toBeNull()
    const releasing = first.release()
    expect(() => lease.assertCurrent()).toThrow('no longer current')
    expect(first.isWriter).toBe(false)
    await releasing
    const successor = (await second.acquire())!
    successor.assertCurrent()
    await second.release()
    const regained = (await first.acquire())!
    expect(regained.generation).toBeGreaterThan(lease.generation)
    expect(() => lease.assertCurrent()).toThrow()
    await first.close()
  })

  it('does not acquire twice for simultaneous callers and closes ownership', async () => {
    const locks = manager()
    const coordinator = createCoordinator({ locks, channel: null })
    const [one, two] = await Promise.all([coordinator.acquire(), coordinator.acquire()])
    expect(one).toBe(two)
    expect(locks.request).toHaveBeenCalledTimes(1)
    await coordinator.close()
    expect(() => one!.assertCurrent()).toThrow()
    expect(await coordinator.acquire()).toBeNull()
  })

  it('treats BroadcastChannel messages only as invalidation, never write authority', async () => {
    let listener: EventListener | undefined
    const channel: InvalidationChannel = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((_type, callback) => { listener = callback }),
      removeEventListener: vi.fn(), close: vi.fn(),
    }
    const coordinator = createCoordinator({ locks: null, channel })
    const changed = vi.fn()
    coordinator.subscribe(changed)
    listener!(new Event('message'))
    expect(changed).toHaveBeenCalledTimes(1)
    expect(coordinator.isWriter).toBe(false)
    coordinator.notify()
    expect(channel.postMessage).toHaveBeenCalledWith({ type: 'invalidate' })
    expect(coordinator.isWriter).toBe(false)
    await coordinator.close()
    expect(channel.removeEventListener).toHaveBeenCalled()
  })
})
