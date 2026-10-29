import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EditorPane } from '../../src/components/editor/EditorPane'
import { LoginForm } from '../../src/components/auth/LoginForm'
import { SyncStatus } from '../../src/components/sync/SyncStatus'
import { ConflictPanel } from '../../src/components/conflicts/ConflictPanel'
import { applySourceInput, withTitle } from '../../src/components/editor/markdownAdapter'
import { emptyWorkspace, type LocalRepository, type Profile, type RecoveryRecord, type WriteLease } from '../../src/domain/contracts'
import type { WorkspacePort } from '../../src/workspace/ports'
import { visibleEntities, folderOptions } from '../../src/workspace/projection'
import { EntityActions } from '../../src/components/navigation/EntityActions'
import { createWorkspacePort, quiesceWorkspacePorts } from '../../src/workspace/createWorkspacePort'
import type { ClientRuntime } from '../../src/workspace/runtime'
import { releaseRuntime } from '../../src/workspace/runtime'
import { WorkspaceScreen } from '../../src/workspace/WorkspaceScreen'
import { RuntimeContext } from '../../src/workspace/RuntimeContext'
import { SessionProvider } from '../../src/auth/SessionProvider'
import { SessionController } from '../../src/auth/session'
import { DEPLOYMENT } from '../../src/domain/ids'
import type { WriteCoordinator } from '../../src/sync/coordinator'
import { apiClient, ApiError } from '../../src/api/client'

afterEach(() => { vi.restoreAllMocks() })

const partition = { deployment: 'component-check', userId: 'user-a', workspaceId: 'workspace-a' }
const raw = '# 标题  \r\n\r\n$$x^2$$\r\n<custom data-x="1">未知</custom>\r\n:::other\r\n尾空白  \r\n'
function port(overrides: Partial<WorkspacePort> = {}): WorkspacePort {
  return { partition, ownershipGeneration: 1, readOnly: false, read: async () => emptyWorkspace(partition), exportLocal: vi.fn(async () => {}), subscribe: () => () => {}, save: vi.fn(async () => {}), create: vi.fn(async () => 'new-id'), move: vi.fn(async () => {}), remove: vi.fn(async () => {}), preserveSource: vi.fn(async () => {}), resolve: vi.fn(async () => {}), retry: vi.fn(async () => {}), ...overrides }
}
const note = { id: 'note-a', title: '原始标题', bodyMd: raw, parentId: null }

