import { createAuthApi, type AuthApi } from '../api/auth'
import { ApiError } from '../api/client'
import type { MeResponse, TokenPair } from '../api/contracts'
import type { Credential, LocalRepository, Profile, WriteLease } from '../domain/contracts'
import { DEPLOYMENT } from '../domain/ids'

export type SessionStatus = 'signed-out' | 'loading' | 'authenticated' | 'offline' | 'reauth-required' | 'readonly' | 'error'
export interface SessionSnapshot {
  status: SessionStatus
  profile: Profile | null
  user: MeResponse['user'] | null
  workspaces: MeResponse['workspaces']
  readonly: boolean
  error: string | null
}
export interface SessionOptions {
  repository: LocalRepository
  lease: WriteLease
  api?: AuthApi
  now?: () => number
}

export function profileKey(userId: string): string { return JSON.stringify([DEPLOYMENT, userId]) }

/** One controller per tab. The injected lease is checked before every credential write. */
export class SessionController {
  private readonly repository: LocalRepository
  private readonly lease: WriteLease
  private readonly api: AuthApi
  private readonly now: () => number
  private listeners = new Set<() => void>()
  private epoch = 0
  private secret_access: string | null = null
  private secret_refresh: string | null = null
  private expiresAt = 0
  private credential: Credential | null = null
  private refreshing: Promise<string | null> | null = null
  private current: SessionSnapshot = { status: 'signed-out', profile: null, user: null, workspaces: [], readonly: false, error: null }

  constructor(options: SessionOptions) {
    this.repository = options.repository; this.lease = options.lease
    this.api = options.api ?? createAuthApi(); this.now = options.now ?? Date.now
  }

  get snapshot(): SessionSnapshot { return this.current }
  getSnapshot = (): SessionSnapshot => this.current
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Call before coordinator.release(); invalidation is synchronous. */
  suspend(): void {
    ++this.epoch
    this.clearSecrets(); this.credential = null; this.refreshing = null
    this.publish({ status: 'readonly', readonly: true, error: null })
  }

  /** Bind workspace writes and sync callbacks to this exact authenticated/local identity epoch. */
  captureIdentityLease(): WriteLease {
    this.lease.assertCurrent()
    const epoch = this.epoch
    const userId = this.current.profile?.userId
    if (!userId || this.current.readonly || this.current.profile?.locked) throw new ApiError('no_active_identity')
    return {
      generation: this.lease.generation,
      assertCurrent: () => {
        this.assert(epoch)
        if (this.current.readonly || this.current.profile?.userId !== userId) throw new ApiError('session_superseded')
      },
    }
  }

  private publish(change: Partial<SessionSnapshot>): void {
    this.current = { ...this.current, ...change }
    for (const listener of this.listeners) listener()
  }
  private clearSecrets(): void { this.secret_access = null; this.secret_refresh = null; this.expiresAt = 0 }
  private assert(epoch: number): void {
    if (epoch !== this.epoch) throw new ApiError('session_superseded')
    this.lease.assertCurrent()
  }
  private ownsLease(): boolean { try { this.lease.assertCurrent(); return true } catch { return false } }
  private guardedLease(epoch: number): WriteLease {
    return { generation: this.lease.generation, assertCurrent: () => this.assert(epoch) }
  }
  private async putCredential(value: Credential, epoch: number): Promise<void> {
    this.assert(epoch)
    await this.repository.putCredential(value, this.guardedLease(epoch))
    this.assert(epoch)
    this.credential = value
  }
  private async putProfile(value: Profile, epoch: number): Promise<void> {
    this.assert(epoch)
    await this.repository.putProfile(value, this.guardedLease(epoch))
    this.assert(epoch)
  }
  private requireLogin(reason: string): void {
    this.clearSecrets()
    const readonly = !this.ownsLease()
    this.publish({ status: readonly ? 'readonly' : 'reauth-required', readonly, error: reason })
  }

  async login(email: string, password: string, remember = false): Promise<void> {
    this.lease.assertCurrent()
    const epoch = ++this.epoch
    this.clearSecrets(); this.credential = null; this.refreshing = null
    this.publish({ status: 'loading', profile: null, user: null, workspaces: [], readonly: false, error: null })
    try {
      const pair = await this.api.login(email, password)
      this.assert(epoch)
      const me = await this.api.me(pair.access_token)
      this.assert(epoch)
      const key = profileKey(me.user.id)
      const previous = await this.repository.getCredential(key)
      this.assert(epoch)
      // Hide every previously active identity before activating the authenticated one.
      for (const profile of await this.repository.listProfiles()) {
        this.assert(epoch)
        if (profile.deployment === DEPLOYMENT && profile.active) {
          await this.putProfile({ ...profile, active: false, locked: true }, epoch)
        }
      }
      const profile: Profile = { key, deployment: DEPLOYMENT, userId: me.user.id, email: me.user.email, active: true, locked: false, workspaces: me.workspaces }
      const credential: Credential = {
        key, userId: me.user.id, generation: (previous?.generation ?? 0) + 1, remember,
        refreshInflight: false, sessionId: pair.session_id,
        ...(remember ? { refreshToken: pair.refresh_token } : {}),
      }
      await this.putCredential(credential, epoch)
      await this.putProfile(profile, epoch)
      this.activate(pair, me, profile)
    } catch (error) {
      if (epoch !== this.epoch) return
      this.clearSecrets()
      this.publish({ status: this.ownsLease() ? 'reauth-required' : 'readonly', readonly: !this.ownsLease(), error: error instanceof ApiError ? error.code : 'local_session_save_failed' })
      throw new ApiError(error instanceof ApiError ? error.code : 'local_session_save_failed')
    }
  }

