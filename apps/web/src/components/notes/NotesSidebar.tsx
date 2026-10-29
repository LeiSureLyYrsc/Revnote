import { FileText, Plus } from 'lucide-react'
import type { Draft } from '../../domain/contracts'
import { buttonClass } from '../ui'

export function NotesSidebar({ notes, selectedId, readOnly, loaded, onSelect, onCreate, onManage }: {
  notes: Draft[]; selectedId: string | null; readOnly: boolean; loaded: boolean
  onSelect(id: string): void; onCreate(): void; onManage(note: Draft): void
}) {
  return <aside aria-label="笔记列表" className="flex h-full w-full flex-col border-r border-white/5 bg-[#11141a] lg:w-[300px]">
    <header className="flex items-center justify-between border-b border-white/5 p-3.5"><h2 className="text-xs font-semibold">笔记列表 <span className="ml-2 text-zinc-500">{loaded ? notes.length : '未加载'}</span></h2><button className={buttonClass} type="button" aria-label="新建笔记" disabled={readOnly || !loaded} onClick={onCreate}><Plus className="h-3.5 w-3.5" />新建</button></header>
    <div className="min-h-0 flex-1 overflow-y-auto p-2">
      {notes.map(note => <div key={note.entityId} className="mb-1 flex min-w-0 items-start gap-1">
        <button className={`min-w-0 flex-1 rounded-md border p-3 text-left focus-visible:outline-2 focus-visible:outline-indigo-400 ${selectedId === note.entityId ? 'border-indigo-500/25 bg-indigo-500/10' : 'border-transparent hover:bg-white/[0.03]'}`} type="button" aria-pressed={selectedId === note.entityId} aria-label={`打开笔记 ${note.title || '无标题笔记'}`} onClick={() => onSelect(note.entityId)}><div className="flex items-center gap-2"><FileText className="h-3.5 w-3.5 shrink-0 text-zinc-500" /><h3 className="truncate text-sm text-zinc-200">{note.title || '无标题笔记'}</h3></div><p className="mt-2 line-clamp-2 whitespace-pre-wrap break-words text-xs leading-relaxed text-zinc-500">{note.bodyMd || '正文为空'}</p>{note.blocked && <p className="mt-2 text-xs text-amber-300">需要人工处理</p>}</button>
        <button className={`${buttonClass} mt-2 px-2`} aria-label={`管理笔记 ${note.title || '无标题笔记'}`} type="button" disabled={readOnly} onClick={() => onManage(note)}>⋯</button>
      </div>)}
      {loaded && !notes.length && <div className="px-5 py-12 text-center"><FileText className="mx-auto mb-3 h-7 w-7 text-zinc-600" /><h3 className="text-sm text-zinc-300">暂无笔记</h3><p className="mt-2 text-xs leading-relaxed text-zinc-500">在当前文件夹新建一篇笔记。</p></div>}
    </div>
    <p className="border-t border-white/5 p-3 text-[11px] text-zinc-500">Markdown 原文保存 · 不提供搜索或归档</p>
  </aside>
}
