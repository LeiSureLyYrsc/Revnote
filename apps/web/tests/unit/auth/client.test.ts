import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError, decodeMeResponse, decodePullResponse, decodePushResponse, decodeSnapshotResponse, decodeTokenPair } from '../../../src/api/client'
import { createAuthApi } from '../../../src/api/auth'

const id = '10000000-0000-4000-8000-000000000001'
const timestamp = '2026-01-01T00:00:00Z'
const pair = { access_token: 'secret_access', refresh_token: 'secret_refresh', token_type: 'Bearer', expires_in: 60, session_id: id, session_expires_at: timestamp }
const errorBody = { code: 'invalid_credentials', message: 'secret_server_text', request_id: 'request_1', retryable: false, details: {} }

describe('strict single attempt API client', () => {
  it('decodes real token pair and separate me response', () => {
    expect(decodeTokenPair(pair)).toEqual(pair)
    expect(decodeMeResponse({ user: { id, email: 'person@example.com', created_at: timestamp }, workspaces: [{ id, role: 'owner' }] }).user.id).toBe(id)
    expect(() => decodeTokenPair({ ...pair, expires_in: '60' })).toThrow(ApiError)
    expect(() => decodeTokenPair({ ...pair, session_id: 'invalid' })).toThrow(ApiError)
    expect(() => decodeMeResponse({ user: {}, workspaces: [] })).toThrow(ApiError)
  })

  it('accepts nullable wire push fields across all six outcomes', () => {
    for (const status of ['applied', 'replayed', 'conflict', 'rejected', 'unknown', 'not_attempted']) {
      const success = status === 'applied' || status === 'replayed'
      const response = decodePushResponse({ results: [{ op_id: id, status, seq: success ? '9007199254740999' : null, rev: success ? '2' : null, error: success ? null : errorBody }] })
      expect(response.results[0].status).toBe(status)
      expect(response.results[0].seq).toBe(success ? '9007199254740999' : undefined)
    }
    expect(decodePushResponse({ results: [{ op_id: id, status: 'not_attempted', seq: null, rev: null, error: null }] }).results[0].error).toBeUndefined()
  })

  it('rejects invalid sync decimal values and malformed objects without rounding', () => {
    const object = { entity_type: 'note', id, workspace_id: id, rev: '9007199254740999', updated_seq: '9007199254741000', created_at: timestamp, updated_at: timestamp, deleted_at: null, deleted: false, title: 'A', body_md: 'secret_note', folder_id: null }
    expect(decodeSnapshotResponse({ as_of_seq: '9007199254741000', min_retained_seq: '0', objects: [object] }).objects[0].rev).toBe('9007199254740999')
    expect(() => decodeSnapshotResponse({ as_of_seq: 9007199254741000, min_retained_seq: '0', objects: [] })).toThrow(ApiError)
    expect(() => decodeSnapshotResponse({ as_of_seq: '9223372036854775808', min_retained_seq: '0', objects: [] })).toThrow(ApiError)
    expect(decodeSnapshotResponse({ as_of_seq: '9223372036854775807', min_retained_seq: '0', objects: [] }).as_of_seq).toBe('9223372036854775807')
    expect(() => decodeSnapshotResponse({ as_of_seq: '1', min_retained_seq: '0', objects: [{ ...object, body_md: null }] })).toThrow(ApiError)
    expect(decodePullResponse({ events: [], objects: [], next_cursor: '0', has_more: false, last_seq: '0', min_retained_seq: '0' }).next_cursor).toBe('0')
  })

  it('uses exact login/me/logout paths and bearer logout', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(pair))
      .mockResolvedValueOnce(Response.json({ user: { id, email: 'person@example.com', created_at: timestamp }, workspaces: [] }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createAuthApi(new ApiClient(fetcher))
    await api.login('person@example.com', 'secret_password')
    await api.me('secret_access')
    await api.logout('secret_access')
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/v1/auth/login', '/api/v1/me', '/api/v1/auth/logout'])
    expect(fetcher.mock.calls[2][1]?.headers).toMatchObject({ Authorization: 'Bearer secret_access' })
    expect(fetcher.mock.calls[2][1]?.body).toBeUndefined()
  })

  it('does not retry authentication or expose server message in thrown errors', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ error: errorBody }, { status: 401 }))
    const api = createAuthApi(new ApiClient(fetcher))
    const error = await api.login('person@example.com', 'secret_password').catch(error => error as ApiError)
    expect(error).toMatchObject({ code: 'invalid_credentials', status: 401, uncertain: false, requestId: 'request_1' })
    expect(JSON.stringify(error)).not.toContain('secret_')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('classifies malformed push response as uncertain, not a definite rejection', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ results: [{ status: 'applied', secret_body: 'secret_note' }] }))
    const client = new ApiClient(fetcher)
    await expect(client.request('/api/v1/workspaces/id/sync/push', { method: 'POST', decode: decodePushResponse })).rejects.toMatchObject({ code: 'invalid_response', uncertain: true })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('classifies network loss and invalid error envelopes as uncertain without retries', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('secret_network_details'))
    const client = new ApiClient(fetcher)
    await expect(client.request('/api/v1/me', { decode: decodeMeResponse })).rejects.toMatchObject({ code: 'network_error', uncertain: true })
    expect(fetcher).toHaveBeenCalledTimes(1)
    fetcher.mockResolvedValueOnce(Response.json({ error: 'secret_bad' }, { status: 500 }))
    await expect(client.request('/api/v1/me', { decode: decodeMeResponse })).rejects.toMatchObject({ code: 'invalid_error_response', uncertain: true })
  })
})
