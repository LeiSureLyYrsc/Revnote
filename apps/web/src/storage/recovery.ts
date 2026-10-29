import type { WorkspaceState } from '../domain/contracts'

export interface RecoveryExport {
  format: 'revnote-recovery-v1'
  exportedAt: string
  workspaces: WorkspaceState[]
}

/** Explicit allowlist: credentials, refresh tokens and profiles never enter it. */
export function recoveryExport(workspaces: WorkspaceState[]): RecoveryExport {
  return {
    format: 'revnote-recovery-v1',
    exportedAt: new Date().toISOString(),
    workspaces: structuredClone(workspaces),
  }
}
