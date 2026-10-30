import { describe, expect, it, vi } from 'vitest'
import {
  canUploadNow,
  extractAttachmentReferences,
  formatAttachmentMarkdown,
  insertMarkdownSnippet,
  validateAttachmentFile,
  createAttachmentPort,
} from '../../src/workspace/attachmentPorts'
import {
  ATTACHMENTS_DISABLED_CAPABILITIES,
  createAttachmentsApiClient,
  decodeCapabilitiesResponse,
  decodePendingAttachment,
  decodeReadyAttachment,
  decodeAttachmentMetadata,
  mapAttachmentErrorMessage,
  type AttachmentCapabilities,
} from '../../src/api/attachments'
import { ApiClient, ApiError } from '../../src/api/client'

const testCapabilities: AttachmentCapabilities = {
  enabled: true,
  maxBytes: 10 * 1024 * 1024,
  acceptedMime: ['image/png', 'image/jpeg', 'application/pdf', 'text/plain'],
}

describe('attachment capabilities and gating', () => {
  it('decodes enabled capabilities correctly from server', () => {
    const raw = {
      attachments: {
        enabled: true,
        max_bytes: 26214400,
        accepted_mime: ['image/png', 'image/jpeg', 'application/pdf'],
      },
    }
    const decoded = decodeCapabilitiesResponse(raw)
    expect(decoded.enabled).toBe(true)
    expect(decoded.maxBytes).toBe(26214400)
    expect(decoded.acceptedMime).toEqual(['image/png', 'image/jpeg', 'application/pdf'])
  })

  it('decodes pending, ready, and metadata responses correctly', () => {
    const pendingRaw = {
      attachment_id: '11111111-1111-1111-1111-111111111111',
      state: 'pending',
      upload: { method: 'PUT', url: '/api/v1/workspaces/ws1/attachments/att1/content' },
      expires_at: '2026-10-30T10:00:00Z',
    }
    const pending = decodePendingAttachment(pendingRaw)
    expect(pending.attachmentId).toBe('11111111-1111-1111-1111-111111111111')
    expect(pending.state).toBe('pending')
    expect(pending.upload.url).toBe('/api/v1/workspaces/ws1/attachments/att1/content')

    const readyRaw = {
      attachment_id: '11111111-1111-1111-1111-111111111111',
      state: 'ready',
      content_hash: 'abcdef1234567890',
      byte_size: 4096,
    }
    const ready = decodeReadyAttachment(readyRaw)
    expect(ready.state).toBe('ready')
    expect(ready.byteSize).toBe(4096)
    expect(ready.contentHash).toBe('abcdef1234567890')

    const metaRaw = {
      id: '11111111-1111-1111-1111-111111111111',
      workspace_id: '22222222-2222-2222-2222-222222222222',
      state: 'ready',
      mime: 'image/png',
      byte_size: 4096,
      content_hash: 'abcdef1234567890',
      created_at: '2026-10-30T09:00:00Z',
      ready_at: '2026-10-30T09:01:00Z',
      file_name: 'photo.png',
    }
    const meta = decodeAttachmentMetadata(metaRaw)
    expect(meta.id).toBe('11111111-1111-1111-1111-111111111111')
    expect(meta.fileName).toBe('photo.png')
  })

  it('degrades 404 from legacy server to disabled capabilities without throwing', async () => {
    const fakeClient = {
      request: vi.fn().mockRejectedValue(new ApiError('not_found', 404, false, false)),
    } as unknown as ApiClient

    const api = createAttachmentsApiClient(fakeClient)
    const caps = await api.getCapabilities()
    expect(caps).toEqual(ATTACHMENTS_DISABLED_CAPABILITIES)
    expect(caps.enabled).toBe(false)
  })

  it('degrades network error to disabled capabilities without throwing', async () => {
    const fakeClient = {
      request: vi.fn().mockRejectedValue(new ApiError('network_error', 0, false, true)),
    } as unknown as ApiClient

    const api = createAttachmentsApiClient(fakeClient)
    const caps = await api.getCapabilities()
    expect(caps).toEqual(ATTACHMENTS_DISABLED_CAPABILITIES)
    expect(caps.enabled).toBe(false)
  })
})

