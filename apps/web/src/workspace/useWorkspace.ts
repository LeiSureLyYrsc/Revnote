import { createContext, useContext } from 'react'
import type { WorkspaceState } from '../domain/contracts'
import type { WorkspacePort } from './ports'

export interface WorkspaceContextValue { port: WorkspacePort; state: WorkspaceState | null; loading: boolean; loadFailed: boolean; reload(): void }
export const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export function useWorkspace() {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('WorkspaceProvider is required')
  return value
}