  async restore(): Promise<void> {
    const epoch = ++this.epoch
    this.clearSecrets(); this.credential = null; this.refreshing = null
    this.publish({ status: 'loading', profile: null, user: null, workspaces: [], error: null })
    try {
      const profiles = await this.repository.listProfiles()
      if (epoch !== this.epoch) return
      const profile = profiles.find(row => row.deployment === DEPLOYMENT && row.active && !row.locked)
      const readonly = !this.ownsLease()
      if (!profile) { this.publish({ status: readonly ? 'readonly' : 'signed-out', readonly }); return }
      this.publish({ status: readonly ? 'readonly' : 'offline', profile, workspaces: profile.workspaces, readonly })
      // Followers may read cached content, but never read or rotate credentials.
      if (readonly) return
      const credential = await this.repository.getCredential(profile.key)
      this.assert(epoch)
      if (!credential || credential.userId !== profile.userId) return
      this.credential = credential
      if (credential.refreshInflight) { this.requireLogin('refresh_outcome_unknown'); return }
      if (!credential.remember || !credential.refreshToken) return
      this.secret_refresh = credential.refreshToken
      await this.refresh()
    } catch {
      if (epoch === this.epoch) this.requireLogin('session_restore_failed')
    }
  }

  private activate(pair: TokenPair, me: MeResponse, profile: Profile): void {
    this.secret_access = pair.access_token; this.secret_refresh = pair.refresh_token
    this.expiresAt = this.now() + pair.expires_in * 1000
    this.publish({ status: 'authenticated', profile, user: me.user, workspaces: me.workspaces, readonly: false, error: null })
  }

  async getAccessToken(): Promise<string | null> {
    if (!this.ownsLease()) { this.clearSecrets(); this.publish({ status: 'readonly', readonly: true }); return null }
    if (this.secret_access && this.now() < this.expiresAt - 5_000) return this.secret_access
    return this.refresh()
  }

  /** Explicit 401 handling: one coordinated rotation; the caller chooses whether to retry its request. */
  invalidateAccessToken(): void { this.secret_access = null; this.expiresAt = 0 }

  private refresh(): Promise<string | null> {
    if (this.refreshing) return this.refreshing
    const run = this.rotate()
    this.refreshing = run
    void run.finally(() => { if (this.refreshing === run) this.refreshing = null })
    return run
  }

  private async rotate(): Promise<string | null> {
    const epoch = this.epoch
    const previous = this.credential
    const profile = this.current.profile
    const secret_old = this.secret_refresh
    if (!previous || !profile || !secret_old || previous.refreshInflight) return null
    try {
      this.assert(epoch)
      // This non-secret marker is durable even for non-remembered sessions.
      await this.putCredential({ ...previous, refreshInflight: true }, epoch)
      this.secret_refresh = null; this.secret_access = null
      const pair = await this.api.refresh(secret_old)
      this.assert(epoch)
      const me = await this.api.me(pair.access_token)
      this.assert(epoch)
      if (me.user.id !== profile.userId) throw new ApiError('identity_mismatch')
      const next: Credential = {
        key: previous.key, userId: previous.userId, generation: previous.generation + 1,
        remember: previous.remember, refreshInflight: false, sessionId: pair.session_id,
        ...(previous.remember ? { refreshToken: pair.refresh_token } : {}),
      }
      const updated = { ...profile, workspaces: me.workspaces, email: me.user.email }
      await this.putProfile(updated, epoch)
      // Clear inflight last: every earlier failed local commit remains a crash fence.
      await this.putCredential(next, epoch)
      this.activate(pair, me, updated)
      return this.secret_access
    } catch {
      if (epoch === this.epoch) {
        // Never reuse the previous refresh after any ambiguous request/save window.
        this.credential = previous ? { ...previous, refreshInflight: true, refreshToken: undefined } : null
        this.requireLogin('refresh_requires_login')
      }
      return null
    }
  }

  async logout(): Promise<void> {
    this.lease.assertCurrent()
    const epoch = ++this.epoch
    const profile = this.current.profile
    const secret_access = this.secret_access
    this.clearSecrets(); this.credential = null; this.refreshing = null
    this.publish({ status: 'loading', profile: null, user: null, workspaces: [], error: null })
    try {
      const profiles = await this.repository.listProfiles()
      this.assert(epoch)
      const active = profiles.filter(value => value.deployment === DEPLOYMENT && value.active)
      if (profile && !active.some(value => value.key === profile.key)) active.push(profile)
      for (const value of active) {
        // Lock first; deleting a credential is not the privacy boundary.
        await this.putProfile({ ...value, active: false, locked: true }, epoch)
        this.assert(epoch)
        await this.repository.deleteCredential(value.key, this.guardedLease(epoch))
        this.assert(epoch)
      }
    } catch {
      if (epoch === this.epoch) this.publish({ status: 'error', error: 'local_logout_failed' })
      throw new ApiError('local_logout_failed')
    }
    let revoked = false
    if (secret_access) {
      try { await this.api.logout(secret_access); revoked = true } catch { /* Local logout remains valid. */ }
    }
    if (epoch === this.epoch) this.publish({ status: 'signed-out', error: revoked ? null : 'local_logout_only' })
  }
}
