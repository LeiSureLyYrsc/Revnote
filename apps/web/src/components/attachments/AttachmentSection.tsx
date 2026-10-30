import { useState } from 'react'
import { extractAttachmentReferences } from '../../workspace/attachmentPorts'
import { AttachmentPreview } from './AttachmentPreview'
import { buttonClass } from '../ui'

export interface AttachmentSectionProps {
  markdown: string
  fetchBlob: (attachmentId: string, signal?: AbortSignal) => Promise<Blob>
  className?: string
}

export function AttachmentSection({ markdown, fetchBlob, className = '' }: AttachmentSectionProps) {
  const references = extractAttachmentReferences(markdown)
  const [collapsed, setCollapsed] = useState(false)

  if (references.length === 0) return null

  return (
    <section
      aria-label="笔记附件预览区"
      className={`border-t border-white/5 bg-[#101319] p-3 sm:p-4 ${className}`}
    >
      <div className="flex items-center justify-between gap-2 pb-2">
        <h3 className="text-xs font-semibold text-zinc-300">
          附件预览 ({references.length})
        </h3>
        <button
          type="button"
          className={`${buttonClass} py-0.5 text-[11px]`}
          onClick={() => setCollapsed(v => !v)}
          aria-expanded={!collapsed}
        >
          {collapsed ? '展开预览' : '收起预览'}
        </button>
      </div>

      {!collapsed && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {references.map((ref, idx) => (
            <AttachmentPreview
              key={`${ref.id}-${idx}`}
              attachmentId={ref.id}
              alt={ref.alt || `附件 ${idx + 1}`}
              fetchBlob={fetchBlob}
            />
          ))}
        </div>
      )}
    </section>
  )
}
