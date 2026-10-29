import React, { useEffect, useState, useSyncExternalStore } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { SessionProvider } from './auth/SessionProvider'
import { activateRuntime, initializeRuntime, type ClientRuntime } from './workspace/runtime'
import { RuntimeContext } from './workspace/RuntimeContext'
import { buttonClass } from './components/ui'

export const App: React.FC = () => {
  const [runtime, setRuntime] = useState<ClientRuntime | null>(null)
  const [failed, setFailed] = useState(false)
  const [takeoverBusy, setTakeoverBusy] = useState(false)
  const [takeoverFailed, setTakeoverFailed] = useState(false)
  useEffect(() => { let alive = true; void initializeRuntime().then(value => { if (alive) setRuntime(value) }, () => { if (alive) setFailed(true) }); return () => { alive = false } }, [])
  useEffect(() => {
    if (!runtime) return
    return runtime.visibility?.watch()
  }, [runtime])
  const writer = useSyncExternalStore(runtime?.coordinator.subscribe ?? noSubscription, () => runtime?.coordinator.isWriter ?? false)
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      })
  )

  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex h-dvh w-full max-w-full flex-col overflow-hidden bg-[#0c0e12] text-zinc-100">
        {failed ? <p role="alert" className="p-8 text-sm text-rose-300">无法打开本地存储，已阻止编辑与登录。请检查浏览器存储权限或关闭旧标签页后重新加载；没有清除数据。</p> : !runtime ? <p role="status" className="p-8 text-sm text-zinc-400">正在打开本地存储…</p> : <RuntimeContext.Provider value={runtime}><SessionProvider controller={runtime.session}>
          {!writer && <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-amber-500/20 bg-amber-500/10 p-3 text-xs text-amber-200"><p>{runtime.coordinator.supported ? '另一个标签页持有写入权限，此页只读。关闭原标签页后可接管。' : '浏览器不支持所需的安全写入锁。请使用安全上下文中的 Chromium 或 Edge。'}</p>{runtime.coordinator.supported && <button className={buttonClass} disabled={takeoverBusy} type="button" onClick={async () => { setTakeoverBusy(true); setTakeoverFailed(false); try { runtime.session.suspend(); const next = await activateRuntime(runtime.repository, runtime.coordinator); queryClient.clear(); setRuntime(next) } catch { setTakeoverFailed(true) } finally { setTakeoverBusy(false) } }}>接管写入</button>}</div>}
          {takeoverFailed && <p role="alert" className="p-3 text-xs text-rose-300">未能接管写入权限，当前仍不可编辑。请关闭原写入标签页后重试。</p>}
          <RouterProvider router={router} />
        </SessionProvider></RuntimeContext.Provider>}
      </div>
    </QueryClientProvider>
  )
}

function noSubscription() { return () => {} }
