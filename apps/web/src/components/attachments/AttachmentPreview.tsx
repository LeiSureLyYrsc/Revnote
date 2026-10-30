import { useEffect, useState } from 'react'
import { mapAttachmentErrorMessage } from '../../api/attachments'

export interface AttachmentPreviewProps {
  attachmentId: string
  alt?: string
  fetchBlob: (attachmentId: string, signal?: AbortSignal) => Promise<Blob>
  className?: string
}

export function AttachmentPreview({
  attachmentId,
  alt = '附件',
  fetchBlob,
  className = '',
}: AttachmentPreviewProps) {
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading')
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [mime, setMime] = useState<string>('')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    let currentUrl: string | null = null
    const controller = new AbortController()

    setStatus('loading')
    setErrorMessage(null)

    async function load() {
      try {
        const blob = await fetchBlob(attachmentId, controller.signal)
        if (!active || controller.signal.aborted) return

        currentUrl = URL.createObjectURL(blob)
        setBlobUrl(currentUrl)
        setMime(blob.type)
        setStatus('loaded')
      } catch (err) {
        if (!active || controller.signal.aborted) return

        const code = (err && typeof err === 'object' && 'code' in err && typeof (err as { code: unknown }).code === 'string')
          ? (err as { code: string }).code
          : 'api_error'
        setErrorMessage(mapAttachmentErrorMessage(code))
        setStatus('error')
      }
    }

    void load()

    return () => {
      active = false
      controller.abort()
      if (currentUrl) {
        URL.revokeObjectURL(currentUrl)
        currentUrl = null
      }
    }
  }, [attachmentId, fetchBlob])

  if (status === 'loading') {
    return (
      <div
        role="status"
        aria-live="polite"
        className={`flex min-h-24 w-full min-w-0 max-w-full items-center justify-center rounded-lg border border-white/10 bg-white/[0.02] p-4 text-xs text-zinc-400 ${className}`}
      >
        <span className="animate-pulse">正在加载附件预览…</span>
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div
        role="alert"
        className={`flex w-full min-w-0 max-w-full flex-col gap-1 rounded-lg border border-rose-500/20 bg-rose-500/10 p-3 text-xs text-rose-200 ${className}`}
      >
        <span className="font-semibold">附件预览加载失败</span>
        <span className="text-rose-300">{errorMessage || '无法获取附件内容'}</span>
      </div>
    )
  }

  if (!blobUrl) return null

  const isImage = mime.startsWith('image/')
  const isPdf = mime === 'application/pdf'

  return (
    <figure
      aria-label={alt}
      className={`my-2 flex w-full min-w-0 max-w-full flex-col overflow-hidden rounded-lg border border-white/10 bg-[#101319] p-2 sm:p-3 ${className}`}
    >
      {isImage ? (
        <div className="flex w-full items-center justify-center overflow-hidden rounded bg-black/20">
          <img
            src={blobUrl}
            alt={alt}
            data-testid="attachment-image"
            className="max-h-96 w-auto max-w-full rounded object-contain"
            loading="lazy"
          />
        </div>
      ) : isPdf ? (
        <div className="flex w-full flex-col gap-2">
          <div className="flex items-center justify-between gap-2 border-b border-white/5 pb-2 text-xs text-zinc-300">
            <span className="truncate" title={alt}>{alt}</span>
            <a
              href={blobUrl}
              download={alt.endsWith('.pdf') ? alt : `${alt}.pdf`}
              className="inline-flex shrink-0 items-center rounded border border-white/10 bg-white/5 px-2 py-1 text-xs text-indigo-300 hover:bg-white/10"
            >
              下载 PDF
            </a>
          </div>
          <iframe
            src={blobUrl}
            title={alt}
            data-testid="attachment-pdf"
            className="h-64 w-full rounded border border-white/5 bg-zinc-900 sm:h-80"
          />
        </div>
      ) : (
        <div className="flex w-full min-w-0 items-center justify-between gap-3 p-2 text-xs text-zinc-300">
          <span className="truncate font-mono" title={alt}>{alt}</span>
          <a
            href={blobUrl}
            download={alt}
            data-testid="attachment-download"
            className="inline-flex shrink-0 items-center rounded border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-indigo-300 transition-colors hover:bg-white/10"
          >
            下载文件
          </a>
        </div>
      )}
      <figcaption className="mt-2 truncate text-center text-[11px] text-zinc-400">
        {alt}
      </figcaption>
    </figure>
  )
}
