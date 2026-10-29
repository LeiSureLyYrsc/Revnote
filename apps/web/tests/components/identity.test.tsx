import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SessionProvider } from '../../src/auth/SessionProvider'
import { apiClient } from '../../src/api/client'
import { emptyWorkspace, type LocalRepository, type Profile, type WorkspaceState } from '../../src/domain/contracts'
import { DEPLOYMENT } from '../../src/domain/ids'
import { activateRuntime } from '../../src/workspace/runtime'
import { RuntimeContext } from '../../src/workspace/RuntimeContext'
import { WorkspaceScreen } from '../../src/workspace/WorkspaceScreen'
import { createWorkspacePort } from '../../src/workspace/createWorkspacePort'
import { ProfileHiddenError } from '../../src/workspace/identityVisibility'
import type { WriteCoordinator } from '../../src/sync/coordinator'
import * as runtimeModule from '../../src/workspace/runtime'
import { App } from '../../src/App'
import { localArchive } from '../../src/workspace/exportLocal'
import { SyncStatus } from '../../src/components/sync/SyncStatus'

afterEach(() => vi.restoreAllMocks())

function fixture() {
  const partition = { deployment: DEPLOYMENT, userId: 'account-a', workspaceId: 'workspace-a' }
  const profile: Profile = { key: 'profile-a', deployment: DEPLOYMENT, userId: partition.userId, email: 'a@example.invalid', active: true, locked: false, workspaces: [{ id: partition.workspaceId, role: 'owner' }] }
  let profiles = [profile]
  const state = emptyWorkspace(partition)
  state.bootstrapped = true
  state.drafts['note:a'] = { key: 'note:a', entityType: 'note', entityId: 'a', title: '账号A笔记', bodyMd: '账号A正文不得泄漏', parentId: null, version: '1', acknowledgedVersion: '0', dirty: true, deleted: false, base: null }
  const repository = {
    listProfiles: vi.fn(async () => structuredClone(profiles)),
    read: vi.fn(async () => structuredClone(state)),
    getCredential: vi.fn(), mutate: vi.fn(), subscribe: () => () => {},
  } as unknown as LocalRepository
  const listeners = new Set<() => void>()
  let writer = false
  let acquireAvailable = false
  const notify = () => { for (const listener of listeners) listener() }
  const coordinator = {
    get isWriter() { return writer }, supported: true,
    acquire: vi.fn(async () => {
      if (acquireAvailable) { writer = true; notify() }
      return writer ? { generation: 1, assertCurrent() {} } : null
    }),
    release: vi.fn(async () => { writer = false; notify() }),
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
  } as unknown as WriteCoordinator
  return { partition, profile, state, repository, coordinator, setProfiles(next: Profile[]) { profiles = next }, setWriter(value: boolean) { writer = value }, setAcquireAvailable(value: boolean) { acquireAvailable = value }, notify }
}

