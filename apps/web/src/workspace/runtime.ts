import { SessionController } from '../auth/session'
import { createRepository } from '../storage/repository'
import { createCoordinator } from '../sync/coordinator'
import type { LocalRepository, WriteLease } from '../domain/contracts'
import type { WriteCoordinator } from '../sync/coordinator'
import { quiesceWorkspacePorts } from './createWorkspacePort'
import { createIdentityVisibility, type IdentityVisibility } from './identityVisibility'

export interface ClientRuntime { repository: LocalRepository; coordinator: WriteCoordinator; session: SessionController; lease: WriteLease; writesPaused?: boolean; visibility?: IdentityVisibility }

/**
 * Handing off must not depend on the server being reachable. Pausing local writes
 * and suspending the identity fence the engine lease, so a synchronization that
 * would otherwise keep retrying ends at its next attempt instead of running out the
 * full backoff while this tab still shows content. Quiescing then waits for those
 * runs and deliberately ignores their outcome: a failed sync is durable state, not
 * a reason to keep the lock. Uncommitted editor input still blocks the handoff.
 */
export async function releaseRuntime(runtime: ClientRuntime): Promise<void> {
  runtime.writesPaused = true
  try {
    await quiesceWorkspacePorts(runtime)
    runtime.session.suspend()
    await runtime.coordinator.release()
  } catch (error) { runtime.writesPaused = false; throw error }
}

export async function activateRuntime(repository: LocalRepository, coordinator: WriteCoordinator): Promise<ClientRuntime> {
  const lease = await coordinator.acquire() ?? { generation: -1, assertCurrent() { throw new Error('This tab is readonly') } }
  const session = new SessionController({ repository, lease })
  await session.restore()
  const runtime: ClientRuntime = { repository, coordinator, session, lease }
  runtime.visibility = createIdentityVisibility(runtime)
  return runtime
}

let initial: Promise<ClientRuntime> | undefined
export function initializeRuntime(): Promise<ClientRuntime> {
  initial ??= (async () => {
    const repository = await createRepository()
    const coordinator = createCoordinator()
    repository.subscribe(() => coordinator.notify())
    return activateRuntime(repository, coordinator)
  })()
  return initial
}
