import { createContext, useContext, useSyncExternalStore, type ReactNode } from 'react'
import { SessionController, type SessionSnapshot } from './session'

const SessionContext = createContext<SessionController | null>(null)

export function SessionProvider({ controller, children }: { controller: SessionController; children: ReactNode }) {
  return <SessionContext.Provider value={controller}>{children}</SessionContext.Provider>
}

export function useSessionController(): SessionController {
  const controller = useContext(SessionContext)
  if (!controller) throw new Error('SessionProvider is required')
  return controller
}

export function useSession(): SessionSnapshot {
  const controller = useSessionController()
  return useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
}