describe('canUploadNow predicate decision table', () => {
  it('denies when offline with offline reason', () => {
    const res = canUploadNow({
      capabilities: testCapabilities,
      isOnline: false,
      readOnly: false,
      workspaceId: 'ws-1',
    })
    expect(res.allowed).toBe(false)
    expect(res.reason).toBe('offline')
  })

  it('denies when read-only with read_only reason', () => {
    const res = canUploadNow({
      capabilities: testCapabilities,
      isOnline: true,
      readOnly: true,
      workspaceId: 'ws-1',
    })
    expect(res.allowed).toBe(false)
    expect(res.reason).toBe('read_only')
  })

  it('denies when capabilities are disabled with disabled reason', () => {
    const res = canUploadNow({
      capabilities: { ...testCapabilities, enabled: false },
      isOnline: true,
      readOnly: false,
      workspaceId: 'ws-1',
    })
    expect(res.allowed).toBe(false)
    expect(res.reason).toBe('disabled')
  })

  it('denies when storage is unavailable with no_storage reason', () => {
    const res = canUploadNow({
      capabilities: testCapabilities,
      isOnline: true,
      readOnly: false,
      storageAvailable: false,
      workspaceId: 'ws-1',
    })
    expect(res.allowed).toBe(false)
    expect(res.reason).toBe('no_storage')
  })

  it('denies when workspaceId is missing with no_workspace reason', () => {
    const res = canUploadNow({
      capabilities: testCapabilities,
      isOnline: true,
      readOnly: false,
      workspaceId: null,
    })
    expect(res.allowed).toBe(false)
    expect(res.reason).toBe('no_workspace')
  })

  it('allows when all conditions are satisfied', () => {
    const res = canUploadNow({
      capabilities: testCapabilities,
      isOnline: true,
      readOnly: false,
      storageAvailable: true,
      workspaceId: 'ws-1',
    })
    expect(res.allowed).toBe(true)
    expect(res.reason).toBeUndefined()
  })

  it('createAttachmentPort checks offline and read-only without requesting capabilities', async () => {
    const getCaps = vi.fn().mockResolvedValue(testCapabilities)
    const port = createAttachmentPort({
      workspaceId: 'ws-1',
      api: { getCapabilities: getCaps } as any,
      readOnly: true,
    })

    const result = await port.canUpload()
    expect(result.allowed).toBe(false)
    expect(result.reason).toBe('read_only')
    expect(getCaps).not.toHaveBeenCalled()
  })
})

describe('validateAttachmentFile mime and size rules', () => {
  it('accepts valid file matching allowed mime and within size limit', () => {
    const res = validateAttachmentFile(
      { name: 'diagram.png', size: 1024, type: 'image/png' },
      testCapabilities
    )
    expect(res.valid).toBe(true)
  })

  it('rejects file with mime not in acceptedMime whitelist', () => {
    const res = validateAttachmentFile(
      { name: 'script.exe', size: 1024, type: 'application/x-msdownload' },
      testCapabilities
    )
    expect(res.valid).toBe(false)
    if (!res.valid) {
      expect(res.reason).toBe('attachment_unsupported_type')
    }
  })

  it('rejects file larger than maxBytes', () => {
    const res = validateAttachmentFile(
      { name: 'huge.pdf', size: 15 * 1024 * 1024, type: 'application/pdf' },
      testCapabilities
    )
    expect(res.valid).toBe(false)
    if (!res.valid) {
      expect(res.reason).toBe('attachment_too_large')
    }
  })

  it('rejects empty 0-byte file', () => {
    const res = validateAttachmentFile(
      { name: 'empty.txt', size: 0, type: 'text/plain' },
      testCapabilities
    )
    expect(res.valid).toBe(false)
    if (!res.valid) {
      expect(res.reason).toBe('empty_file')
    }
  })
})

describe('frozen error code mapping', () => {
  it('maps all frozen error vocabulary to user-facing messages', () => {
    expect(mapAttachmentErrorMessage('attachment_storage_unavailable')).toContain('存储服务暂时不可用')
    expect(mapAttachmentErrorMessage('attachment_already_ready')).toContain('不可重复写入')
    expect(mapAttachmentErrorMessage('attachment_hash_mismatch')).toContain('完整性校验失败')
    expect(mapAttachmentErrorMessage('attachment_expired')).toContain('凭证已过期')
    expect(mapAttachmentErrorMessage('attachment_too_large')).toContain('超出工作区限制')
    expect(mapAttachmentErrorMessage('attachment_unsupported_type')).toContain('不支持该文件格式')
    expect(mapAttachmentErrorMessage('not_found')).toContain('没有访问权限')
    expect(mapAttachmentErrorMessage('login_required')).toContain('请先登录')
    expect(mapAttachmentErrorMessage('network_error')).toContain('网络连接失败')
    expect(mapAttachmentErrorMessage('request_aborted')).toContain('已取消')
  })
})

