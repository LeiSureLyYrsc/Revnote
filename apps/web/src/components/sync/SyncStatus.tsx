import type { WorkspaceState } from '../../domain/contracts'
import { buttonClass } from '../ui'
import { useState } from 'react'

const labels = { idle: '同步就绪', syncing: '正在同步', offline: '网络不可达，等待同步', paused: '同步已暂停', rebuilding: '正在重建远端镜像' }
function errorHint(code: string): string {
  if (code.includes('snapshot') || code.includes('too_large') || code.includes('limit') || code.includes('budget')) return '工作区或同步数据超过当前可处理范围，已暂停。请保留并导出本地内容；不会清空工作区或提高限制。'
  if (code.includes('cursor') || code.includes('rebuild')) return '同步位置需要重新核对。正在保留草稿与原命令；重建未完成前不会把工作区当作空白。'
  if (code.includes('login') || code.includes('auth') || code.includes('token')) return '登录会话不可用，请重新登录同一账号后同步。本地草稿仍保留。'
  return '同步暂未完成。本地草稿不会因此被清除。请检查连接或重新登录。'
}
function operationHint(code?: string): string {
  if (code?.includes('not_empty') || code?.includes('non_empty')) return '文件夹非空，不能删除。请先移动或删除其中内容。'
  if (code?.includes('parent') || code?.includes('cycle')) return '目标文件夹不可用或移动会形成循环，请选择其他位置。'
  if (code?.includes('not_found') || code?.includes('deleted')) return '远端对象不可用；本地内容仍保留，请检查恢复记录。'
  if (code?.includes('forbidden') || code?.includes('permission')) return '当前账号没有执行此操作的权限。'
  return '操作被拒绝，本地内容仍保留。请检查目标位置与工作区权限。'
}

export function SyncStatus({ state, readOnly, onRetry, onExport }: { state: WorkspaceState | null; readOnly: boolean; onRetry(): void; onExport?(): Promise<void> }) {
  const [exportBusy, setExportBusy] = useState(false)
  const [exportStatus, setExportStatus] = useState('')
  const pending = state?.intents.filter(intent => intent.state !== 'done').length ?? 0
  const conflicts = state?.recoveryRecords.filter(record => !record.resolved).length ?? 0
  return <section aria-label="工作区同步状态" data-testid="workspace-sync-status" className="space-y-2 border-t border-white/5 bg-[#0b0d11] p-3 text-xs">
    <p className="font-medium text-zinc-200">{!state ? '工作区尚未加载' : !state.bootstrapped ? '工作区正在加载，尚未完成初始化' : labels[state.status]}</p>
    {state && <p className="text-zinc-400">{pending ? `${pending} 项待同步` : state.bootstrapped ? '没有待同步操作' : '尚不能确认工作区内容'}{conflicts ? `，${conflicts} 项待处理` : ''}</p>}
    {state?.error && <p role="alert" className="break-words text-amber-300">{errorHint(state.error)}</p>}
    {state?.operations.some(operation => operation.state === 'rejected' && state.intents.some(intent => operation.intentIds.includes(intent.id) && intent.state !== 'done')) && <ul aria-label="被拒绝的同步操作" className="space-y-2 text-amber-300">{state.operations.filter(operation => operation.state === 'rejected' && state.intents.some(intent => operation.intentIds.includes(intent.id) && intent.state !== 'done')).map(operation => <li key={operation.id}>{operationHint(operation.result?.error?.code ?? operation.error)}</li>)}</ul>}
    <button type="button" className={buttonClass} disabled={readOnly || !state} onClick={onRetry}>重试同步</button>
    {onExport && <div className="space-y-2"><button type="button" className={buttonClass} disabled={!state || exportBusy} onClick={async () => { setExportBusy(true); setExportStatus(''); try { await onExport(); setExportStatus('已准备本地数据下载；导出不会清除草稿或待同步操作。') } catch { setExportStatus('导出未完成。账号可能已锁定或本地读取失败，请重新核对账号。') } finally { setExportBusy(false) } }}>导出当前工作区本地数据</button><p className="leading-relaxed text-zinc-500">包含此工作区已落盘的草稿、意图、原命令与恢复记录，不包含登录凭据。文件含笔记内容，请妥善保管。尚未写入本地的编辑请另行复制或导出 Markdown。</p>{exportStatus && <p role="status" className="text-zinc-300">{exportStatus}</p>}</div>}
    <p className="leading-relaxed text-zinc-500">此设备保存不等于永久备份；页面关闭后不会后台同步。</p>
  </section>
}