describe('markdown_preservation component source truth', () => {
  it('keeps bytes on open view switching and title-only save', async () => {
    const api = port()
    render(<EditorPane port={api} note={note} />)
    expect(api.save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '查看原文' }))
    fireEvent.click(screen.getByRole('button', { name: '查看原文' }))
    fireEvent.click(screen.getByRole('button', { name: 'Markdown 源码' }))
    expect(api.save).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('笔记标题'), { target: { value: '仅改标题' } })
    await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.save).mock.calls[0][0].bodyMd).toBe(raw)
    expect(new TextEncoder().encode(withTitle(note, '新标题').bodyMd)).toEqual(new TextEncoder().encode(raw))
  })

  it('retains CRLF and trailing spaces without parser round trip', () => {
    expect(applySourceInput(raw, raw.replace(/\r\n/g, '\n'))).toBe(raw)
    expect(applySourceInput(raw, raw.replace(/\r\n/g, '\n') + 'next\n')).toBe(raw + 'next\r\n')
  })

  it('waits for local commit and does not let older completion clear newer edit', async () => {
    let finishA!: () => void
    let finishB!: () => void
    const api = port({ save: vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finishA = resolve })).mockImplementationOnce(() => new Promise<void>(resolve => { finishB = resolve })) })
    render(<EditorPane port={api} note={note} />)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: 'A' } })
    await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: 'B' } })
    expect(screen.getByTestId('local-save-status').textContent).toBe('正在写入本地…')
    await act(async () => { finishA() })
    expect(screen.getByTestId('local-save-status').textContent).toBe('正在写入本地…')
    await act(async () => { finishB() })
    expect(screen.getByTestId('local-save-status').textContent).toBe('已写入本地')
    expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('B')
  })

  it('retains edited content and export actions after local failure', async () => {
    render(<EditorPane port={port({ save: vi.fn(async () => { throw new Error('quota') }) })} note={note} />)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: '仍在编辑区' } })
    await waitFor(() => expect(screen.getByTestId('local-save-status').textContent).toBe('尚未写入本地'))
    expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('仍在编辑区')
    expect(screen.getByRole('button', { name: '复制 Markdown' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '导出 Markdown' })).toBeTruthy()
  })

  it('blocks rich conversion when the source recovery transaction fails', async () => {
    const api = port({ preserveSource: vi.fn(async () => { throw new Error('abort') }) })
    render(<EditorPane port={api} note={note} />)
    fireEvent.click(screen.getByRole('button', { name: '转换为富文本' }))
    fireEvent.click(screen.getByRole('button', { name: '保存恢复点并转换' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('已阻止转换'))
    expect(screen.getByLabelText('Markdown 正文')).toBeTruthy()
    expect(api.save).not.toHaveBeenCalled()
  })

  it('does not serialize loaded rich text before an actual user edit', async () => {
    const api = port()
    render(<EditorPane port={api} note={note} />)
    fireEvent.click(screen.getByRole('button', { name: '转换为富文本' }))
    fireEvent.click(screen.getByRole('button', { name: '保存恢复点并转换' }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: '富文本正文' })).toBeTruthy())
    expect(api.preserveSource).toHaveBeenCalledWith(note.id, note.title, raw)
    expect(api.save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Markdown 源码' }))
    expect(api.save).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('笔记标题'), { target: { value: '转换后仅改标题' } })
    await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.save).mock.calls[0][0].bodyMd).toBe(raw)
  })

  it('binds late saves to the original entity and partition during note switching', async () => {
    let finish!: () => void
    const api = port({ save: vi.fn(() => new Promise<void>(resolve => { finish = resolve })) })
    const { rerender } = render(<EditorPane key="a" port={api} note={note} />)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: 'A 后续输入' } })
    await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1))
    const otherPort = port({ partition: { ...partition, userId: 'user-b' } })
    rerender(<EditorPane key="b" port={otherPort} note={{ ...note, id: 'note-b', bodyMd: 'B 原文' }} />)
    await act(async () => { finish() })
    expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('B 原文')
    expect(otherPort.save).not.toHaveBeenCalled()
    expect(vi.mocked(api.save).mock.calls[0][0].entityId).toBe('note-a')
  })

  it('retains a failed editor buffer when switching away and returning', async () => {
    const api = port({ save: vi.fn(async () => { throw new Error('quota') }) })
    const { rerender } = render(<EditorPane key="a" port={api} note={note} />)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: '尚未落盘的输入' } })
    await waitFor(() => expect(screen.getByTestId('local-save-status').textContent).toBe('尚未写入本地'))
    rerender(<EditorPane key="b" port={api} note={{ ...note, id: 'note-b' }} />)
    rerender(<EditorPane key="a-returned" port={api} note={note} />)
    expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('尚未落盘的输入')
    expect(screen.getByTestId('local-save-status').textContent).toBe('尚未写入本地')
  })

  it('locks title source rich conversion and recovery writes in readonly editors', () => {
    const api = port({ readOnly: true })
    render(<EditorPane port={api} note={note} />)
    expect((screen.getByLabelText('笔记标题') as HTMLInputElement).readOnly).toBe(true)
    expect((screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement).readOnly).toBe(true)
    expect((screen.getByRole('button', { name: '转换为富文本' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: '不能持久化' } })
    expect(api.save).not.toHaveBeenCalled()
    expect(api.preserveSource).not.toHaveBeenCalled()
  })
})

