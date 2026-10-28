import React, { useState, useEffect } from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import { editorExtensions } from './extensions'
import {
  Bold,
  Italic,
  Strikethrough,
  Code,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  Quote,
  SquareCode,
  Undo2,
  Redo2,
  Save,
  Cloud,
  GitBranch,
  Copy,
  Check,
  AlertTriangle,
  FileText,
  Info,
} from 'lucide-react'

export const EditorPane: React.FC = () => {
  const [copied, setCopied] = useState(false)
  const [showConflictInfo, setShowConflictInfo] = useState(false)
  const [markdownView, setMarkdownView] = useState(false)
  const [markdownOutput, setMarkdownOutput] = useState('')
  const [title, setTitle] = useState('无标题笔记（内存草稿）')

  const editor = useEditor({
    extensions: editorExtensions,
    // Clean empty initial content as required: "首屏无示例正文/虚构笔记数"
    content: '',
    editorProps: {
      attributes: {
        role: 'textbox',
        'aria-label': '笔记正文编辑区',
        'aria-multiline': 'true',
        class:
          'tiptap prose prose-invert max-w-none focus:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/40 focus-visible:ring-offset-2 focus-visible:ring-offset-[#14171e] min-h-[450px] px-4 sm:px-8 py-6 text-zinc-200 text-base leading-relaxed',
        'data-placeholder': '在此开始输入 Markdown 正文...（内容仅保留在当前页面内存中）',
      },
    },
    onUpdate: ({ editor }) => {
      if (markdownView) {
        setMarkdownOutput(editor.getMarkdown())
      }
    },
  })

  useEffect(() => {
    if (markdownView && editor) {
      setMarkdownOutput(editor.getMarkdown())
    }
  }, [markdownView, editor])

  const handleCopyMarkdown = async () => {
    if (!editor) return
    const md = editor.getMarkdown()
    try {
      await navigator.clipboard.writeText(md)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Fallback
    }
  }

  // Word count and char count stats
  const textContent = editor?.getText() || ''
  const charCount = textContent.length
  const wordCount = textContent.trim() ? textContent.trim().split(/\s+/).length : 0

  return (
    <main
      className="flex-1 h-full flex flex-col bg-[#14171e] min-w-0 max-w-full overflow-x-hidden overflow-y-auto"
      aria-label="笔记编辑区"
    >
      {/* Prominent but restrained warning banner */}
      <div
        className="px-3 sm:px-4 py-2 bg-amber-500/10 border-b border-amber-500/20 text-amber-200/90 text-xs flex items-center justify-between shrink-0"
        role="alert"
      >
        <div className="flex items-center gap-2 min-w-0">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
          <span className="font-medium truncate sm:whitespace-normal">
            演示编辑区，仅内存内容，刷新即丢失；保存与同步未实现。
          </span>
        </div>
        <span className="text-[11px] text-amber-300/70 hidden sm:inline-block shrink-0 ml-2">
          单机内存模式
        </span>
      </div>

      {/* Editor Top Bar: Title & Actions (Responsive layout for 390px mobile) */}
      <div className="px-3 sm:px-6 py-2.5 sm:py-3 border-b border-white/5 bg-[#12151b] flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 shrink-0 max-w-full">
        <div className="w-full sm:flex-1 min-w-0">
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full bg-transparent text-sm sm:text-base font-semibold text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-indigo-500/30 rounded px-1 -mx-1"
            placeholder="输入笔记标题..."
            aria-label="笔记标题"
          />
        </div>

        {/* Action Controls: Compact and responsive */}
        <div className="flex items-center gap-1.5 sm:gap-2 shrink-0 flex-wrap sm:flex-nowrap">
          {/* Neutral Conflict Placeholder */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowConflictInfo((prev) => !prev)}
              className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium border border-white/5 bg-white/[0.02] text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition-colors"
              title="冲突状态详情"
              aria-expanded={showConflictInfo}
            >
              <GitBranch className="w-3.5 h-3.5 text-zinc-500" />
              <span className="hidden md:inline">冲突：</span>
              <span className="text-zinc-400">未接入</span>
            </button>

            {/* Neutral Conflict Info Popover */}
            {showConflictInfo && (
              <div className="absolute right-0 top-full mt-2 w-72 p-3 rounded-lg border border-white/10 bg-[#161a22] shadow-xl text-left z-20">
                <div className="flex items-center gap-1.5 text-xs font-medium text-zinc-200 mb-1.5">
                  <Info className="w-3.5 h-3.5 text-indigo-400" />
                  <span>冲突状态：尚未接入</span>
                </div>
                <p className="text-[11px] text-zinc-400 leading-relaxed mb-2">
                  Revnote 后续将由 Sync Kernel 分配严格递增的序列号并进行 CAS 乐观并发校验。尚未接入冲突检测与三方合并。
                </p>
                <div className="text-[10px] text-zinc-500 border-t border-white/5 pt-1.5">
                  当前为纯单机内存演示，未连接远端同步服务。
                </div>
              </div>
            )}
          </div>

          {/* Markdown Output / Raw View Toggle */}
          <button
            type="button"
            onClick={() => setMarkdownView((prev) => !prev)}
            className={`px-2 py-1 sm:px-2.5 rounded-md text-xs font-medium border transition-colors ${
              markdownView
                ? 'bg-indigo-500/20 border-indigo-500/40 text-indigo-200'
                : 'bg-white/[0.02] border-white/5 text-zinc-400 hover:text-zinc-200'
            }`}
            title="切换 Markdown 源码视图"
          >
            {markdownView ? '富文本' : 'Markdown'}
          </button>

          {/* Copy Markdown */}
          <button
            type="button"
            onClick={handleCopyMarkdown}
            disabled={!editor}
            className="flex items-center gap-1 px-2 py-1 sm:px-2.5 rounded-md text-xs font-medium border border-white/5 bg-white/[0.02] text-zinc-300 hover:text-white hover:bg-white/5 transition-colors"
            title="复制 TipTap 生成的 Markdown 到剪贴板"
          >
            {copied ? (
              <>
                <Check className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-emerald-400">已复制</span>
              </>
            ) : (
              <>
                <Copy className="w-3.5 h-3.5 text-zinc-400" />
                <span className="hidden sm:inline">复制 MD</span>
                <span className="sm:hidden">复制</span>
              </>
            )}
          </button>

          {/* Disabled Save Action */}
          <button
            type="button"
            disabled
            title="保存功能未实现（仅内存内容，刷新即丢失）"
            className="flex items-center gap-1 px-2 py-1 sm:px-2.5 rounded-md text-xs font-medium bg-white/5 text-zinc-500 border border-white/5 cursor-not-allowed opacity-50"
            aria-label="保存（未实现）"
          >
            <Save className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">保存</span>
          </button>

          {/* Disabled Sync Action */}
          <button
            type="button"
            disabled
            title="同步功能未实现（等待后端同步内核服务）"
            className="flex items-center gap-1 px-2 py-1 sm:px-2.5 rounded-md text-xs font-medium bg-white/5 text-zinc-500 border border-white/5 cursor-not-allowed opacity-50"
            aria-label="同步（未实现）"
          >
            <Cloud className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">同步</span>
          </button>
        </div>
      </div>

      {/* TipTap Formatting Toolbar */}
      {!markdownView && editor && (
        <div
          className="px-3 sm:px-6 py-1.5 sm:py-2 border-b border-white/5 bg-[#101319] flex items-center gap-0.5 sm:gap-1 overflow-x-auto shrink-0 select-none"
          role="toolbar"
          aria-label="文本格式化工具栏"
        >
          {/* Headings */}
          <button
            type="button"
            onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('heading', { level: 1 })
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="一级标题 (H1)"
            aria-label="一级标题"
          >
            <Heading1 className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('heading', { level: 2 })
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="二级标题 (H2)"
            aria-label="二级标题"
          >
            <Heading2 className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('heading', { level: 3 })
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="三级标题 (H3)"
            aria-label="三级标题"
          >
            <Heading3 className="w-4 h-4" />
          </button>

          <div className="w-[1px] h-4 bg-white/10 mx-1 shrink-0" />

          {/* Inline Marks */}
          <button
            type="button"
            onClick={() => editor.chain().focus().toggleBold().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('bold')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="粗体 (Ctrl+B)"
            aria-label="粗体"
          >
            <Bold className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleItalic().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('italic')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="斜体 (Ctrl+I)"
            aria-label="斜体"
          >
            <Italic className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleStrike().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('strike')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="删除线"
            aria-label="删除线"
          >
            <Strikethrough className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleCode().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('code')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="行内代码"
            aria-label="行内代码"
          >
            <Code className="w-4 h-4" />
          </button>

          <div className="w-[1px] h-4 bg-white/10 mx-1 shrink-0" />

          {/* Blocks */}
          <button
            type="button"
            onClick={() => editor.chain().focus().toggleBulletList().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('bulletList')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="无序列表"
            aria-label="无序列表"
          >
            <List className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleOrderedList().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('orderedList')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="有序列表"
            aria-label="有序列表"
          >
            <ListOrdered className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleBlockquote().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('blockquote')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="引用块"
            aria-label="引用块"
          >
            <Quote className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().toggleCodeBlock().run()}
            className={`p-1.5 rounded text-xs font-medium transition-colors ${
              editor.isActive('codeBlock')
                ? 'bg-indigo-500/20 text-indigo-300'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/5'
            }`}
            title="代码块"
            aria-label="代码块"
          >
            <SquareCode className="w-4 h-4" />
          </button>

          <div className="w-[1px] h-4 bg-white/10 mx-1 shrink-0" />

          {/* History */}
          <button
            type="button"
            onClick={() => editor.chain().focus().undo().run()}
            disabled={!editor.can().undo()}
            className="p-1.5 rounded text-xs font-medium text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            title="撤销 (Ctrl+Z)"
            aria-label="撤销"
          >
            <Undo2 className="w-4 h-4" />
          </button>

          <button
            type="button"
            onClick={() => editor.chain().focus().redo().run()}
            disabled={!editor.can().redo()}
            className="p-1.5 rounded text-xs font-medium text-zinc-400 hover:text-zinc-200 hover:bg-white/5 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            title="重做 (Ctrl+Y)"
            aria-label="重做"
          >
            <Redo2 className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Editor Content Body */}
      <div className="flex-1 overflow-y-auto">
        {markdownView ? (
          <div className="p-4 sm:p-8 font-mono text-xs text-zinc-300 whitespace-pre-wrap leading-relaxed select-text">
            {markdownOutput || '（当前 Markdown 正文为空）'}
          </div>
        ) : (
          <div className="h-full">
            <EditorContent editor={editor} className="h-full" />
          </div>
        )}
      </div>

      {/* Bottom Status Bar */}
      <div className="px-3 sm:px-6 py-2 border-t border-white/5 bg-[#0f1217] flex items-center justify-between text-[11px] text-zinc-400 shrink-0 select-none">
        <div className="flex items-center gap-2 sm:gap-3">
          <span className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400/80" />
            <span>内存临时草稿</span>
          </span>
          <span className="hidden sm:inline-block">·</span>
          <span className="hidden sm:inline-block">刷新页面内容将丢失</span>
        </div>

        <div className="flex items-center gap-2 sm:gap-3 font-mono">
          <span>{wordCount} 词</span>
          <span>·</span>
          <span>{charCount} 字符</span>
          <span className="hidden md:inline-block">·</span>
          <span className="hidden md:inline-block text-zinc-400 flex items-center gap-1">
            <FileText className="w-3 h-3" />
            <span>TipTap Markdown</span>
          </span>
        </div>
      </div>
    </main>
  )
}