describe('formatAttachmentMarkdown reference contract', () => {
  it('formats exact markdown reference snippet ![alt](/api/v1/attachments/{id}/content)', () => {
    const id = '123e4567-e89b-12d3-a456-426614174000'
    const snippet = formatAttachmentMarkdown(id, '示意图.png')
    expect(snippet).toBe(`![示意图.png](/api/v1/attachments/${id}/content)`)
  })

  it('sanitizes brackets and handles fallback alt', () => {
    const id = '123e4567-e89b-12d3-a456-426614174000'
    expect(formatAttachmentMarkdown(id, '[test]')).toBe(`![test](/api/v1/attachments/${id}/content)`)
    expect(formatAttachmentMarkdown(id, '')).toBe(`![附件](/api/v1/attachments/${id}/content)`)
    expect(formatAttachmentMarkdown(id, undefined)).toBe(`![附件](/api/v1/attachments/${id}/content)`)
  })
})

describe('insertMarkdownSnippet preserving surrounding text', () => {
  it('inserts snippet at caret position without modifying any surrounding characters', () => {
    const original = 'Hello World!'
    const snippet = '![pic](/api/v1/attachments/att-1/content)'
    const result = insertMarkdownSnippet(original, snippet, { start: 6, end: 6 })

    expect(result.nextBody).toBe('Hello ![pic](/api/v1/attachments/att-1/content)World!')
    expect(result.newCursor).toBe(6 + snippet.length)
  })

  it('preserves CRLF line endings when inserting into CRLF markdown', () => {
    const original = '# Title\r\n\r\nFirst paragraph.\r\nSecond paragraph.'
    const snippet = '![doc](/api/v1/attachments/att-2/content)'
    const start = original.indexOf('Second')
    const result = insertMarkdownSnippet(original, snippet, { start, end: start })

    expect(result.nextBody).toBe('# Title\r\n\r\nFirst paragraph.\r\n![doc](/api/v1/attachments/att-2/content)Second paragraph.')
    expect(result.nextBody.includes('\r\n')).toBe(true)
    expect(result.nextBody.replace(/\r\n/g, '').includes('\n')).toBe(false)
  })

  it('replaces selection range cleanly without modifying text outside selection', () => {
    const original = 'Before [REPLACE ME] After'
    const snippet = '![img](/api/v1/attachments/att-3/content)'
    const start = original.indexOf('[REPLACE ME]')
    const end = start + '[REPLACE ME]'.length
    const result = insertMarkdownSnippet(original, snippet, { start, end })

    expect(result.nextBody).toBe('Before ![img](/api/v1/attachments/att-3/content) After')
    expect(result.newCursor).toBe(start + snippet.length)
  })

  it('appends with double newlines when selection is omitted', () => {
    const original = '# Note'
    const snippet = '![chart](/api/v1/attachments/att-4/content)'
    const result = insertMarkdownSnippet(original, snippet)

    expect(result.nextBody).toBe('# Note\n\n![chart](/api/v1/attachments/att-4/content)\n')
  })
})

describe('extractAttachmentReferences regex extractor', () => {
  it('extracts attachment IDs and alt text from markdown content', () => {
    const md = `
# Title
![图表一](/api/v1/attachments/11111111-1111-1111-1111-111111111111/content)
Some other text.
![文档说明](/api/v1/attachments/22222222-2222-2222-2222-222222222222/content)
`
    const refs = extractAttachmentReferences(md)
    expect(refs).toHaveLength(2)
    expect(refs[0].id).toBe('11111111-1111-1111-1111-111111111111')
    expect(refs[0].alt).toBe('图表一')
    expect(refs[1].id).toBe('22222222-2222-2222-2222-222222222222')
    expect(refs[1].alt).toBe('文档说明')
  })
})