describe('workspace accessible boundaries', () => {
  it('invalidates a rejected access token and coordinates refresh before manual retry', async () => {
    const state = emptyWorkspace(partition)
    const lease = { generation: 1, assertCurrent() {} }
    const repository = {
      read: vi.fn(async () => state), subscribe: () => () => {},
      mutate: vi.fn(async (_partition, captured: WriteLease, change) => { captured.assertCurrent(); return change(state) }),
    } as unknown as LocalRepository
    const session = {
      snapshot: { profile: { userId: partition.userId, active: true, locked: false }, readonly: false, status: 'authenticated' },
      captureIdentityLease: () => lease,
      getAccessToken: vi.fn().mockResolvedValueOnce('secret_old_access').mockResolvedValue('secret_new_access'),
      invalidateAccessToken: vi.fn(),
    }
    const runtime = { repository, lease, session, coordinator: { isWriter: true } } as unknown as ClientRuntime
    const request = vi.spyOn(apiClient, 'request').mockRejectedValueOnce(new ApiError('access_expired', 401, false))
    const api = createWorkspacePort(runtime, partition)
    await api.synchronize?.()
    expect(session.invalidateAccessToken).toHaveBeenCalledTimes(1)
    expect(session.getAccessToken).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenCalledTimes(1)
    expect(state.status).toBe('paused')
    expect(state.error).toBe('access_expired')
  })

  it('suspends identity before releasing the browser write lock', async () => {
    const order: string[] = []
    const runtime = { session: { suspend() { order.push('suspend') } }, coordinator: { async release() { order.push('release') } } } as unknown as ClientRuntime
    await releaseRuntime(runtime)
    expect(runtime.writesPaused).toBe(true)
    expect(order).toEqual(['suspend', 'release'])
  })

  it('fences old workspace ports across logout and same-account reauthentication', async () => {
    let epoch = 1
    const state = emptyWorkspace(partition)
    const lease = { generation: 1, assertCurrent() {} }
    const repository = {
      read: vi.fn(async () => state), subscribe: () => () => {},
      mutate: vi.fn(async (_partition, captured: WriteLease, change) => { captured.assertCurrent(); return change(state) }),
    } as unknown as LocalRepository
    const profile = { userId: partition.userId, active: true, locked: false }
    const session = {
      snapshot: { profile, readonly: false, status: 'offline' },
      captureIdentityLease() { const captured = epoch; return { generation: 1, assertCurrent() { if (captured !== epoch) throw new Error('identity superseded') } } },
    }
    const runtime = { repository, lease, session, coordinator: { isWriter: true } } as unknown as ClientRuntime
    const first = createWorkspacePort(runtime, partition)
    const id = await first.create('note', 'first', null)
    epoch++
    const second = createWorkspacePort(runtime, partition)
    expect(second.ownershipGeneration).not.toBe(first.ownershipGeneration)
    await expect(first.save({ entityType: 'note', entityId: id, title: 'stale', bodyMd: 'stale', parentId: null })).rejects.toThrow('identity superseded')
    expect(state.drafts[`note:${id}`].title).toBe('first')
    await second.save({ entityType: 'note', entityId: id, title: 'current', bodyMd: raw, parentId: null })
    expect(state.drafts[`note:${id}`].bodyMd).toBe(raw)
  })

  it('does not remember login by default and locks credentials in readonly tabs', () => {
    render(<LoginForm readOnly onLogin={vi.fn()} />)
    expect((screen.getByRole('checkbox', { name: '记住登录' }) as HTMLInputElement).checked).toBe(false)
    expect((screen.getByLabelText('登录密码') as HTMLInputElement).closest('fieldset')?.disabled).toBe(true)
    expect(screen.getByText(/同源脚本可能读取/)).toBeTruthy()
  })

  it('separates network failure from local saving failure', () => {
    const state = emptyWorkspace(partition)
    state.bootstrapped = true; state.status = 'offline'; state.error = 'network'
    render(<SyncStatus state={state} readOnly={false} onRetry={vi.fn()} />)
    expect(screen.getByTestId('workspace-sync-status').textContent).toContain('网络不可达')
    expect(screen.getByTestId('workspace-sync-status').textContent).not.toContain('本地写入失败')
  })

  it('shows three plain-text versions and requires explicit conflict confirmation', async () => {
    const record = { id: 'recovery-a', entityType: 'note', entityId: 'note-a', reason: 'conflict', base: { body_md: 'base' }, local: { title: '冲突笔记', bodyMd: '<img src=x onerror=alert(1)>' }, remote: { body_md: 'remote', deleted_at: null }, resolved: false, createdAt: 'now' } as RecoveryRecord
    const resolve = vi.fn(async () => {})
    render(<ConflictPanel records={[record]} readOnly={false} onResolve={resolve} />)
    expect(screen.getByLabelText('基础版本').textContent).toBe('base')
    expect(screen.getByLabelText('本地版本').querySelector('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '采用远端' }))
    expect(resolve).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认采用远端' }))
    await waitFor(() => expect(resolve).toHaveBeenCalledWith(record, 'remote'))
  })

  it('remote deletion allows only explicit save as a new note', () => {
    const record = { id: 'deleted', entityType: 'note', entityId: 'note-a', reason: 'remote_deleted', base: null, local: { title: '恢复', bodyMd: raw }, remote: null, resolved: false, createdAt: 'now' } as RecoveryRecord
    render(<ConflictPanel records={[record]} readOnly onResolve={vi.fn()} />)
    expect(screen.queryByRole('button', { name: '采用远端' })).toBeNull()
    expect(screen.queryByRole('button', { name: '以本地内容提交' })).toBeNull()
    expect((screen.getByRole('button', { name: '另存为新笔记' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not hide notes with a missing parent and excludes descendants from folder move choices', () => {
    const state = emptyWorkspace(partition)
    const draft = { key: 'note:orphan', entityType: 'note' as const, entityId: 'orphan', title: '未归类笔记', bodyMd: raw, parentId: 'not-cached', version: '1', acknowledgedVersion: '0', base: null, dirty: true, deleted: false }
    state.drafts[draft.key] = draft
    expect(visibleEntities(state).map(entity => entity.entityId)).toEqual(['orphan'])
    const parent = { ...draft, entityType: 'folder' as const, entityId: 'parent', parentId: null }
    const child = { ...parent, entityId: 'child', parentId: 'parent' }
    const unrelated = { ...parent, entityId: 'other' }
    expect(folderOptions([parent, child, unrelated], 'parent').map(folder => folder.entityId)).toEqual(['other'])
  })

  it('exposes keyboard-native move selector and requires deletion confirmation', async () => {
    const api = port()
    const entity = { key: 'note:note-a', entityType: 'note' as const, entityId: 'note-a', title: '标题', bodyMd: raw, parentId: null, version: '1', acknowledgedVersion: '0', base: null, dirty: true, deleted: false }
    render(<EntityActions target={{ mode: 'manage', entity }} folders={[]} port={api} onClose={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: '目标文件夹' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '删除笔记' }))
    expect(api.remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认删除笔记' }))
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith('note', 'note-a'))
  })
})

/**
 * Real E2E defect: with synchronization failing (network unreachable) the logout
 * button never completed, because quiescing rethrew the sync rejection and the
 * caller treated it as a fatal error. Locking local content must not depend on
 * the server being reachable; uncommitted editor input still must.
 */
describe('quiescing survives failed synchronization', () => {
  const quiescePartition = { deployment: DEPLOYMENT, userId: 'account-q', workspaceId: 'workspace-q' }
  const profile: Profile = { key: 'profile-q', deployment: DEPLOYMENT, userId: quiescePartition.userId, email: 'q@example.invalid', active: true, locked: false, workspaces: [{ id: quiescePartition.workspaceId, role: 'owner' }] }

  function runtimeFixture() {
    const state = emptyWorkspace(quiescePartition)
    state.bootstrapped = true
    const repository = {
      read: vi.fn(async () => structuredClone(state)),
      listProfiles: vi.fn(async () => [structuredClone(profile)]),
      mutate: vi.fn(async (_partition, captured: WriteLease, change) => { captured.assertCurrent(); return change(state) }),
      subscribe: () => () => {}, getCredential: vi.fn(), putProfile: vi.fn(), putCredential: vi.fn(), deleteCredential: vi.fn(),
    } as unknown as LocalRepository
    const listeners = new Set<() => void>()
    let writer = true
    const coordinator = {
      get isWriter() { return writer }, supported: true,
      acquire: vi.fn(async () => (writer ? { generation: 1, assertCurrent() {} } : null)),
      release: vi.fn(async () => { writer = false; for (const listener of listeners) listener() }),
      subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    } as unknown as WriteCoordinator
    // A real SessionController in its authenticated state: the engine only sends when
    // the session says authenticated, and the token lookup must not fail first.
    const lease = { generation: 1, assertCurrent() {} }
    const session = new SessionController({ repository, lease })
    Reflect.set(session, 'current', { status: 'authenticated', profile: structuredClone(profile), user: null, workspaces: profile.workspaces, readonly: false, error: null })
    Reflect.set(session, 'secret_access', 'secret_test_access')
    Reflect.set(session, 'expiresAt', Date.now() + 3_600_000)
    const runtime = { repository, coordinator, session, lease } as unknown as ClientRuntime
    return { runtime, state, repository, coordinator }
  }

  // The engine keeps the failure in durable state instead of rejecting the pass, so
  // "sync failed" has to be asserted through state, not through a thrown promise.
  function failSync() {
    return vi.spyOn(apiClient, 'request').mockRejectedValue(new ApiError('network_error', 0, false, true))
  }

  it('releaseRuntime completes and still releases the write lock when sync rejects', async () => {
    const f = runtimeFixture()
    const failing = failSync()
    const workspace = createWorkspacePort(f.runtime, quiescePartition)
    // Start a poll-style sync but do not await it: a failing pass retries with
    // backoff, which is exactly the window a user would press "退出登录" in.
    const running = workspace.synchronize?.()
    await waitFor(() => expect(failing).toHaveBeenCalled())
    await expect(releaseRuntime(f.runtime)).resolves.toBeUndefined()
    await running?.catch(() => undefined)
    expect(f.runtime.writesPaused).toBe(true)
    expect(f.coordinator.release).toHaveBeenCalledTimes(1)
    expect(f.coordinator.isWriter).toBe(false)
    // The failure is durable for the next explicit retry; quiescing never clears it.
    expect(f.state.error).toBe('network_error')
    // Stopping the backoff must not be recorded as an extra failed attempt.
    expect(f.state.failures).toBe(1)
  })

  it('logout quiescing completes so the login form can be reached with a failed sync', async () => {
    const f = runtimeFixture()
    const failing = failSync()
    const workspace = createWorkspacePort(f.runtime, quiescePartition)
    const running = workspace.synchronize?.()
    await waitFor(() => expect(failing).toHaveBeenCalled())
    // The defect was the throw: without it the caller proceeds to controller.logout().
    await expect(quiesceWorkspacePorts(f.runtime)).resolves.toBeUndefined()
    await running?.catch(() => undefined)
    await expect(f.runtime.session.logout()).resolves.toBeUndefined()
    expect(f.runtime.session.snapshot.profile).toBeNull()
    expect(f.runtime.session.snapshot.status).toBe('signed-out')
    // The failed sync stays durable for the next explicit retry; it is not cleared.
    expect(f.state.error).toBe('network_error')
  })

  it('renders the login form after logout while a failing sync is still running', async () => {
    const f = runtimeFixture()
    vi.spyOn(apiClient, 'request').mockRejectedValue(new ApiError('network_error', 0, false, true))
    render(<RuntimeContext.Provider value={f.runtime}><SessionProvider controller={f.runtime.session}><WorkspaceScreen /></SessionProvider></RuntimeContext.Provider>)
    // The workspace effect starts a sync on mount; the click must land while the
    // engine is inside its retry backoff, which is what used to hang logout.
    const button = await screen.findByRole('button', { name: '退出登录' })
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByLabelText('登录邮箱')).toBeTruthy())
    expect(screen.queryByText('本机退出未完成，请重试。当前不宣称已退出。')).toBeNull()
  })

  it('uncommitted editor input still blocks quitting instead of being abandoned', async () => {
    const f = runtimeFixture()
    const workspace = createWorkspacePort(f.runtime, quiescePartition)
    const noteId = await workspace.create('note', '未落盘', null)
    // A local save that fails leaves the buffer dirty, exactly like a quota error.
    // The same port instance must be used: pending editor buffers are keyed by it.
    vi.spyOn(workspace, 'save').mockRejectedValue(new Error('quota'))
    render(<RuntimeContext.Provider value={f.runtime}><SessionProvider controller={f.runtime.session}>
      <EditorPane port={workspace} note={{ id: noteId, title: '未落盘', bodyMd: '', parentId: null }} />
    </SessionProvider></RuntimeContext.Provider>)
    fireEvent.change(screen.getByLabelText('Markdown 正文'), { target: { value: '不能丢弃的输入' } })
    await waitFor(() => expect(screen.getByTestId('local-save-status').textContent).toBe('尚未写入本地'))
    await expect(quiesceWorkspacePorts(f.runtime)).rejects.toThrow('Uncommitted editor input must be saved or exported first')
    // The handoff path keeps its guard: the lock is still held after the refusal.
    await expect(releaseRuntime(f.runtime)).rejects.toThrow('Uncommitted editor input must be saved or exported first')
    expect(f.runtime.writesPaused).toBe(false)
    expect(f.coordinator.release).not.toHaveBeenCalled()
    expect(f.coordinator.isWriter).toBe(true)
  })
})
