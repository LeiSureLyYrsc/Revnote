import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../../src/App'
import { AppLayout } from '../../src/components/layout/AppLayout'
import { WorkspaceProvider } from '../../src/workspace/WorkspaceProvider'
import { activateRuntime } from '../../src/workspace/runtime'
import * as runtimeModule from '../../src/workspace/runtime'
import { DEPLOYMENT } from '../../src/domain/ids'
import { emptyWorkspace, type LocalRepository, type Profile, type WorkspaceState, type WriteLease } from '../../src/domain/contracts'
import type { WorkspacePort } from '../../src/workspace/ports'
import type { WriteCoordinator } from '../../src/sync/coordinator'

// jsdom has no viewport scrolling and the router restores scroll on every
// committed match; unmocked, that jsdom "not implemented" report fails the run.
beforeEach(() => { vi.spyOn(window, 'scrollTo').mockImplementation(() => {}) })
afterEach(() => vi.restoreAllMocks())

const partition = { deployment: DEPLOYMENT, userId: 'account-a', workspaceId: 'workspace-a' }
const profile: Profile = {
  key: 'profile-a', deployment: DEPLOYMENT, userId: partition.userId, email: 'a@example.invalid',
  active: true, locked: false, workspaces: [{ id: partition.workspaceId, role: 'owner' }],
}

function workspace(): WorkspaceState {
  const state = emptyWorkspace(partition)
  state.bootstrapped = true
  state.drafts['note:a'] = {
    key: 'note:a', entityType: 'note', entityId: 'a', title: '接管笔记', bodyMd: '接管后的正文',
    parentId: null, version: '1', acknowledgedVersion: '0', dirty: false, deleted: false, base: null,
  }
  return state
}

/**
 * The real takeover seam, held open deterministically.
 *
 * `App`'s handler is `session.suspend()` then `await activateRuntime(...)`, and
 * `activateRuntime` starts with `coordinator.acquire()`. Holding *that* call open
 * parks the app inside the takeover window itself — between the click and the
 * commit — with no timer involved: the window stays open until the test lowers
 * the gate. Whatever the DOM says while the gate is up is therefore the state of
 * the app during a real takeover, not a guess about the timing of one.
 */
function takeoverFixture() {
  const state = workspace()
  const repository = {
    listProfiles: vi.fn(async () => [structuredClone(profile)]),
    read: vi.fn(async () => structuredClone(state)),
    getCredential: vi.fn(async () => undefined),
    mutate: vi.fn(async (_partition: unknown, captured: WriteLease, change: (value: WorkspaceState) => unknown) => { captured.assertCurrent(); return change(state) }),
    subscribe: () => () => {},
  } as unknown as LocalRepository
  const listeners = new Set<() => void>()
  const notify = (): void => { for (const listener of listeners) listener() }
  let writer = false
  let lockAvailable = false
  let gate: (() => void) | null = null
  const lease = (): WriteLease => ({ generation: 1, assertCurrent() { if (!writer) throw new Error('Write ownership is no longer current') } })
  const coordinator = {
    get isWriter() { return writer },
    supported: true,
    acquire: vi.fn(async () => {
      // Parked here only when the test armed the gate; the initial runtime's
      // acquire is plain, so mounting observes an ordinary read-only follower.
      if (gate) await new Promise<void>(resolve => { gate = resolve })
      if (!lockAvailable) return null
      writer = true
      notify()
      return lease()
    }),
    release: vi.fn(async () => { writer = false; notify() }),
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
  } as unknown as WriteCoordinator
  return {
    state, repository, coordinator,
    allowLock() { lockAvailable = true },
    /** Park the next `acquire` inside the takeover window until released. */
    holdTakeover() { gate = () => {} },
    releaseTakeover() { const wake = gate; gate = null; wake?.() },
  }
}

function portFixture(state: WorkspaceState, overrides: Partial<WorkspacePort> = {}): WorkspacePort {
  return {
    partition, ownershipGeneration: 7, readOnly: false,
    read: async () => structuredClone(state),
    exportLocal: vi.fn(async () => {}), subscribe: () => () => {},
    save: vi.fn(async () => {}), create: vi.fn(async () => 'new-id'), move: vi.fn(async () => {}),
    remove: vi.fn(async () => {}), preserveSource: vi.fn(async () => {}), resolve: vi.fn(async () => {}),
    retry: vi.fn(async () => {}), ...overrides,
  }
}

/**
 * `workspace-ready` is the readiness contract the browser helpers wait on, so it
 * has to mean exactly one thing: *this mounted tree has settled its own durable
 * read*. Two independent facts are proven here.
 */
