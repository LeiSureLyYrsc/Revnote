import { Folder, Layers, Plus } from 'lucide-react'
import type { Draft } from '../../domain/contracts'
import { folderDepth, orderedFolders } from '../../workspace/projection'
import { buttonClass } from '../ui'
import { SyncStatus } from '../sync/SyncStatus'
import { useWorkspace } from '../../workspace/useWorkspace'

interface FolderSidebarProps {
  folders: Draft[]
  selectedId: string | null
  onSelect(id: string | null): void
  onCreate(): void
  onManage(folder: Draft): void
}

export function FolderSidebar({ folders, selectedId, onSelect, onCreate, onManage }: FolderSidebarProps) {
  const { state, port } = useWorkspace()
  return <aside aria-label="文件夹与导航" className="flex h-full w-full flex-col border-r border-white/5 bg-[#0e1116] lg:w-60">
    <div className="flex items-center justify-between gap-2 border-b border-white/5 p-3.5"><div className="flex min-w-0 items-center gap-2"><Layers className="h-5 w-5 shrink-0 text-indigo-400" /><div className="min-w-0"><h1 className="text-sm font-semibold">Revnote</h1><p className="truncate text-[10px] text-zinc-400" title={port.partition.workspaceId}>工作区 {port.partition.workspaceId}</p></div></div><button aria-label="新建文件夹" title="新建文件夹" type="button" disabled={port.readOnly || !state?.bootstrapped} className={`${buttonClass} px-2`} onClick={onCreate}><Plus className="h-4 w-4" /></button></div>
    <nav aria-label="工作区文件夹" className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
      <button className={`${buttonClass} w-full justify-start ${selectedId === null ? 'bg-white/10' : ''}`} aria-pressed={selectedId === null} type="button" onClick={() => onSelect(null)}>全部笔记</button>
      {orderedFolders(folders).map(folder => <div key={folder.entityId} className="flex min-w-0 items-center gap-1" style={{ paddingLeft: `${folderDepth(folder, folders) * 12}px` }}>
        <button className={`${buttonClass} min-w-0 flex-1 justify-start ${selectedId === folder.entityId ? 'bg-indigo-500/15 text-indigo-200' : ''}`} type="button" aria-pressed={selectedId === folder.entityId} onClick={() => onSelect(folder.entityId)}><Folder className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{folder.title}</span></button>
        <button className={`${buttonClass} shrink-0 px-2`} type="button" aria-label={`管理文件夹 ${folder.title}`} disabled={port.readOnly} onClick={() => onManage(folder)}>⋯</button>
      </div>)}
      {state?.bootstrapped && !folders.length && <p className="p-3 text-xs leading-relaxed text-zinc-500">还没有文件夹。可直接新建笔记，或先建立文件夹。</p>}
    </nav>
    <SyncStatus state={state} readOnly={port.readOnly} onRetry={() => { void port.retry().catch(() => undefined) }} onExport={port.exportLocal} />
  </aside>
}
