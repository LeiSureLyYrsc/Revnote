import type { ApiErrorBody, PullResponse, PushResponse, ServerObject, SnapshotPageResponse, SnapshotResponse, SyncEvent, WireOperation } from './contracts'
import { ApiClient, ApiError, apiClient } from './client'
import { compareDecimal, decimal, incrementDecimal } from '../domain/decimal'
import { entityKey } from '../domain/ids'
import type { WriteLease } from '../domain/contracts'

function invalid(): never { throw new ApiError('invalid_response', 0, false, true) }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
  return value as Record<string, unknown>
}
function text(value: unknown): string { return typeof value === 'string' ? value : invalid() }
function bool(value: unknown): boolean { return typeof value === 'boolean' ? value : invalid() }
function uuid(value: unknown): string {
  const id = text(value)
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ? id.toLowerCase() : invalid()
}
function counter(value: unknown, positive = false): string {
  let result: string
  try { result = decimal(value) } catch { return invalid() }
  return positive && result === '0' ? invalid() : result
}
function date(value: unknown): string {
  const result = text(value)
  return /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(result) && Number.isFinite(Date.parse(result)) ? result : invalid()
}
function array<T>(value: unknown, decode: (value: unknown) => T): T[] { return Array.isArray(value) ? value.map(decode) : invalid() }
function oneOf<T extends string>(value: unknown, values: readonly T[]): T { return values.includes(value as T) ? value as T : invalid() }
function nullable<T>(value: unknown, decode: (value: unknown) => T): T | null { return value === null ? null : decode(value) }
function fields(row: Record<string, unknown>, allowed: readonly string[]) {
  if (Object.keys(row).some(key => !allowed.includes(key))) invalid()
}
function decodeError(value: unknown): ApiErrorBody {
  const row = object(value)
  fields(row, ['code', 'message', 'request_id', 'retryable', 'details'])
  const code = text(row.code)
  if (!/^[a-z][a-z0-9_]{0,95}$/.test(code)) return invalid()
  return { code, message: text(row.message), request_id: text(row.request_id), retryable: bool(row.retryable), details: object(row.details) }
}

/** Response validation is deliberately stricter than the shared display type. */
export function decodeServerObject(value: unknown, workspaceId: string): ServerObject {
  const row = object(value)
  const entityType = oneOf(row.entity_type, ['note', 'folder'])
  fields(row, ['entity_type', 'id', 'workspace_id', 'rev', 'updated_seq', 'created_at', 'updated_at', 'deleted', 'deleted_at',
    ...(entityType === 'note' ? ['title', 'body_md', 'folder_id'] : ['name', 'parent_id'])])
  const workspace = uuid(row.workspace_id)
  if (workspace !== uuid(workspaceId)) return invalid()
  const common: ServerObject = {
    entity_type: entityType, id: uuid(row.id), workspace_id: workspace,
    rev: counter(row.rev, true), updated_seq: counter(row.updated_seq, true),
    created_at: date(row.created_at), updated_at: date(row.updated_at),
    deleted: bool(row.deleted), deleted_at: nullable(row.deleted_at, date),
  }
  if (common.deleted !== (common.deleted_at !== null)) return invalid()
  return entityType === 'note'
    ? { ...common, title: text(row.title), body_md: text(row.body_md), folder_id: nullable(row.folder_id, uuid) }
    : { ...common, name: text(row.name), parent_id: nullable(row.parent_id, uuid) }
}

/** Match every outcome to the submitted command, not merely to a UUID shape. */
export function decodePushResponse(value: unknown, operations: readonly WireOperation[]): PushResponse {
  const row = object(value)
  fields(row, ['results'])
  const results = array(row.results, value => {
    const item = object(value)
    fields(item, ['op_id', 'status', 'seq', 'rev', 'error'])
    const status = oneOf(item.status, ['applied', 'replayed', 'conflict', 'rejected', 'unknown', 'not_attempted'])
    const seq = item.seq == null ? null : counter(item.seq, true)
    const rev = item.rev == null ? null : counter(item.rev, true)
    const error = item.error == null ? null : decodeError(item.error)
    const success = status === 'applied' || status === 'replayed'
    if (success ? (!seq || !rev || error !== null) : (seq !== null || rev !== null)) return invalid()
    return { op_id: uuid(item.op_id), status, seq, rev, error }
  })
  if (results.length !== operations.length || new Set(results.map(result => result.op_id)).size !== results.length) return invalid()
  for (const [index, result] of results.entries()) if (result.op_id !== uuid(operations[index].op_id)) return invalid()
  return { results }
}

