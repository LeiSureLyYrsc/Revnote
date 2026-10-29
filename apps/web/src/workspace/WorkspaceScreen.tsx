import { useContext, useEffect, useState, useSyncExternalStore } from 'react'
import { useSession, useSessionController } from '../auth/SessionProvider'
import { LoginForm } from '../components/auth/LoginForm'
import { AppLayout } from '../components/layout/AppLayout'
import { buttonClass, inputClass } from '../components/ui'
import { DEPLOYMENT, partitionKey } from '../domain/ids'
import { WorkspaceProvider } from './WorkspaceProvider'
import { createWorkspacePort, quiesceWorkspacePorts } from './createWorkspacePort'
import { RuntimeContext } from './RuntimeContext'
import { releaseRuntime } from './runtime'
import type { VisibilitySnapshot } from './identityVisibility'

const visible: VisibilitySnapshot = { checking: false, failed: false, generation: 0 }
const noSubscription = () => () => {}

export function WorkspaceScreen() {
  const runtime = useContext(RuntimeContext)
  const session = useSession()
  const controller = useSessionController()
  const visibility = useSyncExternalStore(runtime?.visibility?.subscribe ?? noSubscription, runtime?.visibility?.getSnapshot ?? (() => visible))
  const [workspaceId, setWorkspaceId] = useState('')
  const [loginOpen, setLoginOpen] = useState(false)
  const [logoutFailed, setLogoutFailed] = useState(false)
  const [selection, setSelection] = useState<string | null>(null)
  const [handoffBusy, setHandoffBusy] = useState(false)
  const [handoffFailed, setHandoffFailed] = useState(false)
  const currentUser = session.profile?.userId
  const available = session.workspaces.some(workspace => workspace.id === workspaceId) ? workspaceId : session.workspaces[0]?.id ?? ''
  const chosen = selection === currentUser ? available : session.workspaces[0]?.id ?? ''
  // The factory reuses only a still-valid identity-epoch binding. Revalidate on
  // every session render, including same-account reauthentication.
  const port = !visibility.checking && !visibility.failed && runtime && currentUser && chosen ? createWorkspacePort(runtime, { deployment: DEPLOYMENT, userId: currentUser, workspaceId: chosen }) : null
  useEffect(() => {
    if (!runtime || !port || port.readOnly || session.status !== 'authenticated') return
    void port.synchronize?.().catch(() => undefined)
    const timer = setInterval(() => { void port.synchronize?.().catch(() => undefined) }, 15_000)
    const online = () => { void port.synchronize?.().catch(() => undefined) }
    window.addEventListener('online', online)
    return () => { clearInterval(timer); window.removeEventListener('online', online) }
  }, [runtime, port, session.status])
  if (visibility.checking) return <p role="status" className="p-8 text-sm text-zinc-400">正在核对本机账号，已隐藏工作区内容…</p>
  if (visibility.failed) return <p role="alert" className="p-8 text-sm text-amber-200">无法确认本机账号状态，已隐藏工作区内容。请重新加载。</p>
  if (session.status === 'loading' && !session.profile) return <div role="status" className="p-8 text-sm text-zinc-400">正在加载登录状态…</div>
  if (!session.profile) return <div className="min-h-0 flex-1 overflow-y-auto py-10"><LoginForm readOnly={session.readonly} error={session.error === 'local_logout_only' ? '已在本机退出；未能确认远端会话已撤销。' : undefined} onLogin={(email, password, remember) => controller.login(email, password, remember)} /></div>
  return <>
    <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-white/5 bg-[#0e1116] px-3 py-2 text-xs">
      <span className="min-w-0 max-w-44 truncate text-zinc-400" title={session.profile.email}>{session.profile.email}</span>
      <label className="flex min-w-0 flex-1 items-center gap-2"><span className="shrink-0">工作区</span><select aria-label="选择工作区" className={`${inputClass} max-w-sm py-1 text-xs`} value={chosen} onChange={event => { setSelection(currentUser ?? null); setWorkspaceId(event.target.value) }}>{session.workspaces.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.id}</option>)}</select></label>
      {session.status !== 'authenticated' && !session.readonly && <button className={buttonClass} type="button" onClick={() => setLoginOpen(value => !value)}>重新登录</button>}
      <button className={buttonClass} type="button" disabled={session.readonly || handoffBusy} onClick={async () => { setLogoutFailed(false); try { if (runtime) await quiesceWorkspacePorts(runtime); await controller.logout() } catch { setLogoutFailed(true) } }}>退出登录</button>
      {!session.readonly && runtime && <button className={buttonClass} type="button" disabled={handoffBusy} onClick={async () => { setHandoffBusy(true); setHandoffFailed(false); try { await releaseRuntime(runtime) } catch { setHandoffFailed(true) } finally { setHandoffBusy(false) } }}>{handoffBusy ? '正在保存并交接…' : '交出写入权限'}</button>}
    </header>
    {session.status !== 'authenticated' && <p role="status" className="shrink-0 bg-amber-500/5 px-3 py-2 text-xs text-amber-200">{session.readonly ? '此标签页只读。请在可写标签页编辑或登录。' : '已恢复此设备缓存。当前尚未连接登录会话，可继续本地编辑；同步前请重新登录。'}</p>}
    {logoutFailed && <p role="alert" className="p-3 text-xs text-rose-300">本机退出未完成，请重试。当前不宣称已退出。</p>}
    {handoffFailed && <p role="alert" className="p-3 text-xs text-rose-300">交接未完成，仍保留写入权限。请先保存或导出未落盘内容后重试。</p>}
    {loginOpen && !session.readonly && <div className="max-h-[60vh] shrink-0 overflow-y-auto border-b border-white/10"><LoginForm readOnly={handoffBusy} onLogin={async (email, password, remember) => { if (runtime) await quiesceWorkspacePorts(runtime); await controller.login(email, password, remember); setLoginOpen(false) }} /></div>}
    {!session.workspaces.length ? <p className="p-8 text-sm text-zinc-400">该账号没有可用工作区。请联系工作区管理员。</p> : port && <WorkspaceProvider key={`${partitionKey(port.partition)}:${port.ownershipGeneration}`} port={port}><AppLayout /></WorkspaceProvider>}
  </>
}
