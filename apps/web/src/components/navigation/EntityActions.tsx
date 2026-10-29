import { useState } from 'react'
import type { Draft } from '../../domain/contracts'
import type { EntityType } from '../../domain/ids'
import type { WorkspacePort } from '../../workspace/ports'
import { folderOptions } from '../../workspace/projection'
import { buttonClass, inputClass } from '../ui'
import { flushNoteWrites } from '../../workspace/useNoteDraft'

export type ActionTarget = { mode: 'create'; entityType: EntityType; parentId: string | null } | { mode: 'manage'; entity: Draft }

export function EntityActions({ target, folders, port, onClose, onCreated }: { target: ActionTarget; folders: Draft[]; port: WorkspacePort; onClose(): void; onCreated(type: EntityType, id: string): void }) {
  const type = target.mode === 'create' ? target.entityType : target.entity.entityType
  const [title, setTitle] = useState(target.mode === 'manage' ? target.entity.title : '')
  const [parent, setParent] = useState(target.mode === 'manage' ? target.entity.parentId : target.parentId)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const name = type === 'folder' ? '文件夹' : '笔记'
  const options = folderOptions(folders, target.mode === 'manage' && type === 'folder' ? target.entity.entityId : undefined)
  async function run(work: () => Promise<void>) { if (port.readOnly || busy) return; setBusy(true); setError(false); try { await flushNoteWrites(port); await work(); onClose() } catch { setError(true) } finally { setBusy(false) } }
  return <section role="region" aria-label={`${target.mode === 'create' ? '新建' : '管理'}${name}`} className="max-h-[60dvh] shrink-0 space-y-3 overflow-y-auto border-b border-indigo-500/20 bg-[#161a24] p-3 sm:p-5">
    <div className="flex items-center justify-between"><h2 className="text-sm font-semibold">{target.mode === 'create' ? '新建' : '管理'}{name}</h2><button className={buttonClass} type="button" disabled={busy} onClick={onClose}>关闭操作</button></div>
    {(target.mode === 'create' || type === 'folder') ? <label className="block space-y-1 text-xs text-zinc-400">{name}名称<input autoFocus className={inputClass} aria-label={`${name}名称`} value={title} disabled={port.readOnly || busy} onChange={event => setTitle(event.target.value)} /></label> : <p className="text-xs text-zinc-400">{title || '无标题笔记'}。标题请在编辑区修改。</p>}
    <label className="block space-y-1 text-xs text-zinc-400">目标文件夹<select className={inputClass} aria-label="目标文件夹" value={parent ?? ''} disabled={port.readOnly || busy} onChange={event => setParent(event.target.value || null)}><option value="">根目录</option>{options.map(folder => <option key={folder.entityId} value={folder.entityId}>{folder.title}</option>)}</select></label>
    <div className="flex flex-wrap gap-2">
      {target.mode === 'create' ? <button className={buttonClass} type="button" disabled={port.readOnly || busy || (type === 'folder' && !title.trim())} onClick={() => { void run(async () => { const id = await port.create(type, title, parent); onCreated(type, id) }) }}>创建{name}</button> : <>
        {type === 'folder' && <button className={buttonClass} type="button" disabled={port.readOnly || busy || !title.trim()} onClick={() => { void run(() => port.save({ entityType: type, entityId: target.entity.entityId, title, bodyMd: target.entity.bodyMd, parentId: target.entity.parentId })) }}>保存{name}名称</button>}
        <button className={buttonClass} type="button" disabled={port.readOnly || busy} onClick={() => { void run(() => port.move(type, target.entity.entityId, parent)) }}>移动{name}</button>
        <button className={`${buttonClass} text-rose-300`} type="button" disabled={port.readOnly || busy} onClick={() => setDeleting(true)}>删除{name}</button>
      </>}
    </div>
    {deleting && target.mode === 'manage' && <div role="group" aria-label="确认删除" className="space-y-2 border-t border-white/10 pt-3"><p className="text-xs text-amber-200">确认删除{name}？{type === 'folder' ? '非空文件夹不会递归删除，服务器将拒绝操作。' : '此操作会加入同步队列。'}</p><div className="flex gap-2"><button className={buttonClass} type="button" disabled={port.readOnly || busy} onClick={() => { void run(() => port.remove(type, target.entity.entityId)) }}>确认删除{name}</button><button className={buttonClass} type="button" disabled={busy} onClick={() => setDeleting(false)}>取消删除</button></div></div>}
    {error && <p role="alert" className="text-xs text-rose-300">操作未完成，请检查本地存储和待处理项目。现有内容不会被清除。</p>}
  </section>
}
