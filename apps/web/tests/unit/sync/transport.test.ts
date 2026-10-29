import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from '../../../src/api/client'
import { createSyncTransport } from '../../../src/api/sync'

describe('native sync transport', () => {
  it('uses v1 routes, current bearer and one unchanged wire command', async () => {
    const workspace = '11111111-1111-4111-8111-111111111111'
    const operation = {
      op_id: '22222222-2222-4222-8222-222222222222', entity_id: '33333333-3333-4333-8333-333333333333',
      entity_type: 'note' as const, op: 'create' as const, payload: { title: 'Title', body_md: 'exact\r\n ', folder_id: null },
    }
    const requests: { url: string; init?: RequestInit }[] = []
    const fetcher = vi.fn<typeof fetch>(async (url, init) => {
      requests.push({ url: String(url), init })
      if (String(url).endsWith('/push')) return new Response(JSON.stringify({ results: [{ op_id: operation.op_id, status: 'applied', seq: '9007199254740993', rev: '1', error: null }] }))
      if (String(url).includes('/pull?')) return new Response(JSON.stringify({ objects: [], events: [], next_cursor: '9007199254740993', last_seq: '9007199254740993', min_retained_seq: '0', has_more: false }))
      return new Response(JSON.stringify({ objects: [], as_of_seq: '9007199254740993', min_retained_seq: '0' }))
    })
    let token = 'secret_access_a'
    const transport = createSyncTransport(() => token, new ApiClient(fetcher))
    const pushed = await transport.push(workspace, [operation])
    token = 'secret_access_b'
    await transport.pull(workspace, '9007199254740993')
    await transport.snapshot(workspace)
    expect(pushed.results[0].seq).toBe('9007199254740993')
    expect(requests.map(row => row.url)).toEqual([
      `/api/v1/workspaces/${workspace}/sync/push`, `/api/v1/workspaces/${workspace}/sync/pull?since=9007199254740993`,
      `/api/v1/workspaces/${workspace}/sync/snapshot`,
    ])
    expect(JSON.parse(String(requests[0].init?.body))).toEqual({ ops: [operation] })
    expect(requests[0].init?.headers).toMatchObject({ Authorization: 'Bearer secret_access_a' })
    expect(requests[1].init?.headers).toMatchObject({ Authorization: 'Bearer secret_access_b' })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('awaits the session token on every request and does not send without one', async () => {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ objects: [], as_of_seq: '0', min_retained_seq: '1' })))
    const getter = vi.fn(async () => null as string | null)
    const transport = createSyncTransport(getter, new ApiClient(fetcher))
    await expect(transport.snapshot('11111111-1111-4111-8111-111111111111')).rejects.toMatchObject({ status: 401, code: 'login_required' })
    expect(fetcher).not.toHaveBeenCalled()
    getter.mockResolvedValue('secret_access')
    await transport.snapshot('11111111-1111-4111-8111-111111111111')
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(getter).toHaveBeenCalledTimes(2)
  })

  it('fences account switches during an async token lookup before HTTP', async () => {
    const fetcher = vi.fn<typeof fetch>()
    let current = true
    const lease = { generation: 1, assertCurrent() { if (!current) throw new ApiError('session_superseded') } }
    const transport = createSyncTransport(async () => { current = false; return 'secret_new_identity' }, new ApiClient(fetcher), lease)
    await expect(transport.snapshot('11111111-1111-4111-8111-111111111111')).rejects.toMatchObject({ code: 'session_superseded' })
    expect(fetcher).not.toHaveBeenCalled()
  })
})