export function decodePullResponse(value: unknown, workspaceId: string, expectedCursor: string): PullResponse {
  const row = object(value)
  fields(row, ['events', 'objects', 'next_cursor', 'has_more', 'last_seq', 'min_retained_seq'])
  const response: PullResponse = {
    events: array(row.events, value => {
      const event = object(value)
      fields(event, ['seq', 'entity_type', 'entity_id', 'op', 'rev', 'op_id', 'created_at'])
      return {
        seq: counter(event.seq, true), entity_type: oneOf(event.entity_type, ['note', 'folder']),
        entity_id: uuid(event.entity_id), op: oneOf(event.op, ['create', 'update', 'move', 'delete']),
        rev: counter(event.rev, true), op_id: uuid(event.op_id), created_at: date(event.created_at),
      } satisfies SyncEvent
    }),
    objects: array(row.objects, value => decodeServerObject(value, workspaceId)),
    next_cursor: counter(row.next_cursor), has_more: bool(row.has_more),
    last_seq: counter(row.last_seq), min_retained_seq: counter(row.min_retained_seq),
  }
  let cursor = counter(expectedCursor)
  const objects = new Map(response.objects.map(value => [entityKey(value.entity_type, value.id), value]))
  if (objects.size !== response.objects.length) return invalid()
  const named = new Set<string>()
  for (const event of response.events) {
    if (event.seq !== incrementDecimal(cursor)) return invalid()
    cursor = event.seq
    const key = entityKey(event.entity_type, event.entity_id)
    named.add(key)
    const current = objects.get(key)
    if (!current || compareDecimal(current.rev, event.rev) < 0 || compareDecimal(current.updated_seq, event.seq) < 0) return invalid()
  }
  if (cursor !== response.next_cursor || compareDecimal(response.last_seq, cursor) < 0
    || BigInt(response.min_retained_seq) > BigInt(expectedCursor) + 1n
    || response.has_more !== (compareDecimal(cursor, response.last_seq) < 0)
    || (response.has_more && response.events.length === 0)
    || objects.size !== named.size) return invalid()
  for (const current of objects.values()) if (compareDecimal(current.updated_seq, response.last_seq) > 0) return invalid()
  return response
}

export function decodeSnapshotResponse(value: unknown, workspaceId: string): SnapshotResponse {
  const row = object(value)
  fields(row, ['as_of_seq', 'min_retained_seq', 'objects'])
  const response: SnapshotResponse = {
    as_of_seq: counter(row.as_of_seq), min_retained_seq: counter(row.min_retained_seq),
    objects: array(row.objects, value => decodeServerObject(value, workspaceId)),
  }
  if (BigInt(response.min_retained_seq) > BigInt(response.as_of_seq) + 1n) return invalid()
  const keys = new Set<string>()
  for (const current of response.objects) {
    const key = entityKey(current.entity_type, current.id)
    if (keys.has(key) || current.deleted || compareDecimal(current.updated_seq, response.as_of_seq) > 0) return invalid()
    keys.add(key)
  }
  return response
}

/** One page of the Phase 7 bootstrap; the watermark is repeated on every page. */
export function decodeSnapshotPageResponse(
  value: unknown,
  workspaceId: string,
  expectedAsOf?: string,
  expectedMinRetained?: string,
): SnapshotPageResponse {
  const row = object(value)
  fields(row, ['as_of_seq', 'min_retained_seq', 'objects', 'next_cursor', 'has_more'])
  const asOf = counter(row.as_of_seq)
  const minRetained = counter(row.min_retained_seq)
  if (BigInt(minRetained) > BigInt(asOf) + 1n) return invalid()
  if (expectedAsOf !== undefined && asOf !== expectedAsOf) return invalid()
  // The retention floor may advance while a bootstrap is running, but it never
  // moves backwards. The pinned as_of_seq itself must be byte-identical.
  if (expectedMinRetained !== undefined && BigInt(minRetained) < BigInt(expectedMinRetained)) return invalid()
  const objects = array(row.objects, value => decodeServerObject(value, workspaceId))
  const keys = new Set<string>()
  for (const current of objects) {
    const key = entityKey(current.entity_type, current.id)
    if (keys.has(key) || current.deleted || compareDecimal(current.updated_seq, asOf) > 0) return invalid()
    keys.add(key)
  }
  const hasMore = bool(row.has_more)
  let nextCursor: string | null
  if (row.next_cursor === null) nextCursor = null
  else {
    const cursor = text(row.next_cursor)
    if (!/^[A-Za-z0-9_.-]{1,2048}$/.test(cursor)) return invalid()
    nextCursor = cursor
  }
  // ``has_more`` is honest only when a non-empty page and a boundary exist, and
  // a terminal page must not hand out a cursor that pretends another follows.
  if (hasMore !== (nextCursor !== null)) return invalid()
  if (hasMore && objects.length === 0) return invalid()
  return {
    as_of_seq: asOf,
    min_retained_seq: minRetained,
    objects,
    next_cursor: nextCursor,
    has_more: hasMore,
  }
}

