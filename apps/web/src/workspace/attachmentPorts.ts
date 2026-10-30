import {
  type AttachmentCapabilities,
  type AttachmentsApiClient,
  createAttachmentsApiClient,
  mapAttachmentErrorMessage,
  type UploadProgress,
} from '../api/attachments'
import { ApiError } from '../api/client'
import { applySourceInput } from '../components/editor/markdownAdapter'

export type { UploadProgress } from '../api/attachments'

export type UploadDisabledReason =
  | 'disabled'
  | 'offline'
  | 'read_only'
  | 'no_storage'
  | 'no_workspace'

export function uploadDisabledMessage(reason: UploadDisabledReason): string {
  switch (reason) {
    case 'disabled':
      return '当前服务未启用附件功能。'
    case 'offline':
      return '当前处于离线状态，无法上传附件。'
    case 'read_only':
      return '此工作区为只读状态，已禁用附件上传。'
    case 'no_storage':
      return '附件存储不可用，无法上传。'
    case 'no_workspace':
      return '未选择工作区，无法上传附件。'
  }
}

export interface CanUploadContext {
  capabilities: AttachmentCapabilities
  isOnline: boolean
  readOnly: boolean
  storageAvailable?: boolean
  workspaceId?: string | null
}

export interface CanUploadResult {
  allowed: boolean
  reason?: UploadDisabledReason
  message?: string
}

export function canUploadNow(context: CanUploadContext): CanUploadResult {
  if (!context.workspaceId) {
    return { allowed: false, reason: 'no_workspace', message: uploadDisabledMessage('no_workspace') }
  }
  if (context.readOnly) {
    return { allowed: false, reason: 'read_only', message: uploadDisabledMessage('read_only') }
  }
  if (!context.isOnline) {
    return { allowed: false, reason: 'offline', message: uploadDisabledMessage('offline') }
  }
  if (!context.capabilities.enabled) {
    return { allowed: false, reason: 'disabled', message: uploadDisabledMessage('disabled') }
  }
  if (context.storageAvailable === false) {
    return { allowed: false, reason: 'no_storage', message: uploadDisabledMessage('no_storage') }
  }
  return { allowed: true }
}

export type FileValidationResult =
  | { valid: true }
  | {
      valid: false
      reason: 'attachment_unsupported_type' | 'attachment_too_large' | 'empty_file'
      message: string
    }

export function validateAttachmentFile(
  file: { name: string; size: number; type: string },
  capabilities: AttachmentCapabilities
): FileValidationResult {
  if (!capabilities.enabled) {
    return {
      valid: false,
      reason: 'attachment_unsupported_type',
      message: uploadDisabledMessage('disabled'),
    }
  }

  if (file.size <= 0) {
    return {
      valid: false,
      reason: 'empty_file',
      message: '所选文件为空，无法上传。',
    }
  }

  if (file.size > capabilities.maxBytes) {
    return {
      valid: false,
      reason: 'attachment_too_large',
      message: mapAttachmentErrorMessage('attachment_too_large'),
    }
  }

  const normalizedMime = (file.type || '').trim().toLowerCase()
  const isAccepted = capabilities.acceptedMime.some(
    mime => mime.toLowerCase() === normalizedMime
  )

  if (!isAccepted) {
    return {
      valid: false,
      reason: 'attachment_unsupported_type',
      message: mapAttachmentErrorMessage('attachment_unsupported_type'),
    }
  }

  return { valid: true }
}

export function formatAttachmentMarkdown(attachmentId: string, altText?: string): string {
  const sanitizedAlt = (altText ?? '')
    .replace(/[[\]]/g, '')
    .replace(/[\r\n]/g, ' ')
    .trim()
  const alt = sanitizedAlt || '附件'
  return `![${alt}](/api/v1/attachments/${attachmentId}/content)`
}

export interface InsertMarkdownResult {
  nextBody: string
  newCursor: number
}

export function insertMarkdownSnippet(
  original: string,
  snippet: string,
  selection?: { start: number; end: number }
): InsertMarkdownResult {
  const isCrlf = original.includes('\r\n') && !original.replace(/\r\n/g, '').includes('\n')
  const newline = isCrlf ? '\r\n' : '\n'

  if (!selection) {
    if (!original) {
      return { nextBody: snippet, newCursor: snippet.length }
    }
    const endsWithDouble = original.endsWith(newline + newline)
    const endsWithSingle = original.endsWith(newline)
    const glue = endsWithDouble ? '' : endsWithSingle ? newline : newline + newline
    const nextBody = original + glue + snippet + newline
    return { nextBody, newCursor: nextBody.length }
  }

  const start = Math.max(0, Math.min(selection.start, original.length))
  const end = Math.max(start, Math.min(selection.end, original.length))

  const before = original.slice(0, start)
  const after = original.slice(end)
  const rawMerged = before + snippet + after
  const nextBody = applySourceInput(original, rawMerged)
  const newCursor = start + snippet.length

  return { nextBody, newCursor }
}

