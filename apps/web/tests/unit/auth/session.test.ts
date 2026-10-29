import { describe, expect, it, vi } from 'vitest'
import { SessionController, profileKey } from '../../../src/auth/session'
import type { AuthApi } from '../../../src/api/auth'
import type { Credential, LocalRepository, Profile, WriteLease } from '../../../src/domain/contracts'
import type { TokenPair } from '../../../src/api/contracts'
import { DEPLOYMENT } from '../../../src/domain/ids'

const id = '10000000-0000-4000-8000-000000000001'
const workspaceId = '20000000-0000-4000-8000-000000000001'
const me = { user: { id, email: 'person@example.com', created_at: '2026-01-01T00:00:00Z' }, workspaces: [{ id: workspaceId, role: 'owner' }] }
const pair: TokenPair = { access_token: 'secret_access', refresh_token: 'secret_refresh', token_type: 'Bearer', expires_in: 60, session_id: id, session_expires_at: '2026-11-01T00:00:00Z' }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function setup() {
  const profiles = new Map<string, Profile>()
  const credentials = new Map<string, Credential>()
  let writable = true
  let now = 0
  const lease: WriteLease = { generation: 1, assertCurrent: () => { if (!writable) throw new Error('readonly') } }
  const repository: LocalRepository = {
    read: vi.fn(), mutate: vi.fn(), subscribe: () => () => undefined,
    listProfiles: vi.fn(async () => [...profiles.values()].map(value => structuredClone(value))),
    putProfile: vi.fn(async (profile, guard) => { guard.assertCurrent(); profiles.set(profile.key, structuredClone(profile)) }),
    getCredential: vi.fn(async key => { const value = credentials.get(key); return value && structuredClone(value) }),
    putCredential: vi.fn(async (credential, guard) => { guard.assertCurrent(); credentials.set(credential.key, structuredClone(credential)) }),
    deleteCredential: vi.fn(async (key, guard) => { guard.assertCurrent(); credentials.delete(key) }),
  }
  const api: AuthApi = {
    login: vi.fn(async () => pair), me: vi.fn(async () => me),
    refresh: vi.fn(async () => ({ ...pair, refresh_token: 'secret_rotated', access_token: 'secret_rotated_access' })),
    logout: vi.fn(async () => undefined),
  }
  const controller = new SessionController({ repository, lease, api, now: () => now })
  return { controller, repository, api, profiles, credentials, lease,
    loseLease: () => { writable = false }, advance: () => { now = 60_000 } }
}

