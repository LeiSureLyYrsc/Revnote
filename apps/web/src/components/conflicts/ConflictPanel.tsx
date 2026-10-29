import { useState } from 'react'
import type { RecoveryRecord } from '../../domain/contracts'
import { buttonClass } from '../ui'
import { downloadMarkdown } from '../editor/markdownAdapter'

export type ResolutionAction = 'remote' | 'local' | 'copy'
const actionLabels = { remote: '采用远端', local: '以本地内容提交', copy: '另存为新笔记' }

export function ConflictPanel({ records, readOnly, onResolve }: {
  records: RecoveryRecord[]
  readOnly: boolean
  onResolve(record: RecoveryRecord, action: ResolutionAction): Promise<void>
}) {
  const [confirmation, setConfirmation] = useState<{ id: string; action: ResolutionAction } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const active = records.filter(record => !record.resolved)
  return <section aria-label="冲突与恢复" data-testid="conflict-panel" className="border-b border-white/10 bg-amber-500/[0.04] p-3 sm:p-5">
    <h2 className="text-sm font-semibold text-amber-200">冲突与恢复 <span className="text-xs">{active.length}</span></h2>
    {!active.length && <p className="mt-2 text-xs text-zinc-400">没有待处理的恢复记录。</p>}
    <div className="max-h-[55vh] space-y-5 overflow-y-auto">
      {active.map(record => {
        const deleted = !record.remote || !!record.remote.deleted_at || record.remote.deleted === true
        return <article key={record.id} className="mt-3 space-y-3" aria-label={`恢复记录 ${record.local?.title || record.entityId}`}>
          <p className="break-words text-xs text-zinc-300">{record.local?.title || record.entityId}：{deleted ? '远端已删除或不存在。保留本地内容，只能另存新对象。' : '版本不同。不会自动合并或覆盖。'}</p>
          <div className="grid min-w-0 gap-2 xl:grid-cols-3">
            {[
              ['基础版本', record.base?.body_md ?? record.base?.name ?? '（没有基础版本）'],
              ['本地版本', record.local?.bodyMd ?? record.local?.title ?? '（没有本地正文）'],
              ['远端版本', deleted ? '（远端已删除或不存在）' : record.remote?.body_md ?? record.remote?.name ?? ''],
            ].map(([label, text]) => <div key={label} className="min-w-0 rounded border border-white/10 bg-[#101319] p-2"><h3 className="mb-2 text-xs text-zinc-400">{label}</h3><pre aria-label={label} className="max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-zinc-200">{text}</pre></div>)}
          </div>
          <div className="flex flex-wrap gap-2">{(['remote', 'local', 'copy'] as const).filter(action => !deleted || action === 'copy').map(action => <button key={action} className={buttonClass} type="button" disabled={readOnly || busy || (action !== 'remote' && !record.local)} onClick={() => { setConfirmation({ id: record.id, action }); setError(false) }}>{actionLabels[action]}</button>)}</div>
          {confirmation?.id === record.id && <div role="group" aria-label="确认冲突处理" className="space-y-2 rounded border border-amber-500/30 p-3">
            <p className="text-xs text-amber-200">确认{actionLabels[confirmation.action]}？{confirmation.action === 'remote' ? '当前本地修改将被放弃，恢复记录仍保留。' : confirmation.action === 'local' ? '将基于当前远端版本重新提交；若版本再次变化，仍需重新处理。' : '将使用新的对象编号，不覆盖原对象。'}</p>
            <div className="flex gap-2"><button className={buttonClass} disabled={readOnly || busy} type="button" onClick={async () => { setBusy(true); try { await onResolve(record, confirmation.action); setConfirmation(null) } catch { setError(true) } finally { setBusy(false) } }}>确认{actionLabels[confirmation.action]}</button><button className={buttonClass} disabled={busy} type="button" onClick={() => setConfirmation(null)}>取消</button></div>
          </div>}
        </article>
      })}
    </div>
    {records.some(record => record.resolved && record.local) && <details className="mt-4 border-t border-white/10 pt-3 text-xs text-zinc-400"><summary className="cursor-pointer py-1 focus-visible:outline-2 focus-visible:outline-indigo-400">已保留的原文与处理记录</summary><div className="space-y-3 pt-3">{records.filter(record => record.resolved && record.local).map(record => <article key={record.id} className="min-w-0 rounded border border-white/10 p-3"><h3 className="break-words text-zinc-300">{record.local!.title || '无标题笔记'}{record.reason === 'before_rich_conversion' ? ' · 富文本转换前原文' : ' · 已处理的本地版本'}</h3><pre aria-label="保留的 Markdown 原文" className="my-2 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono">{record.local!.bodyMd}</pre><button className={buttonClass} type="button" onClick={() => downloadMarkdown(record.local!.title, record.local!.bodyMd)}>导出保留原文</button></article>)}</div></details>}
    {error && <p role="alert" className="mt-2 text-xs text-rose-300">处理未完成，内容仍保留。请先解决未决命令或重试。</p>}
  </section>
}
