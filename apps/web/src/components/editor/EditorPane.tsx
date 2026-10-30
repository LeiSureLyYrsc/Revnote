import { useEffect, useRef, useState } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import { editorExtensions } from './extensions'
import { applySourceInput, downloadMarkdown } from './markdownAdapter'
import { useNoteDraft, type NoteDocument } from '../../workspace/useNoteDraft'
import type { WorkspacePort } from '../../workspace/ports'
import {
  type AttachmentPort,
  createAttachmentPort,
  insertMarkdownSnippet,
} from '../../workspace/attachmentPorts'
import { AttachmentUpload, AttachmentSection } from '../attachments'
import { useSessionController } from '../../auth/SessionProvider'
import type { SessionController } from '../../auth/session'
import { buttonClass, inputClass } from '../ui'

export interface EditorPaneProps {
  port: WorkspacePort
  note: NoteDocument
  attachmentPort?: AttachmentPort
}

function RichEditor({ bodyMd, readOnly, onChange }: { bodyMd: string; readOnly: boolean; onChange(body: string): void }) {
  const loaded = useRef(bodyMd)
  const userEdit = useRef(false)
  // Only mounted after the original source recovery point has committed.
  const editor = useEditor({
    extensions: editorExtensions,
    content: bodyMd,
    contentType: 'markdown',
    editable: !readOnly,
    editorProps: {
      attributes: { role: 'textbox', 'aria-label': '富文本正文', 'aria-multiline': 'true', class: 'tiptap min-h-80 p-4 sm:p-8' },
      handleDOMEvents: {
        beforeinput: () => { userEdit.current = true; return false },
        paste: () => { userEdit.current = true; return false },
        drop: () => { userEdit.current = true; return false },
      },
      handleKeyDown: (_view, event) => {
        if (event.key.length === 1 || ['Enter', 'Backspace', 'Delete'].includes(event.key)) userEdit.current = true
        return false
      },
    },
    onUpdate: ({ editor }) => {
      // TipTap's Markdown load may emit an update. Only real editing gestures
      // or explicit toolbar actions can serialize the source of truth.
      if (!userEdit.current || readOnly) return
      userEdit.current = false
      const next = editor.getMarkdown(); loaded.current = next; onChange(next)
    },
  })
  useEffect(() => { editor?.setEditable(!readOnly) }, [editor, readOnly])
  useEffect(() => {
    if (editor && loaded.current !== bodyMd) {
      loaded.current = bodyMd
      editor.commands.setContent(bodyMd, { contentType: 'markdown', emitUpdate: false })
    }
  }, [editor, bodyMd])
  return <div className="flex min-h-0 flex-1 flex-col">
    <div role="toolbar" aria-label="富文本格式" className="flex flex-wrap gap-2 border-b border-white/5 p-2">
      <button type="button" className={buttonClass} disabled={readOnly || !editor} onClick={() => { userEdit.current = true; editor?.chain().focus().toggleBold().run() }}>粗体</button>
      <button type="button" className={buttonClass} disabled={readOnly || !editor} onClick={() => { userEdit.current = true; editor?.chain().focus().toggleHeading({ level: 2 }).run() }}>二级标题</button>
      <button type="button" className={buttonClass} disabled={readOnly || !editor} onClick={() => { userEdit.current = true; editor?.chain().focus().undo().run() }}>撤销</button>
    </div>
    <EditorContent editor={editor} className="flex-1 overflow-y-auto" />
  </div>
}