/** The server's paged-bootstrap page size, or null when the capability is absent. */
export function decodeSnapshotPaginationCapability(value: unknown): number | null {
  const row = object(value)
  const capability = row.snapshot_pagination
  if (capability === undefined || capability === null) return null
  const page = object(capability)
  if (page.enabled === false) return null
  if (page.enabled !== true) return invalid()
  const maxObjects = page.page_max_objects
  if (!Number.isSafeInteger(maxObjects) || (maxObjects as number) < 1) return invalid()
  const maxBytes = page.page_max_bytes
  if (!Number.isSafeInteger(maxBytes) || (maxBytes as number) < 1) return invalid()
  const maxTotal = page.max_total_objects
  if (!Number.isSafeInteger(maxTotal) || (maxTotal as number) < 1) return invalid()
  // The server is authoritative, but a client still bounds the request it will
  // make; a deployment that advertises an enormous page must not become one
  // browser response the UI cannot hold.
  return Math.min(maxObjects as number, 1_000)
}

export interface SyncTransport {
  push(workspaceId: string, operations: readonly WireOperation[]): Promise<PushResponse>
  pull(workspaceId: string, cursor: string): Promise<PullResponse>
  snapshot(workspaceId: string): Promise<SnapshotResponse>
}

/** Token lookup occurs per request; refresh and retries belong to the caller. */
export type AccessTokenGetter = () => string | null | undefined | Promise<string | null | undefined>

export function createSyncTransport(getAccessToken: AccessTokenGetter, client: ApiClient = apiClient, lease?: WriteLease): SyncTransport {
  const path = (workspaceId: string) => `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/sync`
  const accessToken = async () => {
    lease?.assertCurrent()
    const token = await getAccessToken()
    // Account switches can occur during refresh while the same tab still owns
    // its Web Lock. Check the captured identity again before sending anything.
    lease?.assertCurrent()
    if (!token) throw new ApiError('login_required', 401, false, false)
    return token
  }

  // Probed once per transport. ``null`` means the server predates Phase 7 (or
  // deliberately disables paging), so the legacy single snapshot is used.
  let paginationPageSize: Promise<number | null> | undefined
  const snapshotPageSize = (): Promise<number | null> => {
    if (paginationPageSize === undefined) {
      paginationPageSize = (async () => {
        const token = await accessToken()
        try {
          return await client.request('/api/v1/capabilities', {
            accessToken: token,
            decode: decodeSnapshotPaginationCapability,
          })
        } catch (error) {
          if (error instanceof ApiError && (error.status === 404 || error.code === 'not_found')) return null
          throw error
        }
      })().catch(error => {
        // A transient capability failure must not poison every later sync with
        // a cached rejection.
        paginationPageSize = undefined
        throw error
      })
    }
    return paginationPageSize
  }

  const snapshotOnce = async (workspaceId: string) => client.request(`${path(workspaceId)}/snapshot`, {
    accessToken: await accessToken(), decode: value => decodeSnapshotResponse(value, workspaceId),
  })

  const snapshotPaged = async (workspaceId: string, pageSize: number): Promise<SnapshotResponse> => {
    let asOf: string | undefined
    let minRetained: string | undefined
    let cursor: string | null = null
    const objects: ServerObject[] = []
    const keys = new Set<string>()
    for (let pages = 1; pages <= 1_000; pages += 1) {
      lease?.assertCurrent()
      const query = new URLSearchParams({ limit: String(pageSize) })
      if (cursor !== null) query.set('cursor', cursor)
      const page = await client.request(`${path(workspaceId)}/snapshot?${query.toString()}`, {
        accessToken: await accessToken(),
        decode: value => decodeSnapshotPageResponse(value, workspaceId, asOf, minRetained),
      })
      if (asOf === undefined) {
        asOf = page.as_of_seq
        minRetained = page.min_retained_seq
      } else if (BigInt(page.min_retained_seq) > BigInt(minRetained!)) {
        minRetained = page.min_retained_seq
      }
      for (const object of page.objects) {
        const key = entityKey(object.entity_type, object.id)
        if (keys.has(key)) return invalid()
        keys.add(key)
        objects.push(object)
      }
      if (!page.has_more) {
        return { as_of_seq: asOf!, min_retained_seq: minRetained!, objects }
      }
      if (page.next_cursor === null) return invalid()
      cursor = page.next_cursor
    }
    throw new ApiError('snapshot_page_budget', 0, false)
  }

  return {
    push: async (workspaceId, operations) => client.request(`${path(workspaceId)}/push`, {
      method: 'POST', accessToken: await accessToken(), body: { ops: operations }, decode: value => decodePushResponse(value, operations),
    }),
    pull: async (workspaceId, cursor) => client.request(`${path(workspaceId)}/pull?since=${encodeURIComponent(cursor)}`, {
      accessToken: await accessToken(), decode: value => decodePullResponse(value, workspaceId, cursor),
    }),
    snapshot: async workspaceId => {
      const pageSize = await snapshotPageSize()
      return pageSize === null ? snapshotOnce(workspaceId) : snapshotPaged(workspaceId, pageSize)
    },
  }
}
