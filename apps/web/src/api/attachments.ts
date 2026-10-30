import { ApiClient, apiClient, ApiError, decodeErrorBody } from './client'

export interface AttachmentCapabilities {
  enabled: boolean
  maxBytes: number
  acceptedMime: readonly string[]
}

export const ATTACHMENTS_DISABLED_CAPABILITIES: Readonly<AttachmentCapabilities> = Object.freeze({
  enabled: false,
  maxBytes: 0,
  acceptedMime: Object.freeze([]),
})

export interface CreateAttachmentInput {
  mime: string
  byteSize?: number
  fileName?: string
}

export interface PendingAttachment {
  attachmentId: string
  state: 'pending'
  upload: {
    method: 'PUT'
    url: string
  }
  expiresAt: string
}

export interface ReadyAttachment {
  attachmentId: string
  state: 'ready'
  contentHash: string
  byteSize: number
}

export interface AttachmentMetadata {
  id: string
  workspaceId: string
  state: 'pending' | 'ready' | 'deleted'
  mime: string
  byteSize: number | null
  contentHash: string | null
  createdAt: string
  readyAt: string | null
  fileName: string | null
}

export interface UploadProgress {
  loaded: number
  total: number
}

export interface UploadAttachmentOptions {
  uploadUrl: string
  mime: string
  content: Blob | File | ArrayBuffer | Uint8Array
  contentSha256?: string
  accessToken?: string | null
  onProgress?: (progress: UploadProgress) => void
  signal?: AbortSignal
}

function invalid(): never { throw new ApiError('invalid_response', 0, false, true) }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
  return value as Record<string, unknown>
}
function text(value: unknown): string { return typeof value === 'string' ? value : invalid() }
function nonempty(value: unknown): string { const result = text(value); return result ? result : invalid() }
function bool(value: unknown): boolean { return typeof value === 'boolean' ? value : invalid() }
function uuid(value: unknown): string {
  const result = text(value)
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result) ? result : invalid()
}
function date(value: unknown): string {
  const result = text(value)
  return /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(result) && Number.isFinite(Date.parse(result)) ? result : invalid()
}
function array<T>(value: unknown, decode: (item: unknown) => T): T[] {
  return Array.isArray(value) ? value.map(decode) : invalid()
}
function oneOf<T extends string>(value: unknown, values: readonly T[]): T {
  return values.includes(value as T) ? value as T : invalid()
}
function nullable<T>(value: unknown, decode: (item: unknown) => T): T | null {
  return value === null ? null : decode(value)
}

export function decodeCapabilitiesResponse(value: unknown): AttachmentCapabilities {
  const root = object(value)
  const att = object(root.attachments)
  const maxBytes = att.max_bytes
  if (!Number.isSafeInteger(maxBytes) || (maxBytes as number) < 0) return invalid()
  return {
    enabled: bool(att.enabled),
    maxBytes: maxBytes as number,
    acceptedMime: Object.freeze(array(att.accepted_mime, nonempty)),
  }
}

export function decodePendingAttachment(value: unknown): PendingAttachment {
  const row = object(value)
  const upload = object(row.upload)
  return {
    attachmentId: uuid(row.attachment_id),
    state: oneOf(row.state, ['pending'] as const),
    upload: {
      method: oneOf(upload.method, ['PUT'] as const),
      url: nonempty(upload.url),
    },
    expiresAt: date(row.expires_at),
  }
}

export function decodeReadyAttachment(value: unknown): ReadyAttachment {
  const row = object(value)
  const byteSize = row.byte_size
  if (!Number.isSafeInteger(byteSize) || (byteSize as number) < 0) return invalid()
  return {
    attachmentId: uuid(row.attachment_id),
    state: oneOf(row.state, ['ready'] as const),
    contentHash: nonempty(row.content_hash),
    byteSize: byteSize as number,
  }
}

export function decodeAttachmentMetadata(value: unknown): AttachmentMetadata {
  const row = object(value)
  const byteSize = row.byte_size
  if (byteSize !== null && (!Number.isSafeInteger(byteSize) || (byteSize as number) < 0)) return invalid()
  return {
    id: uuid(row.id),
    workspaceId: uuid(row.workspace_id),
    state: oneOf(row.state, ['pending', 'ready', 'deleted'] as const),
    mime: nonempty(row.mime),
    byteSize: byteSize as number | null,
    contentHash: nullable(row.content_hash, nonempty),
    createdAt: date(row.created_at),
    readyAt: nullable(row.ready_at, date),
    fileName: nullable(row.file_name, text),
  }
}

export function mapAttachmentErrorMessage(code: string): string {
  switch (code) {
    case 'attachment_storage_unavailable':
      return '附件存储服务暂时不可用，请稍后重试。'
    case 'attachment_already_ready':
      return '附件已完成上传，不可重复写入。'
    case 'attachment_hash_mismatch':
      return '附件完整性校验失败，请重新上传。'
    case 'attachment_expired':
      return '上传凭证已过期，请重新发起上传。'
    case 'attachment_too_large':
    case 'payload_too_large':
      return '附件文件过大，超出工作区限制。'
    case 'attachment_unsupported_type':
      return '不支持该文件格式，请选择允许的文件类型。'
    case 'not_found':
      return '附件不存在、尚未就绪或您没有访问权限。'
    case 'login_required':
      return '请先登录以访问或上传附件。'
    case 'network_error':
      return '网络连接失败，请检查网络后重试。'
    case 'request_aborted':
      return '上传已取消。'
    default:
      return '附件操作失败，请重试。'
  }
}

/**
 * Uploads raw attachment bytes using XMLHttpRequest to support cross-browser
 * native byte-level upload progress tracking and cancellation.
 */
