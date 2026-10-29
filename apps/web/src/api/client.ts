import type { ApiErrorBody, MeResponse, PullResponse, PushResponse, ServerObject, SnapshotResponse, SyncEvent, TokenPair } from './contracts'
import { decimal as protocolDecimal } from '../domain/decimal'

export type Decoder<T> = (value: unknown) => T

/** Errors deliberately never retain response bodies, request bodies or credentials. */
export class ApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 0,
    public readonly retryable = false,
    public readonly uncertain = false,
    public readonly requestId?: string,
  ) { super(code); this.name = 'ApiError' }
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
function decimal(value: unknown): string {
  try { return protocolDecimal(value) } catch { return invalid() }
}
function array<T>(value: unknown, decode: Decoder<T>): T[] { return Array.isArray(value) ? value.map(decode) : invalid() }
function oneOf<T extends string>(value: unknown, values: readonly T[]): T { return values.includes(value as T) ? value as T : invalid() }
function nullable<T>(value: unknown, decode: Decoder<T>): T | null { return value === null ? null : decode(value) }

export function decodeErrorBody(value: unknown): ApiErrorBody {
  const row = object(value)
  return { code: nonempty(row.code), message: text(row.message), request_id: text(row.request_id), retryable: bool(row.retryable), details: object(row.details) }
}

export function decodeTokenPair(value: unknown): TokenPair {
  const row = object(value)
  if (!Number.isSafeInteger(row.expires_in) || (row.expires_in as number) <= 0) return invalid()
  return {
    access_token: nonempty(row.access_token), refresh_token: nonempty(row.refresh_token),
    token_type: oneOf(row.token_type, ['Bearer']), expires_in: row.expires_in as number,
    session_id: uuid(row.session_id), session_expires_at: date(row.session_expires_at),
  }
}

export function decodeMeResponse(value: unknown): MeResponse {
  const row = object(value); const user = object(row.user)
  return {
    user: { id: uuid(user.id), email: nonempty(user.email), created_at: date(user.created_at) },
    workspaces: array(row.workspaces, value => { const item = object(value); return { id: uuid(item.id), role: nonempty(item.role) } }),
  }
}

export function decodeServerObject(value: unknown): ServerObject {
  const row = object(value)
  const common: ServerObject = {
    entity_type: oneOf(row.entity_type, ['folder', 'note']), id: uuid(row.id), workspace_id: uuid(row.workspace_id),
    rev: decimal(row.rev), updated_seq: decimal(row.updated_seq), created_at: date(row.created_at), updated_at: date(row.updated_at),
    deleted: bool(row.deleted), deleted_at: nullable(row.deleted_at, date),
  }
  return common.entity_type === 'folder'
    ? { ...common, name: text(row.name), parent_id: nullable(row.parent_id, uuid) }
    : { ...common, title: text(row.title), body_md: text(row.body_md), folder_id: nullable(row.folder_id, uuid) }
}

export function decodePushResponse(value: unknown): PushResponse {
  const row = object(value)
  return { results: array(row.results, value => {
    const item = object(value)
    const status = oneOf(item.status, ['applied', 'replayed', 'conflict', 'rejected', 'unknown', 'not_attempted'])
    const result: PushResponse['results'][number] = { op_id: uuid(item.op_id), status }
    // The server serializes absent optional fields as null. Normalize those away.
    if (item.seq != null) result.seq = decimal(item.seq)
    if (item.rev != null) result.rev = decimal(item.rev)
    if (item.error != null) result.error = decodeErrorBody(item.error)
    if ((status === 'applied' || status === 'replayed') && (!result.seq || !result.rev)) return invalid()
    return result
  }) }
}

export function decodePullResponse(value: unknown): PullResponse {
  const row = object(value)
  return {
    events: array(row.events, value => {
      const item = object(value)
      return {
        seq: decimal(item.seq), entity_type: oneOf(item.entity_type, ['folder', 'note']), entity_id: uuid(item.entity_id),
        op: oneOf(item.op, ['create', 'update', 'move', 'delete']), rev: decimal(item.rev), op_id: uuid(item.op_id), created_at: date(item.created_at),
      } satisfies SyncEvent
    }),
    objects: array(row.objects, decodeServerObject), next_cursor: decimal(row.next_cursor), has_more: bool(row.has_more),
    last_seq: decimal(row.last_seq), min_retained_seq: decimal(row.min_retained_seq),
  }
}

export function decodeSnapshotResponse(value: unknown): SnapshotResponse {
  const row = object(value)
  return { as_of_seq: decimal(row.as_of_seq), min_retained_seq: decimal(row.min_retained_seq), objects: array(row.objects, decodeServerObject) }
}

export interface RequestOptions<T> {
  method?: 'GET' | 'POST'
  accessToken?: string
  body?: unknown
  decode: Decoder<T>
  signal?: AbortSignal
}

export class ApiClient {
  constructor(private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis), private readonly timeoutMs = 15_000) {}

  async request<T>(path: string, options: RequestOptions<T>): Promise<T> {
    if (!path.startsWith('/api/v1/') || path.includes('://')) throw new ApiError('invalid_api_path')
    const controller = new AbortController()
    const abort = () => controller.abort()
    options.signal?.addEventListener('abort', abort, { once: true })
    if (options.signal?.aborted) controller.abort()
    const timer = setTimeout(abort, this.timeoutMs)
    try {
      const headers: Record<string, string> = { Accept: 'application/json' }
      if (options.accessToken) headers.Authorization = `Bearer ${options.accessToken}`
      if (options.body !== undefined) headers['Content-Type'] = 'application/json'
      const response = await this.fetcher(path, {
        method: options.method ?? 'GET', headers, body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      })
      if (!response.ok) {
        let error: ApiErrorBody
        try { error = decodeErrorBody(object(await response.json()).error) }
        catch { throw new ApiError('invalid_error_response', response.status, false, true) }
        // Codes and request IDs are protocol metadata, never use server message/details as display/log text.
        const code = /^[a-z][a-z0-9_]{0,95}$/.test(error.code) ? error.code : 'api_error'
        const requestId = /^[a-zA-Z0-9_-]{1,128}$/.test(error.request_id) ? error.request_id : undefined
        throw new ApiError(code, response.status, error.retryable, response.status >= 500, requestId)
      }
      if (response.status === 204) return options.decode(undefined)
      let value: unknown
      try { value = await response.json() } catch { throw new ApiError('invalid_response', response.status, false, true) }
      try { return options.decode(value) } catch { throw new ApiError('invalid_response', response.status, false, true) }
    } catch (error) {
      if (error instanceof ApiError) throw error
      throw new ApiError(controller.signal.aborted ? 'request_aborted' : 'network_error', 0, false, true)
    } finally {
      clearTimeout(timer)
      options.signal?.removeEventListener('abort', abort)
    }
  }
}

export const apiClient = new ApiClient()
