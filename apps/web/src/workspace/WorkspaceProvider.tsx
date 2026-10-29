import { useEffect, useState, type ReactNode } from 'react'
import type { WorkspaceState } from '../domain/contracts'
import type { WorkspacePort } from './ports'
import { WorkspaceContext } from './useWorkspace'
import { ProfileHiddenError } from './identityVisibility'

export function WorkspaceProvider({ port, children }: { port: WorkspacePort; children: ReactNode }) {
  const [state, setState] = useState<WorkspaceState | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [revision, setRevision] = useState(0)
  const [hidden, setHidden] = useState(false)
  useEffect(() => {
    let alive = true
    let request = 0
    async function read() {
      const ticket = ++request
      try { const next = await port.read(); if (alive && ticket === request) { setState(next); setHidden(false); setLoadFailed(false); setLoading(false) } }
      catch (error) { if (alive && ticket === request) { setState(null); setHidden(error instanceof ProfileHiddenError); setLoadFailed(true); setLoading(false) } }
    }
    void read()
    const unsubscribe = port.subscribe(() => { void read() })
    return () => { alive = false; unsubscribe() }
  }, [port, revision])
  if (hidden) return <p role="status" className="p-8 text-sm text-amber-200">此账号的本机内容已锁定或账号已切换，工作区已隐藏。请重新登录后打开。</p>
  return <WorkspaceContext.Provider value={{ port, state, loading, loadFailed, reload: () => setRevision(value => value + 1) }}>{children}</WorkspaceContext.Provider>
}