export function uploadAttachmentBytes(options: UploadAttachmentOptions): Promise<ReadyAttachment> {
  return new Promise((resolve, reject) => {
    const { uploadUrl, mime, content, contentSha256, accessToken, onProgress, signal } = options

    if (signal?.aborted) {
      reject(new ApiError('request_aborted', 0, false, true))
      return
    }

    const xhr = new XMLHttpRequest()
    xhr.open('PUT', uploadUrl)

    xhr.setRequestHeader('Accept', 'application/json')
    xhr.setRequestHeader('Content-Type', mime)
    if (accessToken) {
      xhr.setRequestHeader('Authorization', `Bearer ${accessToken}`)
    }
    if (contentSha256) {
      xhr.setRequestHeader('X-Content-SHA256', contentSha256)
    }

    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (event: ProgressEvent) => {
        if (event.lengthComputable) {
          onProgress({ loaded: event.loaded, total: event.total })
        }
      }
    }

    const onAbort = () => {
      xhr.abort()
      reject(new ApiError('request_aborted', 0, false, true))
    }
    signal?.addEventListener('abort', onAbort, { once: true })

    xhr.onload = () => {
      signal?.removeEventListener('abort', onAbort)
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const json = JSON.parse(xhr.responseText)
          resolve(decodeReadyAttachment(json))
        } catch {
          reject(new ApiError('invalid_response', xhr.status, false, true))
        }
      } else {
        try {
          const json = JSON.parse(xhr.responseText)
          const error = decodeErrorBody(object(json).error)
          const code = /^[a-z][a-z0-9_]{0,95}$/.test(error.code) ? error.code : 'api_error'
          reject(new ApiError(code, xhr.status, error.retryable, xhr.status >= 500, error.request_id))
        } catch {
          reject(new ApiError('api_error', xhr.status, false, xhr.status >= 500))
        }
      }
    }

    xhr.onerror = () => {
      signal?.removeEventListener('abort', onAbort)
      reject(new ApiError('network_error', 0, false, true))
    }

    xhr.ontimeout = () => {
      signal?.removeEventListener('abort', onAbort)
      reject(new ApiError('network_error', 0, false, true))
    }

    const payload = (content instanceof Uint8Array ? content : content) as XMLHttpRequestBodyInit
    xhr.send(payload)
  })
}

export interface AttachmentsApiClient {
  getCapabilities(): Promise<AttachmentCapabilities>
  create(workspaceId: string, input: CreateAttachmentInput, accessToken?: string | null): Promise<PendingAttachment>
  upload(options: UploadAttachmentOptions): Promise<ReadyAttachment>
  getMetadata(attachmentId: string, accessToken?: string | null): Promise<AttachmentMetadata>
  getContent(attachmentId: string, accessToken?: string | null, signal?: AbortSignal): Promise<Blob>
}

export function createAttachmentsApiClient(
  client: ApiClient = apiClient,
  fetcher: typeof fetch = globalThis.fetch.bind(globalThis)
): AttachmentsApiClient {
  return {
    async getCapabilities(): Promise<AttachmentCapabilities> {
      try {
        return await client.request('/api/v1/capabilities', {
          method: 'GET',
          decode: decodeCapabilitiesResponse,
        })
      } catch {
        // Legacy server returning 404 or network outage gracefully degrades to disabled.
        // Never throws at the app shell.
        return ATTACHMENTS_DISABLED_CAPABILITIES
      }
    },

    async create(workspaceId: string, input: CreateAttachmentInput, accessToken?: string | null): Promise<PendingAttachment> {
      const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/attachments`
      const body: Record<string, unknown> = { mime: input.mime }
      if (input.byteSize !== undefined) body.byte_size = input.byteSize
      if (input.fileName !== undefined) body.file_name = input.fileName

      return await client.request(path, {
        method: 'POST',
        accessToken: accessToken ?? undefined,
        body,
        decode: decodePendingAttachment,
      })
    },

    async upload(options: UploadAttachmentOptions): Promise<ReadyAttachment> {
      return await uploadAttachmentBytes(options)
    },

    async getMetadata(attachmentId: string, accessToken?: string | null): Promise<AttachmentMetadata> {
      const path = `/api/v1/attachments/${encodeURIComponent(attachmentId)}`
      return await client.request(path, {
        method: 'GET',
        accessToken: accessToken ?? undefined,
        decode: decodeAttachmentMetadata,
      })
    },

    async getContent(attachmentId: string, accessToken?: string | null, signal?: AbortSignal): Promise<Blob> {
      const path = `/api/v1/attachments/${encodeURIComponent(attachmentId)}/content`
      const headers: Record<string, string> = { Accept: '*/*' }
      if (accessToken) headers.Authorization = `Bearer ${accessToken}`

      const response = await fetcher(path, {
        method: 'GET',
        headers,
        signal,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
      })

      if (!response.ok) {
        if (response.status === 404) {
          throw new ApiError('not_found', 404, false, false)
        }
        let errorBody: unknown
        try {
          errorBody = await response.json()
        } catch {
          throw new ApiError('api_error', response.status, false, response.status >= 500)
        }
        try {
          const error = decodeErrorBody(object(errorBody).error)
          const code = /^[a-z][a-z0-9_]{0,95}$/.test(error.code) ? error.code : 'api_error'
          throw new ApiError(code, response.status, error.retryable, response.status >= 500, error.request_id)
        } catch (e) {
          if (e instanceof ApiError) throw e
          throw new ApiError('api_error', response.status, false, response.status >= 500)
        }
      }

      return await response.blob()
    },
  }
}
