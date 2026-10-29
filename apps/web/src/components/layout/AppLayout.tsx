import { useState } from 'react'
import { FolderSidebar } from '../navigation/FolderSidebar'
import { NotesSidebar } from '../notes/NotesSidebar'
import { EditorPane } from '../editor/EditorPane'
import { ConflictPanel } from '../conflicts/ConflictPanel'
import { EntityActions, type ActionTarget } from '../navigation/EntityActions'
import { useWorkspace } from '../../workspace/useWorkspace'
import { visibleEntities } from '../../workspace/projection'
import { partitionKey } from '../../domain/ids'
import { buttonClass } from '../ui'
import { flushNoteWrites } from '../../workspace/useNoteDraft'

type ColumnView = 'folders' | 'notes' | 'editor'

export function AppLayout() {
  const { port, state, loading, loadFailed, reload } = useWorkspace()
  const [activeColumn, setActiveColumn] = useState<ColumnView>('notes')
  const [folderId, setFolderId] = useState<string | null>(null)
  const [noteId, setNoteId] = useState<string | null>(null)
  const [target, setTarget] = useState<ActionTarget | null>(null)
  const [recoveries, setRecoveries] = useState(false)
  const entities = state ? visibleEntities(state) : []
  const folders = entities.filter(entity => entity.entityType === 'folder')
  const notes = entities.filter(entity => entity.entityType === 'note' && (folderId === null || entity.parentId === folderId))
  const selected = entities.find(entity => entity.entityType === 'note' && entity.entityId === noteId)
  const recoveryCount = state?.recoveryRecords.filter(record => !record.resolved).length ?? 0

  return <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-[#0c0e12] text-zinc-100">
    <nav aria-label="栏位切换" className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-white/5 bg-[#101319] px-3 py-2">
      <div className="flex gap-1 lg:hidden">{([['folders', '文件夹'], ['notes', '笔记列表'], ['editor', '编辑区']] as const).map(([column, label]) => <button key={column} className={`${buttonClass} px-2 ${activeColumn === column ? 'border-indigo-500/30 bg-indigo-600/30' : ''}`} type="button" aria-pressed={activeColumn === column} onClick={() => setActiveColumn(column)}>{label}</button>)}</div>
      <button className={`${buttonClass} ml-auto`} type="button" aria-expanded={recoveries} onClick={() => setRecoveries(value => !value)}>冲突与恢复{recoveryCount ? ` (${recoveryCount})` : ''}</button>
    </nav>
    {loadFailed && <div role="alert" className="shrink-0 border-b border-rose-500/20 bg-rose-500/10 p-3 text-xs text-rose-200">工作区读取失败，不代表工作区为空。<button type="button" className={`${buttonClass} ml-2`} onClick={reload}>重新加载工作区</button></div>}
    {loading && <p role="status" className="shrink-0 p-3 text-xs text-zinc-400">正在读取此设备的工作区…</p>}
    {recoveries && <ConflictPanel records={state?.recoveryRecords ?? []} readOnly={port.readOnly} onResolve={async (record, action) => { await flushNoteWrites(port); await port.resolve(record, action) }} />}
    {target && <EntityActions key={JSON.stringify(target)} target={target} folders={folders} port={port} onClose={() => setTarget(null)} onCreated={(type, id) => { if (type === 'note') { setNoteId(id); setActiveColumn('editor') } else setFolderId(id) }} />}
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className={`h-full shrink-0 ${activeColumn === 'folders' ? 'flex w-full lg:w-60' : 'hidden lg:flex lg:w-60'}`}><FolderSidebar folders={folders} selectedId={folderId} onSelect={id => { setFolderId(id); setActiveColumn('notes') }} onCreate={() => setTarget({ mode: 'create', entityType: 'folder', parentId: folderId })} onManage={entity => setTarget({ mode: 'manage', entity })} /></div>
      <div className={`h-full shrink-0 ${activeColumn === 'notes' ? 'flex w-full lg:w-[300px]' : 'hidden lg:flex lg:w-[300px]'}`}><NotesSidebar notes={notes} selectedId={noteId} readOnly={port.readOnly} loaded={!!state?.bootstrapped} onSelect={id => { setNoteId(id); setActiveColumn('editor') }} onCreate={() => setTarget({ mode: 'create', entityType: 'note', parentId: folderId })} onManage={entity => setTarget({ mode: 'manage', entity })} /></div>
      <div className={`h-full min-w-0 ${activeColumn === 'editor' ? 'flex w-full flex-1' : 'hidden lg:flex lg:flex-1'}`}>
        {selected ? <EditorPane key={`${partitionKey(port.partition)}:${selected.entityId}:${port.ownershipGeneration}`} port={port} note={{ id: selected.entityId, title: selected.title, bodyMd: selected.bodyMd, parentId: selected.parentId }} /> : <main aria-label="笔记编辑区" className="flex flex-1 items-center justify-center bg-[#14171e] p-8 text-center"><div><h2 className="text-sm text-zinc-300">选择一篇笔记</h2><p className="mt-2 text-xs leading-relaxed text-zinc-500">从列表打开已有笔记，或新建一篇笔记。</p></div></main>}
      </div>
    </div>
  </div>
}
