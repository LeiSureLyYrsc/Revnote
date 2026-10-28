import React from 'react'
import {
  Folder,
  Archive,
  Trash2,
  FileText,
  Plus,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  Activity,
  Layers,
} from 'lucide-react'
import { useHealthCheck } from '../../api/health'

interface FolderSidebarProps {
  onSelectCategory?: (category: string) => void
  activeCategory?: string
}

export const FolderSidebar: React.FC<FolderSidebarProps> = ({
  activeCategory = 'all',
}) => {
  const { data: health, isLoading, isFetching, refetch } = useHealthCheck()

  const navItems = [
    { id: 'all', label: '全部笔记', icon: FileText, active: activeCategory === 'all' },
    { id: 'memos', label: '快速备忘', icon: Folder, active: activeCategory === 'memos' },
    { id: 'archive', label: '归档笔记', icon: Archive, active: activeCategory === 'archive' },
    { id: 'trash', label: '废纸篓', icon: Trash2, active: activeCategory === 'trash' },
  ]

  return (
    <aside
      className="w-full lg:w-60 h-full flex flex-col bg-[#0e1116] border-r border-white/5 select-none"
      aria-label="文件夹与导航"
    >
      {/* Workspace Header */}
      <div className="p-3.5 border-b border-white/5 flex items-center justify-between">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-7 h-7 rounded-md bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shrink-0">
            <Layers className="w-4 h-4" />
          </div>
          <div className="truncate">
            <div className="text-xs font-semibold text-zinc-100 tracking-tight flex items-center gap-1.5">
              <span>Revnote</span>
              <span className="text-[10px] font-normal text-indigo-300/80 px-1 py-0.2 bg-indigo-500/10 rounded border border-indigo-500/20">
                Web
              </span>
            </div>
            <div className="text-[11px] text-zinc-400 truncate">本地演示工作区</div>
          </div>
        </div>

        {/* Disabled New Folder action */}
        <button
          type="button"
          disabled
          title="新建文件夹功能未实现（等待同步内核）"
          className="p-1 rounded text-zinc-400 opacity-40 cursor-not-allowed hover:text-zinc-300"
          aria-label="新建文件夹（未实现）"
        >
          <Plus className="w-4 h-4" />
        </button>
      </div>

      {/* Navigation Folder List */}
      <div className="flex-1 overflow-y-auto px-2 py-3 space-y-0.5">
        <div className="px-2 pb-1.5 text-[10px] font-medium tracking-wider text-zinc-400 uppercase">
          导航目录
        </div>

        {navItems.map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.id}
              type="button"
              disabled
              title="目录筛选尚未接入（待后端分类索引支持）"
              className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-xs font-medium text-left cursor-default ${
                item.active
                  ? 'bg-white/10 text-zinc-100'
                  : 'text-zinc-400 opacity-60'
              }`}
            >
              <Icon className={`w-3.5 h-3.5 shrink-0 ${item.active ? 'text-indigo-400' : 'text-zinc-500'}`} />
              <span className="truncate flex-1">{item.label}</span>
            </button>
          )
        })}

        {/* Notebook Folders Section (Placeholder empty state) */}
        <div className="pt-4 px-2 pb-1.5 flex items-center justify-between text-[10px] font-medium tracking-wider text-zinc-400 uppercase">
          <span>自定义笔记本</span>
          <span className="text-[10px] text-zinc-500 font-normal">未加载</span>
        </div>
        <div className="px-2 py-2 text-[11px] text-zinc-400 leading-relaxed border border-dashed border-white/5 rounded-md bg-white/[0.01]">
          暂未挂载远端笔记本树，待 Sync Kernel 接入。
        </div>
      </div>

      {/* Health Check & Status Footer */}
      <div className="p-3 border-t border-white/5 bg-[#0b0d11]">
        <div className="p-2.5 rounded-lg border border-white/5 bg-white/[0.02]">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-300">
              <Activity className="w-3.5 h-3.5 text-zinc-400" />
              <span>API 状态检测</span>
            </div>

            <button
              type="button"
              onClick={() => refetch()}
              disabled={isLoading || isFetching}
              title="重新检测后端 /api/health"
              className="p-1 rounded text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition-colors disabled:opacity-40"
              aria-label="重新检测"
            >
              <RefreshCw className={`w-3 h-3 ${isFetching ? 'animate-spin text-indigo-400' : ''}`} />
            </button>
          </div>

          {/* Health query results */}
          {isLoading || isFetching ? (
            <div className="flex items-center gap-2 text-xs text-amber-400/90 py-0.5">
              <span className="w-2 h-2 rounded-full bg-amber-400 animate-ping shrink-0" />
              <span>检测连接中...</span>
            </div>
          ) : health?.ok ? (
            <div className="space-y-1">
              <div className="flex items-center gap-1.5 text-xs text-emerald-400 font-medium">
                <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
                <span className="truncate">后端连通正常 ({health.statusCode} OK)</span>
              </div>
              <div className="text-[10px] text-zinc-400 flex items-center justify-between">
                <span>耗时: {health.latencyMs}ms</span>
                <span className="text-zinc-400">仅探活 / 保存未实现</span>
              </div>
            </div>
          ) : (
            <div className="space-y-1">
              <div className="flex items-center gap-1.5 text-xs text-rose-400 font-medium">
                <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                <span className="truncate">后端未连接</span>
              </div>
              <p className="text-[10px] text-zinc-400 line-clamp-2" title={health?.errorMessage}>
                FastAPI 未启动或代理未就绪 (127.0.0.1:8000)
              </p>
            </div>
          )}

          {/* Explicit disclaimer notice */}
          <div className="mt-2 pt-2 border-t border-white/5 flex items-start gap-1 text-[10px] text-zinc-400">
            <HelpCircle className="w-3 h-3 text-zinc-400 shrink-0 mt-0.5" />
            <span className="leading-tight">
              探活成功不代表已持久化；保存与同步功能尚未实现。
            </span>
          </div>
        </div>
      </div>
    </aside>
  )
}