export function EditorPane({ port, note, attachmentPort }: EditorPaneProps) {
  const { document, status, updateTitle, updateBody, retry } = useNoteDraft(port, note)
  const [mode, setMode] = useState<'source' | 'rich'>('source')
  const [confirmConversion, setConfirmConversion] = useState(false)
  const [conversionBusy, setConversionBusy] = useState(false)
  const [conversionError, setConversionError] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')
  const [inspect, setInspect] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  let sessionController: SessionController | null = null
  try {
    // eslint-disable-next-line react-hooks/rules-of-hooks
    sessionController = useSessionController()
  } catch {
    // Graceful fallback when rendered outside SessionProvider (e.g. testing)
  }

  const defaultAttachmentPort = useRef<AttachmentPort | null>(null)
  if (!defaultAttachmentPort.current) {
    defaultAttachmentPort.current = createAttachmentPort({
      workspaceId: port.partition.workspaceId,
      readOnly: port.readOnly,
      getAccessToken: sessionController ? () => sessionController.getAccessToken() : undefined,
    })
  }
  const resolvedAttachmentPort = attachmentPort ?? defaultAttachmentPort.current

  const handleInsertAttachment = (snippet: string) => {
    const textarea = textareaRef.current
    if (mode === 'source' && textarea) {
      const selection = {
        start: textarea.selectionStart ?? document.bodyMd.length,
        end: textarea.selectionEnd ?? document.bodyMd.length,
      }
      const { nextBody, newCursor } = insertMarkdownSnippet(document.bodyMd, snippet, selection)
      updateBody(nextBody)
      requestAnimationFrame(() => {
        textarea.focus()
        textarea.setSelectionRange(newCursor, newCursor)
      })
    } else {
      const { nextBody } = insertMarkdownSnippet(document.bodyMd, snippet)
      updateBody(nextBody)
    }
  }

  return <main aria-label="笔记编辑区" className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-[#14171e]">
    <header className="shrink-0 space-y-3 border-b border-white/5 bg-[#12151b] p-3 sm:p-5">
      <label className="block"><span className="sr-only">笔记标题</span><input className={`${inputClass} border-transparent bg-transparent font-semibold`} aria-label="笔记标题" data-testid="note-title" value={document.title} readOnly={port.readOnly} onChange={event => updateTitle(event.target.value)} /></label>
      <div className="flex flex-wrap items-center gap-2">
        <button className={buttonClass} type="button" aria-pressed={mode === 'source'} onClick={() => setMode('source')}>Markdown 源码</button>
        <button className={buttonClass} type="button" disabled={port.readOnly || conversionBusy || status !== 'saved'} onClick={() => setConfirmConversion(true)}>转换为富文本</button>
        <button className={buttonClass} type="button" aria-pressed={inspect} onClick={() => setInspect(value => !value)}>查看原文</button>
        <button className={buttonClass} type="button" onClick={async () => { try { await navigator.clipboard.writeText(document.bodyMd); setCopyStatus('已复制原文') } catch { setCopyStatus('复制未完成，请选择源码复制或导出') } }}>复制 Markdown</button>
        <button className={buttonClass} type="button" onClick={() => downloadMarkdown(document.title, document.bodyMd)}>导出 Markdown</button>
        <AttachmentUpload port={resolvedAttachmentPort} readOnly={port.readOnly} onInsert={handleInsertAttachment} />
      </div>
      <p className="text-xs leading-relaxed text-zinc-400">{mode === 'source' ? 'Markdown 原文为准。打开、查看原文和仅修改标题不会重新生成正文。' : '富文本编辑可能规范化不支持的语法。转换前原文已保留在恢复记录中。'}</p>
      {port.readOnly && <p role="status" className="text-xs text-amber-300">此标签页只读，编辑与凭据变更已锁定。</p>}
      {confirmConversion && <div role="group" aria-label="确认富文本转换" className="space-y-2 rounded border border-amber-500/30 p-3 text-xs text-amber-200">
        <p>富文本无法保证保留所有 Markdown 语法。先保存原文恢复点，再进入富文本；只有实际编辑才会生成新的正文。</p>
        <div className="flex flex-wrap gap-2"><button className={buttonClass} type="button" disabled={port.readOnly || conversionBusy} onClick={async () => { setConversionBusy(true); setConversionError(false); try { await port.preserveSource(note.id, document.title, document.bodyMd); setMode('rich'); setConfirmConversion(false) } catch { setConversionError(true) } finally { setConversionBusy(false) } }}>保存恢复点并转换</button><button className={buttonClass} type="button" disabled={conversionBusy} onClick={() => setConfirmConversion(false)}>取消转换</button></div>
        {conversionError && <p role="alert">未能保存原文恢复点，已阻止转换。</p>}
      </div>}
    </header>
    {status === 'failed' && <div role="alert" className="shrink-0 space-y-2 border-b border-rose-500/20 bg-rose-500/10 p-3 text-xs text-rose-200"><p>本地写入失败。当前编辑仍保留在此页面；离开或刷新前请复制或导出。</p><button className={buttonClass} type="button" disabled={port.readOnly} onClick={retry}>重试本地保存</button></div>}
    {inspect ? <pre aria-label="Markdown 原文预览" className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-sm text-zinc-300 sm:p-8">{document.bodyMd}</pre> : mode === 'source' ? <textarea ref={textareaRef} aria-label="Markdown 正文" data-testid="note-source" spellCheck={false} readOnly={port.readOnly} value={document.bodyMd} onChange={event => updateBody(applySourceInput(document.bodyMd, event.target.value))} className="min-h-0 w-full flex-1 resize-none bg-transparent p-4 font-mono text-sm leading-7 text-zinc-200 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-indigo-400 sm:p-8" placeholder="在这里输入 Markdown…" /> : <RichEditor bodyMd={document.bodyMd} readOnly={port.readOnly} onChange={updateBody} />}
    <AttachmentSection markdown={document.bodyMd} fetchBlob={resolvedAttachmentPort.fetchBlob} />
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-white/5 bg-[#0f1217] px-3 py-2 text-xs text-zinc-400">
      <span role="status" data-testid="local-save-status">{status === 'writing' ? '正在写入本地…' : status === 'failed' ? '尚未写入本地' : '已写入本地'}</span><span role="status">{copyStatus}</span><span>{document.bodyMd.length} 字符</span>
    </footer>
  </main>
}