describe('workspace readiness signal', () => {
  it('is absent until the mounted tree settles its read and carries that tree generation', async () => {
    const state = workspace()
    let finish!: (value: WorkspaceState) => void
    const first = portFixture(state, { read: () => new Promise<WorkspaceState>(resolve => { finish = resolve }) })
    const { rerender } = render(<WorkspaceProvider port={first}><AppLayout /></WorkspaceProvider>)
    // Pending read: no marker, and nothing that could be clicked either. The
    // columns are not rendered from a projection the tree has not read yet.
    expect(screen.queryByTestId('workspace-ready')).toBeNull()
    expect(screen.queryByRole('button', { name: '打开笔记 接管笔记' })).toBeNull()
    await act(async () => { finish(structuredClone(state)) })
    const settled = screen.getByTestId('workspace-ready')
    expect(settled.getAttribute('data-ownership-generation')).toBe('7')
    expect(settled.getAttribute('data-bootstrapped')).toBe('true')
    // A settled read is enough even when bootstrap has not completed: the marker
    // must not silently require it, or opening a note in a capped workspace would
    // wait for a snapshot that can never arrive.
    const capped = { ...state, bootstrapped: false }
    let finishCapped!: (value: WorkspaceState) => void
    const second = portFixture(capped, { ownershipGeneration: 8, read: () => new Promise<WorkspaceState>(resolve => { finishCapped = resolve }) })
    rerender(<WorkspaceProvider key="second" port={second}><AppLayout /></WorkspaceProvider>)
    // A new ownership generation is a remount, so the previous tree's marker dies
    // with it. A wait on this marker can therefore never be satisfied by a tree
    // that is about to be replaced.
    expect(screen.queryByTestId('workspace-ready')).toBeNull()
    expect(screen.queryByRole('button', { name: '打开笔记 接管笔记' })).toBeNull()
    await act(async () => { finishCapped(structuredClone(capped)) })
    const next = screen.getByTestId('workspace-ready')
    expect(next.getAttribute('data-ownership-generation')).toBe('8')
    expect(next.getAttribute('data-bootstrapped')).toBe('false')
    expect(next).not.toBe(settled)
  })
})

/**
 * The reported CI flake, made impossible by construction.
 *
 * `openNote()` used to race the ownership-generation remount a writer takeover
 * performs. The flake was not that a readiness marker was missing: it was that
 * the *superseded* tree stayed on screen and still answered clicks. A click
 * dispatched into it set a selection the remount then discarded, so
 * `note-source` never became visible and the case died at the helper's
 * visibility check.
 */
describe('writer takeover ordering', () => {
  it('replaces the superseded tree with a restoring state, then publishes readiness and opens the note', async () => {
    const f = takeoverFixture()
    const runtime = await activateRuntime(f.repository, f.coordinator)
    vi.spyOn(runtimeModule, 'initializeRuntime').mockResolvedValue(runtime)
    render(<App />)
    const before = await screen.findByTestId('workspace-ready')
    const beforeGeneration = before.getAttribute('data-ownership-generation')
    expect(beforeGeneration).not.toBeNull()
    expect(screen.getByRole('button', { name: '打开笔记 接管笔记' })).toBeTruthy()

    f.allowLock()
    f.holdTakeover()
    fireEvent.click(screen.getByRole('button', { name: '接管写入' }))
    await screen.findByTestId('workspace-restoring')

    // Inside the takeover window: readiness is gone and the note buttons are not
    // mounted, so a note cannot be opened into a tree the commit will discard.
    expect(screen.queryByTestId('workspace-ready')).toBeNull()
    expect(screen.queryByRole('button', { name: '打开笔记 接管笔记' })).toBeNull()
    // The restoring state is a state, not a timer: it holds while the takeover is
    // still in flight, across a bare microtask flush.
    await act(async () => { await Promise.resolve() })
    expect(screen.getByTestId('workspace-restoring')).toBeTruthy()
    expect(screen.queryByTestId('workspace-ready')).toBeNull()

    await act(async () => { f.releaseTakeover() })
    const after = await screen.findByTestId('workspace-ready')
    // The marker now belongs to the successor generation, not to the tree the
    // takeover replaced, which is what makes waiting on it sound.
    expect(after.getAttribute('data-ownership-generation')).not.toBe(beforeGeneration)
    expect(screen.queryByTestId('workspace-restoring')).toBeNull()

    // Ready, then open: the selection survives, so no remount followed the click.
    fireEvent.click(await screen.findByRole('button', { name: '打开笔记 接管笔记' }))
    expect((await screen.findByLabelText('Markdown 正文') as HTMLTextAreaElement).value).toBe('接管后的正文')
    expect(screen.getByTestId('workspace-ready')).toBe(after)
    expect(screen.getByRole('button', { name: '交出写入权限' })).toBeTruthy()
  })
})