describe('session state machine', () => {
  it('login fetches identity separately and does not remember by default', async () => {
    const { controller, api, credentials } = setup()
    await controller.login('person@example.com', 'secret_password')
    expect(api.me).toHaveBeenCalledWith('secret_access')
    expect(controller.snapshot.status).toBe('authenticated')
    expect(controller.snapshot.user?.id).toBe(id)
    expect(controller.snapshot.workspaces).toEqual(me.workspaces)
    const credential = credentials.get(profileKey(id))!
    expect(credential.remember).toBe(false)
    expect(credential.refreshToken).toBeUndefined()
    expect(JSON.stringify(credential)).not.toContain('secret_')
    expect(JSON.stringify(controller.snapshot)).not.toContain('secret_')
  })

  it('non remembered reload retains unlocked cache without refreshing', async () => {
    const { controller, repository, lease, api } = setup()
    await controller.login('person@example.com', 'secret_password')
    const restored = new SessionController({ repository, lease, api })
    await restored.restore()
    expect(restored.snapshot.status).toBe('offline')
    expect(restored.snapshot.profile?.userId).toBe(id)
    expect(await restored.getAccessToken()).toBeNull()
    expect(api.refresh).not.toHaveBeenCalled()
  })

  it('remembered restore rotates once and saves only the next refresh', async () => {
    const { controller, repository, lease, api, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const restored = new SessionController({ repository, lease, api })
    await restored.restore()
    expect(restored.snapshot.status).toBe('authenticated')
    expect(api.refresh).toHaveBeenCalledExactlyOnceWith('secret_refresh')
    expect(credentials.get(profileKey(id))?.refreshToken).toBe('secret_rotated')
    expect(credentials.get(profileKey(id))?.generation).toBe(2)
  })

  it('serializes refresh and persists non secret inflight before network without remember', async () => {
    const { controller, api, credentials, advance } = setup()
    await controller.login('person@example.com', 'secret_password')
    advance()
    const pending = deferred<TokenPair>()
    vi.mocked(api.refresh).mockImplementation(async () => {
      expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
      expect(credentials.get(profileKey(id))?.refreshToken).toBeUndefined()
      return pending.promise
    })
    const first = controller.getAccessToken(); const second = controller.getAccessToken()
    await vi.waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1))
    pending.resolve({ ...pair, access_token: 'secret_next' })
    expect(await first).toBe('secret_next'); expect(await second).toBe('secret_next')
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(false)
    expect(credentials.get(profileKey(id))?.refreshToken).toBeUndefined()
  })

  it('refuses a crash leftover even when its old refresh remains in storage', async () => {
    const { controller, repository, lease, api, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    credentials.get(profileKey(id))!.refreshInflight = true
    const restored = new SessionController({ repository, lease, api })
    await restored.restore()
    expect(restored.snapshot.status).toBe('reauth-required')
    expect(await restored.getAccessToken()).toBeNull()
    expect(api.refresh).not.toHaveBeenCalled()
  })

  it('does not send refresh when the inflight marker cannot be saved', async () => {
    const { controller, repository, api, advance } = setup()
    await controller.login('person@example.com', 'secret_password')
    vi.mocked(repository.putCredential).mockRejectedValueOnce(new Error('quota'))
    advance()
    expect(await controller.getAccessToken()).toBeNull()
    expect(api.refresh).not.toHaveBeenCalled()
    expect(controller.snapshot.status).toBe('reauth-required')
  })

  it('lost response requires login and never resends the old refresh', async () => {
    const { controller, api, advance, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(api.refresh).mockRejectedValue(new Error('network'))
    advance()
    expect(await controller.getAccessToken()).toBeNull()
    expect(await controller.getAccessToken()).toBeNull()
    expect(api.refresh).toHaveBeenCalledTimes(1)
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
  })

  it('pair save failure leaves inflight and does not reuse either token', async () => {
    const { controller, repository, api, advance, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const put = vi.mocked(repository.putCredential).getMockImplementation()!
    vi.mocked(repository.putCredential).mockImplementation(async (value, lease) => {
      if (!value.refreshInflight) throw new Error('quota')
      return put(value, lease)
    })
    advance()
    expect(await controller.getAccessToken()).toBeNull()
    expect(await controller.getAccessToken()).toBeNull()
    expect(api.refresh).toHaveBeenCalledTimes(1)
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
  })

  it('lease loss rejects a late rotation before storing the new pair', async () => {
    const { controller, api, advance, loseLease, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const pending = deferred<TokenPair>()
    vi.mocked(api.refresh).mockReturnValue(pending.promise)
    advance()
    const access = controller.getAccessToken()
    await vi.waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1))
    loseLease(); pending.resolve({ ...pair, refresh_token: 'secret_late' })
    expect(await access).toBeNull()
    expect(credentials.get(profileKey(id))?.refreshToken).toBe('secret_refresh')
    expect(controller.snapshot.readonly).toBe(true)
  })

  it('readonly restore never reads credentials and disallows login/logout', async () => {
    const { controller, repository, api, loseLease } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(repository.getCredential).mockClear()
    loseLease()
    await controller.restore()
    expect(controller.snapshot.status).toBe('readonly')
    expect(repository.getCredential).not.toHaveBeenCalled()
    expect(api.refresh).not.toHaveBeenCalled()
    await expect(controller.login('person@example.com', 'secret_password')).rejects.toThrow()
    await expect(controller.logout()).rejects.toThrow()
  })

  it('logout locks the profile and reports local only on revocation failure without touching queues', async () => {
    const { controller, api, repository, profiles, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(api.logout).mockRejectedValueOnce(new Error('offline'))
    await controller.logout()
    expect(controller.snapshot.status).toBe('signed-out')
    expect(controller.snapshot.error).toBe('local_logout_only')
    expect(profiles.get(profileKey(id))?.locked).toBe(true)
    expect(profiles.get(profileKey(id))?.active).toBe(false)
    expect(credentials.size).toBe(0)
    expect(repository.mutate).not.toHaveBeenCalled()
    expect(api.logout).toHaveBeenCalledWith('secret_access')
  })

  it('logout discards late login completion', async () => {
    const { controller, api, credentials } = setup()
    const pending = deferred<TokenPair>()
    vi.mocked(api.login).mockReturnValue(pending.promise)
    const login = controller.login('person@example.com', 'secret_password')
    await controller.logout()
    pending.resolve(pair); await login
    expect(controller.snapshot.status).toBe('signed-out')
    expect(credentials.size).toBe(0)
    expect(api.me).not.toHaveBeenCalled()
  })

  it('different account locks the prior profile and activates only the new identity', async () => {
    const { controller, api, profiles } = setup()
    await controller.login('person@example.com', 'secret_password')
    const otherId = '30000000-0000-4000-8000-000000000001'
    vi.mocked(api.me).mockResolvedValue({ user: { ...me.user, id: otherId }, workspaces: [] })
    await controller.login('other@example.com', 'secret_other')
    expect(profiles.get(profileKey(id))).toMatchObject({ locked: true, active: false })
    expect(controller.snapshot.profile).toMatchObject({ userId: otherId, deployment: DEPLOYMENT })
    expect(controller.snapshot.workspaces).toEqual([])
  })

  it('logout prevents a late rotation from reactivating the profile', async () => {
    const { controller, api, advance, credentials, profiles } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const pending = deferred<TokenPair>()
    vi.mocked(api.refresh).mockReturnValue(pending.promise)
    advance()
    const access = controller.getAccessToken()
    await vi.waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1))
    await controller.logout()
    pending.resolve(pair)
    expect(await access).toBeNull()
    expect(credentials.size).toBe(0)
    expect(controller.snapshot.status).toBe('signed-out')
    expect(profiles.get(profileKey(id))?.locked).toBe(true)
  })

  it('rejects identity changes in refresh without activating other account content', async () => {
    const { controller, api, advance, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(api.me).mockResolvedValue({ ...me, user: { ...me.user, id: workspaceId } })
    advance()
    expect(await controller.getAccessToken()).toBeNull()
    expect(controller.snapshot.status).toBe('reauth-required')
    expect(controller.snapshot.profile?.userId).toBe(id)
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
  })

  it('failed local logout never claims successful local or remote revocation', async () => {
    const { controller, repository, api } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(repository.putProfile).mockRejectedValueOnce(new Error('quota'))
    await expect(controller.logout()).rejects.toMatchObject({ code: 'local_logout_failed' })
    expect(controller.snapshot.status).toBe('error')
    expect(api.logout).not.toHaveBeenCalled()
    expect(await controller.getAccessToken()).toBeNull()
  })

  it('logout during login still locks the previously active local profile', async () => {
    const { controller, api, profiles, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const pending = deferred<TokenPair>()
    vi.mocked(api.login).mockReturnValue(pending.promise)
    const login = controller.login('other@example.com', 'secret_other')
    await controller.logout()
    pending.resolve(pair); await login
    expect(profiles.get(profileKey(id))?.locked).toBe(true)
    expect(credentials.size).toBe(0)
  })

  it('profile update failure after rotation preserves the durable inflight fence', async () => {
    const { controller, repository, api, credentials, advance } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    vi.mocked(repository.putProfile).mockRejectedValueOnce(new Error('quota'))
    advance()
    expect(await controller.getAccessToken()).toBeNull()
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
    expect(await controller.getAccessToken()).toBeNull()
    expect(api.refresh).toHaveBeenCalledTimes(1)
  })

  it('identity lease fences old workspace callbacks immediately on account change', async () => {
    const { controller, api } = setup()
    await controller.login('person@example.com', 'secret_password')
    const guard = controller.captureIdentityLease()
    expect(() => guard.assertCurrent()).not.toThrow()
    const pending = deferred<TokenPair>()
    vi.mocked(api.login).mockReturnValue(pending.promise)
    const login = controller.login('other@example.com', 'secret_other')
    expect(() => guard.assertCurrent()).toThrow()
    pending.resolve(pair); await login
    expect(() => guard.assertCurrent()).toThrow()
  })

  it('suspend fences identity callbacks and late refresh before coordinator release', async () => {
    const { controller, api, advance, credentials } = setup()
    await controller.login('person@example.com', 'secret_password', true)
    const guard = controller.captureIdentityLease()
    const pending = deferred<TokenPair>()
    vi.mocked(api.refresh).mockReturnValue(pending.promise)
    advance()
    const access = controller.getAccessToken()
    await vi.waitFor(() => expect(api.refresh).toHaveBeenCalledTimes(1))
    controller.suspend()
    expect(() => guard.assertCurrent()).toThrow()
    pending.resolve({ ...pair, refresh_token: 'secret_late' })
    expect(await access).toBeNull()
    expect(controller.snapshot.status).toBe('readonly')
    expect(credentials.get(profileKey(id))?.refreshInflight).toBe(true)
    expect(credentials.get(profileKey(id))?.refreshToken).toBe('secret_refresh')
  })
})
