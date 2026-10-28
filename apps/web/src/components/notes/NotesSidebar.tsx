import React from 'react'
import {
  Search,
  SlidersHorizontal,
  PenSquare,
  FileCode,
  Inbox,
  Info,
} from 'lucide-react'

interface NotesSidebarProps {
  onSelectNote?: (noteId: string) => void
  selectedNoteId?: string | null
}

export const NotesSidebar: React.FC<NotesSidebarProps> = () => {
  return (
    <aside
      className="w-full lg:w-[300px] h-full flex flex-col bg-[#11141a] border-r border-white/5 select-none"
      aria-label="笔记列表"
    >
      {/* Notes List Header */}
      <div className="p-3.5 border-b border-white/5 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-xs font-semibold text-zinc-100 tracking-tight">笔记列表</h2>
          <span className="text-[10px] font-mono text-zinc-400 bg-white/5 px-1.5 py-0.5 rounded">
            未加载
          </span>
        </div>

        {/* Disabled Create Note Action */}
        <button
          type="button"
          disabled
          title="新建笔记功能未实现（等待后端持久化与同步内核）"
          className="flex items-center gap-1 px-2 py-1 rounded bg-white/5 text-zinc-400 text-xs font-medium cursor-not-allowed opacity-50"
          aria-label="新建笔记（未实现）"
        >
          <PenSquare className="w-3.5 h-3.5" />
          <span>新建</span>
        </button>
      </div>

      {/* Search and Filter Bar (Disabled placeholders) */}
      <div className="p-2.5 border-b border-white/5 space-y-2 bg-[#0f1217]">
        <div className="relative">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-400" />
          <input
            type="text"
            disabled
            placeholder="搜索笔记 (未实现)..."
            className="w-full pl-8 pr-3 py-1.5 bg-white/[0.03] border border-white/5 rounded-md text-xs text-zinc-400 placeholder:text-zinc-500 cursor-not-allowed focus:outline-none"
            aria-label="搜索笔记（功能未实现）"
          />
        </div>

        <div className="flex items-center justify-between text-[11px] text-zinc-400 px-1">
          <span className="flex items-center gap-1">
            <SlidersHorizontal className="w-3 h-3 text-zinc-400" />
            <span>按更新时间排序</span>
          </span>
          <span className="text-[10px] text-zinc-400">占位</span>
        </div>
      </div>

      {/* Empty State Body (Zero notes) */}
      <div className="flex-1 flex flex-col items-center justify-center p-6 text-center">
        <div className="w-12 h-12 rounded-xl bg-white/[0.02] border border-white/5 flex items-center justify-center text-zinc-400 mb-3 shadow-inner">
          <Inbox className="w-6 h-6 stroke-[1.5]" />
        </div>

        <h3 className="text-xs font-medium text-zinc-200 mb-1">暂无笔记</h3>
        <p className="text-[11px] text-zinc-400 leading-relaxed max-w-[220px] mb-4">
          尚未连接远端笔记库。可在右侧编辑区输入内容进行临时编辑。
        </p>

        <div className="w-full p-2.5 rounded-lg border border-white/5 bg-white/[0.015] text-left text-[11px] text-zinc-400 flex items-start gap-2">
          <Info className="w-3.5 h-3.5 text-zinc-400 shrink-0 mt-0.5" />
          <span>等待同步服务接入，暂无云端数据。</span>
        </div>
      </div>

      {/* Footer Info */}
      <div className="p-2.5 border-t border-white/5 bg-[#0e1015] flex items-center justify-between text-[10px] text-zinc-400">
        <span className="flex items-center gap-1">
          <FileCode className="w-3 h-3 text-zinc-400" />
          <span>状态: 待接入同步</span>
        </span>
        <span>草稿模式</span>
      </div>
    </aside>
  )
}
