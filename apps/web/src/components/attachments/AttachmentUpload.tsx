import { useEffect, useRef, useState } from 'react'
import type { AttachmentPort, CanUploadResult, UploadProgress } from '../../workspace/attachmentPorts'
import { mapAttachmentErrorMessage } from '../../api/attachments'
import { buttonClass } from '../ui'

export interface AttachmentUploadProps {
  port: AttachmentPort
  readOnly?: boolean
  onInsert: (markdownSnippet: string) => void
  className?: string
}

export function AttachmentUpload({
  port,
  readOnly = false,
  onInsert,
  className = '',
}: AttachmentUploadProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  const [gate, setGate] = useState<CanUploadResult>({ allowed: false, reason: 'disabled' })
  const [checkingGate, setCheckingGate] = useState(true)
  const [acceptedMime, setAcceptedMime] = useState<readonly string[]>([])

  const [uploadStatus, setUploadStatus] = useState<'idle' | 'uploading' | 'error'>('idle')
  const [progress, setProgress] = useState<UploadProgress>({ loaded: 0, total: 100 })
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [statusAnnouncement, setStatusAnnouncement] = useState<string>('')

  // Query capabilities and online/read-only status
  useEffect(() => {
    let active = true

    async function check() {
      const isOnline = typeof navigator !== 'undefined' ? navigator.onLine : true
      if (readOnly) {
        if (!active) return
        setGate({ allowed: false, reason: 'read_only', message: '此工作区为只读状态，已禁用附件上传。' })
        setCheckingGate(false)
        return
      }
      if (!isOnline) {
        if (!active) return
        setGate({ allowed: false, reason: 'offline', message: '当前处于离线状态，无法上传附件。' })
        setCheckingGate(false)
        return
      }

      setCheckingGate(true)
      try {
        const result = await port.canUpload({ readOnly })
        if (!active) return
        setGate(result)
        if (result.allowed) {
          const caps = await port.getCapabilities()
          if (!active) return
          setAcceptedMime(caps.acceptedMime)
        }
      } catch {
        if (!active) return
        setGate({ allowed: false, reason: 'disabled', message: '当前服务未启用附件功能。' })
      } finally {
        if (active) setCheckingGate(false)
      }
    }

    void check()

    const updateGate = () => { void check() }
    window.addEventListener('online', updateGate)
    window.addEventListener('offline', updateGate)

    return () => {
      active = false
      window.removeEventListener('online', updateGate)
      window.removeEventListener('offline', updateGate)
    }
  }, [port, readOnly])

  const handlePickClick = () => {
    if (!gate.allowed || uploadStatus === 'uploading') return
    setErrorMessage(null)
    fileInputRef.current?.click()
  }

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    // Reset file input value so selecting the same file again triggers change
    event.target.value = ''
    if (!file) return

    setErrorMessage(null)
    setUploadStatus('uploading')
    setProgress({ loaded: 0, total: file.size || 100 })
    setStatusAnnouncement(`开始上传附件 ${file.name}`)

    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      const result = await port.upload(file, {
        onProgress: (p) => {
          setProgress(p)
          const pct = Math.round((p.loaded / (p.total || 1)) * 100)
          setStatusAnnouncement(`正在上传 ${pct}%`)
        },
        signal: controller.signal,
      })

      setUploadStatus('idle')
      setStatusAnnouncement('附件上传成功，已插入引用')
      onInsert(result.markdownSnippet)
    } catch (err) {
      if (controller.signal.aborted) {
        setUploadStatus('idle')
        setStatusAnnouncement('已取消附件上传')
        return
      }

      setUploadStatus('error')
      const code = (err && typeof err === 'object' && 'code' in err && typeof (err as { code: unknown }).code === 'string')
        ? (err as { code: string }).code
        : 'api_error'
      const msg = mapAttachmentErrorMessage(code)
      setErrorMessage(msg)
      setStatusAnnouncement(`上传失败: ${msg}`)
    } finally {
      abortControllerRef.current = null
    }
  }

  const handleCancel = () => {
    abortControllerRef.current?.abort()
    setUploadStatus('idle')
  }

  const isDisabled = checkingGate || !gate.allowed || uploadStatus === 'uploading'
  const percent = progress.total > 0 ? Math.round((progress.loaded / progress.total) * 100) : 0

  return (
    <div className={`inline-flex flex-wrap items-center gap-2 ${className}`}>
      <input
        ref={fileInputRef}
        type="file"
        aria-label="选择附件文件"
        data-testid="attachment-file-input"
        className="sr-only"
        accept={acceptedMime.join(',')}
        tabIndex={-1}
        onChange={handleFileChange}
      />

      <button
        type="button"
        className={buttonClass}
        disabled={isDisabled}
        aria-label="上传附件"
        data-testid="upload-attachment-button"
        title={!gate.allowed ? gate.message : '上传附件并插入 Markdown 引用'}
        onClick={handlePickClick}
      >
        {uploadStatus === 'uploading' ? '正在上传…' : '上传附件'}
      </button>

      {/* Uploading progress indicator */}
      {uploadStatus === 'uploading' && (
        <div
          role="group"
          aria-label="附件上传进度"
          className="flex min-w-0 max-w-full items-center gap-2 rounded-md border border-indigo-500/30 bg-indigo-500/10 px-2 py-1 text-xs text-indigo-200"
        >
          <div
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="上传百分比"
            className="h-1.5 w-16 overflow-hidden rounded-full bg-indigo-950 sm:w-24"
          >
            <div
              className="h-full bg-indigo-400 transition-all duration-150"
              style={{ width: `${percent}%` }}
            />
          </div>
          <span className="font-mono text-[11px]">{percent}%</span>
          <button
            type="button"
            className="rounded px-1 text-[11px] text-zinc-300 hover:text-white"
            onClick={handleCancel}
            aria-label="取消上传"
          >
            取消
          </button>
        </div>
      )}

      {/* Explicit disabled reason message */}
      {!gate.allowed && gate.message && (
        <span
          role="status"
          data-testid="upload-disabled-reason"
          className="max-w-xs truncate text-xs text-amber-300/80"
          title={gate.message}
        >
          {gate.message}
        </span>
      )}

      {/* User-visible error alert */}
      {uploadStatus === 'error' && errorMessage && (
        <div
          role="alert"
          data-testid="upload-error-alert"
          className="flex items-center gap-1.5 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-xs text-rose-200"
        >
          <span>{errorMessage}</span>
          <button
            type="button"
            className="ml-1 text-rose-400 hover:text-rose-100"
            onClick={() => setErrorMessage(null)}
            aria-label="关闭错误提示"
          >
            ✕
          </button>
        </div>
      )}

      {/* Screen-reader live status region */}
      <span className="sr-only" role="status" aria-live="polite">
        {statusAnnouncement}
      </span>
    </div>
  )
}
