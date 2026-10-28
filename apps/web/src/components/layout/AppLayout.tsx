import React, { useState } from 'react'
import { FolderSidebar } from '../navigation/FolderSidebar'
import { NotesSidebar } from '../notes/NotesSidebar'
import { EditorPane } from '../editor/EditorPane'
import { Folder, List, PenTool } from 'lucide-react'

type ColumnView = 'folders' | 'notes' | 'editor'

export const AppLayout: React.FC = () => {
  // Mobile / Narrow screen column switcher state
  const [activeColumn, setActiveColumn] = useState<ColumnView>('editor')

  return (
    <div className="h-screen w-screen max-w-full flex flex-col bg-[#0c0e12] text-zinc-100 overflow-hidden font-sans">
      {/* Narrow Screen Segmented Column Switcher (Visible only below lg: 1024px) */}
      <nav
        className="lg:hidden px-3 py-2 bg-[#101319] border-b border-white/5 flex items-center justify-between shrink-0"
        aria-label="栏位切换"
      >
        <div className="flex items-center gap-1.5 text-xs font-semibold text-zinc-200">
          <span className="w-2 h-2 rounded-full bg-indigo-500" />
          <span>Revnote</span>
        </div>

        <div className="flex items-center p-0.5 rounded-lg bg-white/5 border border-white/5 text-xs">
          <button
            type="button"
            onClick={() => setActiveColumn('folders')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors ${
              activeColumn === 'folders'
                ? 'bg-indigo-600 text-white font-medium shadow-xs'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
            aria-pressed={activeColumn === 'folders'}
          >
            <Folder className="w-3.5 h-3.5" />
            <span>文件夹</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveColumn('notes')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors ${
              activeColumn === 'notes'
                ? 'bg-indigo-600 text-white font-medium shadow-xs'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
            aria-pressed={activeColumn === 'notes'}
          >
            <List className="w-3.5 h-3.5" />
            <span>笔记列表</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveColumn('editor')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors ${
              activeColumn === 'editor'
                ? 'bg-indigo-600 text-white font-medium shadow-xs'
                : 'text-zinc-400 hover:text-zinc-200'
            }`}
            aria-pressed={activeColumn === 'editor'}
          >
            <PenTool className="w-3.5 h-3.5" />
            <span>编辑区</span>
          </button>
        </div>
      </nav>

      {/* Main Three-Column Workspace */}
      <div className="flex-1 flex flex-row min-h-0 overflow-hidden">
        {/* Column 1: Folder Navigation (~240px on lg, full width on small when active) */}
        <div
          className={`h-full shrink-0 ${
            activeColumn === 'folders' ? 'flex w-full lg:w-60' : 'hidden lg:flex lg:w-60'
          }`}
        >
          <FolderSidebar />
        </div>

        {/* Column 2: Notes List (300px on lg, full width on small when active) */}
        <div
          className={`h-full shrink-0 ${
            activeColumn === 'notes' ? 'flex w-full lg:w-[300px]' : 'hidden lg:flex lg:w-[300px]'
          }`}
        >
          <NotesSidebar />
        </div>

        {/* Column 3: TipTap Editor Area (flex-1 on lg, full width on small when active) */}
        <div
          className={`h-full min-w-0 ${
            activeColumn === 'editor' ? 'flex flex-1 w-full' : 'hidden lg:flex lg:flex-1'
          }`}
        >
          <EditorPane />
        </div>
      </div>
    </div>
  )
}