describe('readonly cross-tab identity visibility', () => {
  for (const change of ['logout', 'switch-account'] as const) {
    it(`hides account A after writer ${change} without credentials or queue transmission`, async () => {
      const f = fixture()
      const runtime = await activateRuntime(f.repository, f.coordinator)
      const stop = runtime.visibility!.watch()
      const request = vi.spyOn(apiClient, 'request')
      const getToken = vi.spyOn(runtime.session, 'getAccessToken')
      render(<RuntimeContext.Provider value={runtime}><SessionProvider controller={runtime.session}><WorkspaceScreen /></SessionProvider></RuntimeContext.Provider>)
      fireEvent.click(await screen.findByRole('button', { name: '打开笔记 账号A笔记' }))
      expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('账号A正文不得泄漏')
      const locked = { ...f.profile, active: false, locked: true }
      f.setProfiles(change === 'logout' ? [locked] : [locked, { ...f.profile, key: 'profile-b', userId: 'account-b', email: 'b@example.invalid', workspaces: [{ id: 'workspace-b', role: 'owner' }] }])
      await act(async () => f.notify())
      await waitFor(() => expect(screen.queryByLabelText('Markdown 正文')).toBeNull())
      expect(screen.queryByText('账号A正文不得泄漏')).toBeNull()
      expect(runtime.session.snapshot.profile?.userId).toBe(change === 'logout' ? undefined : 'account-b')
      expect(f.repository.getCredential).not.toHaveBeenCalled()
      expect(getToken).not.toHaveBeenCalled()
      expect(request).not.toHaveBeenCalled()
      expect(f.repository.mutate).not.toHaveBeenCalled()
      stop()
    })
  }

  it('rejects a late workspace read when the stored profile locks during await', async () => {
    const f = fixture()
    const runtime = await activateRuntime(f.repository, f.coordinator)
    let finish!: (value: WorkspaceState) => void
    vi.mocked(f.repository.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const port = createWorkspacePort(runtime, f.partition)
    const pending = port.read()
    const rejected = expect(pending).rejects.toBeInstanceOf(ProfileHiddenError)
    await waitFor(() => expect(f.repository.read).toHaveBeenCalledTimes(1))
    f.setProfiles([{ ...f.profile, active: false, locked: true }])
    finish(f.state)
    await rejected
    await expect(port.read()).rejects.toBeInstanceOf(ProfileHiddenError)
    expect(f.repository.read).toHaveBeenCalledTimes(1)
  })

  it('hides existing body immediately while notification profile validation is pending', async () => {
    const f = fixture()
    const runtime = await activateRuntime(f.repository, f.coordinator)
    const stop = runtime.visibility!.watch()
    render(<RuntimeContext.Provider value={runtime}><SessionProvider controller={runtime.session}><WorkspaceScreen /></SessionProvider></RuntimeContext.Provider>)
    fireEvent.click(await screen.findByRole('button', { name: '打开笔记 账号A笔记' }))
    let finish!: (value: Profile[]) => void
    vi.mocked(f.repository.listProfiles).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    act(() => f.notify())
    expect(screen.queryByLabelText('Markdown 正文')).toBeNull()
    expect(screen.queryByText('账号A正文不得泄漏')).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('已隐藏工作区内容')
    await act(async () => { finish([{ ...f.profile, active: false, locked: true }]) })
    expect(runtime.session.snapshot.profile).toBeNull()
    expect(screen.queryByText('账号A正文不得泄漏')).toBeNull()
    stop()
  })

  it('fences older profile restore results when a newer identity notification wins', async () => {
    const f = fixture()
    const runtime = await activateRuntime(f.repository, f.coordinator)
    const stop = runtime.visibility!.watch()
    let finishOld!: (value: Profile[]) => void
    vi.mocked(f.repository.listProfiles).mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
    f.notify()
    expect(runtime.visibility!.getSnapshot().checking).toBe(true)
    const next = { ...f.profile, key: 'profile-b', userId: 'account-b', email: 'b@example.invalid' }
    f.setProfiles([next])
    f.notify()
    await waitFor(() => expect(runtime.session.snapshot.profile?.userId).toBe('account-b'))
    finishOld([f.profile])
    await act(async () => {})
    expect(runtime.session.snapshot.profile?.userId).toBe('account-b')
    expect(runtime.visibility!.getSnapshot().checking).toBe(false)
    expect(f.repository.getCredential).not.toHaveBeenCalled()
    stop()
  })

  it('does not restore the writer on local mutation notifications', async () => {
    const f = fixture()
    f.setWriter(true)
    const runtime = await activateRuntime(f.repository, f.coordinator)
    const restore = vi.spyOn(runtime.session, 'restore')
    const stop = runtime.visibility!.watch()
    f.notify(); f.notify()
    expect(restore).not.toHaveBeenCalled()
    expect(runtime.visibility!.getSnapshot().checking).toBe(false)
    stop()
  })

  it('exports only the visible current partition and preserves drafts intents operations and recovery', async () => {
    const f = fixture()
    const runtime = await activateRuntime(f.repository, f.coordinator)
    const port = createWorkspacePort(runtime, f.partition)
    const op = { op_id: 'op-a', entity_type: 'note' as const, entity_id: 'a', op: 'update' as const, base_rev: '1', payload: { title: '账号A笔记', body_md: '账号A正文不得泄漏' } }
    f.state.intents.push({ id: 'intent-a', entityType: 'note', entityId: 'a', kind: 'update', draftVersion: '1', payload: op.payload, state: 'frozen', operationId: 'op-a' })
    f.state.operations.push({ id: 'op-a', wire: op, intentIds: ['intent-a'], coveredVersion: '1', state: 'uncertain', attempts: 1 })
    f.state.recoveryRecords.push({ id: 'recovery-a', entityType: 'note', entityId: 'a', reason: 'remote_deleted', base: null, local: f.state.drafts['note:a'], remote: null, resolved: false, createdAt: 'now' })
    // Unknown persisted fields must not cross the export allowlist.
    Object.assign(f.state, { credentials: 'secret_credential_marker', profiles: 'secret_profile_marker' })
    Object.assign(f.state.drafts['note:a'], { refreshToken: 'secret_refresh_marker' })
    const archive = localArchive(f.state)
    const serialized = JSON.stringify(archive)
    expect(archive.workspaces).toHaveLength(1)
    expect(archive.workspaces[0].partition).toEqual(f.partition)
    expect(archive.workspaces[0].drafts['note:a']?.bodyMd).toBe('账号A正文不得泄漏')
    expect(archive.workspaces[0].intents).toHaveLength(1)
    expect(archive.workspaces[0].operations[0].wire).toEqual(op)
    expect(archive.workspaces[0].recoveryRecords).toHaveLength(1)
    expect(serialized).not.toContain('secret_credential_marker')
    expect(serialized).not.toContain('secret_profile_marker')
    expect(serialized).not.toContain('secret_refresh_marker')
    const originalCreate = URL.createObjectURL
    const originalRevoke = URL.revokeObjectURL
    const download = vi.fn(() => 'blob:recovery-check')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: download })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    vi.useFakeTimers()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    try {
      await port.exportLocal()
      expect(download).toHaveBeenCalledTimes(1)
      expect(click).toHaveBeenCalledTimes(1)
      expect(f.repository.read).toHaveBeenCalledWith(f.partition)
      expect(f.repository.mutate).not.toHaveBeenCalled()
      expect(f.repository.getCredential).not.toHaveBeenCalled()
      f.setProfiles([{ ...f.profile, active: false, locked: true }])
      await expect(port.exportLocal()).rejects.toBeInstanceOf(ProfileHiddenError)
      expect(download).toHaveBeenCalledTimes(1)
    } finally {
      vi.runOnlyPendingTimers()
      vi.useRealTimers()
      Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreate })
      Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevoke })
    }
  })

  it('allows readonly recovery export while synchronization is paused', async () => {
    const f = fixture()
    f.state.status = 'paused'; f.state.error = 'snapshot_too_large'
    const exportLocal = vi.fn(async () => {})
    render(<SyncStatus state={f.state} readOnly onRetry={vi.fn()} onExport={exportLocal} />)
    expect((screen.getByRole('button', { name: '重试同步' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '导出当前工作区本地数据' }))
    await waitFor(() => expect(exportLocal).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/导出不会清除草稿/)).toBeTruthy()
  })

  it('keeps an operable acquire entry in App after normal handoff', async () => {
    // jsdom has no viewport scrolling; this test covers lock/UI authority only.
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    const f = fixture()
    f.setWriter(true)
    const runtime = await activateRuntime(f.repository, f.coordinator)
    vi.spyOn(runtimeModule, 'initializeRuntime').mockResolvedValue(runtime)
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '交出写入权限' }))
    const acquire = await screen.findByRole('button', { name: '接管写入' })
    expect(f.coordinator.release).toHaveBeenCalledTimes(1)
    f.setAcquireAvailable(true)
    fireEvent.click(acquire)
    await screen.findByRole('button', { name: '交出写入权限' })
    expect(f.coordinator.isWriter).toBe(true)
    expect(screen.queryByRole('button', { name: '接管写入' })).toBeNull()
  })
})
