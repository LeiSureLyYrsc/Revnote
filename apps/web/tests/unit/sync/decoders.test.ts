import { describe, expect, it } from 'vitest'
import { ApiError } from '../../../src/api/client'
import { decodePullResponse, decodePushResponse, decodeServerObject, decodeSnapshotPageResponse, decodeSnapshotResponse } from '../../../src/api/sync'
import { note } from './fixtures'

const workspace = '11111111-1111-4111-8111-111111111111'
const id = '22222222-2222-4222-8222-222222222222'
const opId = '33333333-3333-4333-8333-333333333333'
const operation = { op_id: opId, entity_type: 'note' as const, entity_id: id, op: 'update' as const, base_rev: '1', payload: { title: 'local', body_md: 'body' } }
const object = () => note({ id, workspace_id: workspace })
const pull = () => ({
  events: [{ seq: '1', entity_type: 'note', entity_id: id, op: 'create', rev: '1', op_id: opId, created_at: '2026-01-01T00:00:00Z' }],
  objects: [object()], next_cursor: '1', has_more: false, last_seq: '1', min_retained_seq: '1',
})

describe('complete sync runtime decoders', () => {
  it.each(['applied', 'replayed', 'conflict', 'rejected', 'unknown', 'not_attempted'])('accepts %s and real nullable response fields', status => {
    const success = status === 'applied' || status === 'replayed'
    const result = decodePushResponse({ results: [{ op_id: opId, status, seq: success ? '2' : null, rev: success ? '2' : null, error: null }] }, [operation])
    expect(result.results[0].status).toBe(status)
  })

  it.each([
    { results: [] },
    { results: [{ op_id: id, status: 'applied', seq: '2', rev: '2' }] },
    { results: [{ op_id: opId, status: 'new_status' }] },
    { results: [{ op_id: opId, status: 'applied', seq: null, rev: '2' }] },
    { results: [{ op_id: opId, status: 'applied', seq: 2, rev: '2' }] },
    { results: [{ op_id: opId, status: 'unknown', seq: '2', rev: null }] },
    { results: [{ op_id: opId, status: 'rejected', error: { code: 'failed' } }] },
    { results: [{ op_id: opId, status: 'applied', seq: '2', rev: '2', body_md: 'unexpected' }] },
  ])('rejects malformed or mismatched push results as uncertain', value => {
    try { decodePushResponse(value, [operation]); throw new Error('Expected decoder rejection') }
    catch (error) { expect(error).toBeInstanceOf(ApiError); expect((error as ApiError).uncertain).toBe(true) }
  })

  it.each(['title', 'body_md', 'folder_id', 'deleted', 'deleted_at', 'workspace_id', 'rev', 'updated_seq'])('requires complete note field %s', field => {
    const value: Record<string, unknown> = { ...object() }
    delete value[field]
    expect(() => decodeServerObject(value, workspace)).toThrow(ApiError)
  })

  it('validates entity variants, workspace isolation, tombstones, UUIDs and bounded decimal strings', () => {
    expect(() => decodeServerObject({ ...object(), workspace_id: id }, workspace)).toThrow()
    expect(() => decodeServerObject({ ...object(), id: 'not-an-id' }, workspace)).toThrow()
    expect(() => decodeServerObject({ ...object(), rev: '0' }, workspace)).toThrow()
    expect(() => decodeServerObject({ ...object(), updated_seq: '9223372036854775808' }, workspace)).toThrow()
    expect(() => decodeServerObject({ ...object(), deleted: true }, workspace)).toThrow()
    expect(() => decodeServerObject({ ...object(), entity_type: 'folder' }, workspace)).toThrow()
    expect(decodeServerObject({ entity_type: 'folder', id, workspace_id: workspace, name: 'Folder', parent_id: null,
      rev: '1', updated_seq: '1', deleted: false, deleted_at: null, created_at: object().created_at, updated_at: object().updated_at }, workspace).name).toBe('Folder')
  })

  it('accepts ahead objects while advancing only the contiguous event cursor', () => {
    const value = pull()
    value.objects[0] = { ...object(), rev: '9007199254740999', updated_seq: '9007199254740999' }
    value.events[0].seq = '9007199254740993'
    value.next_cursor = '9007199254740993'
    value.last_seq = '9007199254740999'
    value.has_more = true
    expect(decodePullResponse(value, workspace, '9007199254740992').next_cursor).toBe('9007199254740993')
  })

  it('checks cursor boundaries, complete named objects and duplicate objects', () => {
    expect(decodePullResponse(pull(), workspace, '0').next_cursor).toBe('1')
    expect(() => decodePullResponse({ ...pull(), next_cursor: '2' }, workspace, '0')).toThrow()
    expect(() => decodePullResponse({ ...pull(), events: [], next_cursor: '0', has_more: true }, workspace, '0')).toThrow()
    expect(() => decodePullResponse({ ...pull(), has_more: true }, workspace, '0')).toThrow()
    expect(() => decodePullResponse({ ...pull(), objects: [] }, workspace, '0')).toThrow()
    expect(() => decodePullResponse({ ...pull(), objects: [object(), object()] }, workspace, '0')).toThrow()
    expect(() => decodePullResponse({ ...pull(), objects: [{ ...object(), updated_seq: '2' }] }, workspace, '0')).toThrow()
  })

  it('checks complete snapshot anchor, active-only objects and empty workspace watermark', () => {
    expect(decodeSnapshotResponse({ objects: [], as_of_seq: '0', min_retained_seq: '1' }, workspace).as_of_seq).toBe('0')
    expect(decodeSnapshotResponse({ objects: [object()], as_of_seq: '1', min_retained_seq: '1' }, workspace).objects).toHaveLength(1)
    expect(() => decodeSnapshotResponse({ objects: [object()], as_of_seq: '0', min_retained_seq: '1' }, workspace)).toThrow()
    expect(() => decodeSnapshotResponse({ objects: [object(), object()], as_of_seq: '1', min_retained_seq: '1' }, workspace)).toThrow()
    expect(() => decodeSnapshotResponse({ objects: [{ ...object(), deleted: true, deleted_at: object().updated_at }], as_of_seq: '1', min_retained_seq: '1' }, workspace)).toThrow()
  })

  it('checks paged snapshot watermark, terminal cursor and active-only objects', () => {
    const first = { objects: [object()], as_of_seq: '9', min_retained_seq: '1', next_cursor: 'page.two-1', has_more: true }
    const decoded = decodeSnapshotPageResponse(first, workspace)
    expect(decoded.as_of_seq).toBe('9')
    expect(decoded.next_cursor).toBe('page.two-1')
    expect(decodeSnapshotPageResponse({ ...first, objects: [], as_of_seq: '9', min_retained_seq: '1', next_cursor: null, has_more: false }, workspace).objects).toEqual([])

    // The watermark is pinned across pages; the floor may only advance.
    expect(() => decodeSnapshotPageResponse(first, workspace, '8')).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, min_retained_seq: '0' }, workspace, '9', '1')).toThrow()

    // An empty page may not claim more, and a terminal page may not carry a cursor.
    expect(() => decodeSnapshotPageResponse({ ...first, objects: [] }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, next_cursor: null }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, has_more: false }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, next_cursor: 'bad cursor' }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, next_cursor: 2 }, workspace)).toThrow()

    // Paged objects are active and cannot be newer than the pinned watermark.
    expect(() => decodeSnapshotPageResponse({ ...first, objects: [{ ...object(), deleted: true, deleted_at: object().updated_at }] }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, objects: [{ ...object(), updated_seq: '10' }] }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, objects: [object(), object()] }, workspace)).toThrow()
    expect(() => decodeSnapshotPageResponse({ ...first, objects: [], has_more: false, next_cursor: null, as_of_seq: '0', min_retained_seq: '2' }, workspace)).toThrow()
  })
})