export interface AttachmentReference {
  id: string
  alt: string
  matchedText: string
}

export function extractAttachmentReferences(markdown: string): AttachmentReference[] {
  const pattern = /!\[([^\]]*)\]\(\/api\/v1\/attachments\/([0-9a-fA-F-]{36})\/content\)/g
  const references: AttachmentReference[] = []
  let match: RegExpExecArray | null

  while ((match = pattern.exec(markdown)) !== null) {
    references.push({
      alt: match[1],
      id: match[2].toLowerCase(),
      matchedText: match[0],
    })
  }

  return references
}

export interface AttachmentPort {
  getCapabilities(): Promise<AttachmentCapabilities>
  canUpload(context?: { isOnline?: boolean; readOnly?: boolean }): Promise<CanUploadResult>
  upload(
    file: File,
    options?: {
      onProgress?: (progress: UploadProgress) => void
      signal?: AbortSignal
    }
  ): Promise<{ attachmentId: string; markdownSnippet: string }>
  fetchBlob(attachmentId: string, signal?: AbortSignal): Promise<Blob>
}

export interface AttachmentPortOptions {
  workspaceId: string
  getAccessToken?: () => string | null | undefined | Promise<string | null | undefined>
  api?: AttachmentsApiClient
  readOnly?: boolean
  isOnline?: () => boolean
}

export function createAttachmentPort(options: AttachmentPortOptions): AttachmentPort {
  const api = options.api ?? createAttachmentsApiClient()
  const isOnlineFn = options.isOnline ?? (() => (typeof navigator !== 'undefined' ? navigator.onLine : true))
  let cachedCapabilities: AttachmentCapabilities | null = null

  async function resolveCapabilities(): Promise<AttachmentCapabilities> {
    if (cachedCapabilities) return cachedCapabilities
    cachedCapabilities = await api.getCapabilities()
    return cachedCapabilities
  }

  return {
    async getCapabilities(): Promise<AttachmentCapabilities> {
      return await resolveCapabilities()
    },

    async canUpload(context?: { isOnline?: boolean; readOnly?: boolean }): Promise<CanUploadResult> {
      const isOnline = context?.isOnline ?? isOnlineFn()
      const readOnly = context?.readOnly ?? options.readOnly ?? false

      if (!options.workspaceId) {
        return { allowed: false, reason: 'no_workspace', message: uploadDisabledMessage('no_workspace') }
      }
      if (readOnly) {
        return { allowed: false, reason: 'read_only', message: uploadDisabledMessage('read_only') }
      }
      if (!isOnline) {
        return { allowed: false, reason: 'offline', message: uploadDisabledMessage('offline') }
      }

      const caps = await resolveCapabilities()
      return canUploadNow({
        capabilities: caps,
        isOnline,
        readOnly,
        workspaceId: options.workspaceId,
      })
    },

    async upload(
      file: File,
      uploadOptions?: {
        onProgress?: (progress: UploadProgress) => void
        signal?: AbortSignal
      }
    ): Promise<{ attachmentId: string; markdownSnippet: string }> {
      const isOnline = isOnlineFn()
      const readOnly = options.readOnly ?? false
      const caps = await resolveCapabilities()

      const gate = canUploadNow({
        capabilities: caps,
        isOnline,
        readOnly,
        workspaceId: options.workspaceId,
      })
      if (!gate.allowed) {
        throw new ApiError(gate.reason ?? 'upload_disabled', 0, false, false)
      }

      const validation = validateAttachmentFile(file, caps)
      if (!validation.valid) {
        throw new ApiError(validation.reason, 0, false, false)
      }

      const token = await options.getAccessToken?.()
      const pending = await api.create(
        options.workspaceId,
        {
          mime: file.type,
          byteSize: file.size,
          fileName: file.name,
        },
        token
      )

      const ready = await api.upload({
        uploadUrl: pending.upload.url,
        mime: file.type,
        content: file,
        accessToken: token,
        onProgress: uploadOptions?.onProgress,
        signal: uploadOptions?.signal,
      })

      const markdownSnippet = formatAttachmentMarkdown(ready.attachmentId, file.name)
      return { attachmentId: ready.attachmentId, markdownSnippet }
    },

    async fetchBlob(attachmentId: string, signal?: AbortSignal): Promise<Blob> {
      const token = await options.getAccessToken?.()
      return await api.getContent(attachmentId, token, signal)
    },
  }
}
