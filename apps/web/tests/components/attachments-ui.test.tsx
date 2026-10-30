import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AttachmentPreview } from '../../src/components/attachments/AttachmentPreview'
import { AttachmentUpload } from '../../src/components/attachments/AttachmentUpload'
import { EditorPane } from '../../src/components/editor/EditorPane'
import type { AttachmentPort, CanUploadResult, UploadProgress } from '../../src/workspace/attachmentPorts'
import type { AttachmentCapabilities } from '../../src/api/attachments'
import type { WorkspacePort } from '../../src/workspace/ports'
import { emptyWorkspace } from '../../src/domain/contracts'

const defaultCaps: AttachmentCapabilities = {
  enabled: true,
  maxBytes: 10 * 1024 * 1024,
  acceptedMime: ['image/png', 'image/jpeg', 'application/pdf'],
}

function mockAttachmentPort(overrides: Partial<AttachmentPort> = {}): AttachmentPort {
  return {
    getCapabilities: vi.fn(async () => defaultCaps),
    canUpload: vi.fn(async (): Promise<CanUploadResult> => ({ allowed: true })),
    upload: vi.fn(async (file: File) => ({
      attachmentId: '11111111-1111-1111-1111-111111111111',
      markdownSnippet: `![${file.name}](/api/v1/attachments/11111111-1111-1111-1111-111111111111/content)`,
    })),
    fetchBlob: vi.fn(async () => new Blob(['dummy'], { type: 'image/png' })),
    ...overrides,
  }
}

const partition = { deployment: 'component-test', userId: 'user-1', workspaceId: 'ws-1' }
function mockWorkspacePort(overrides: Partial<WorkspacePort> = {}): WorkspacePort {
  return {
    partition,
    ownershipGeneration: 1,
    readOnly: false,
    read: async () => emptyWorkspace(partition),
    exportLocal: vi.fn(async () => {}),
    subscribe: () => () => {},
    save: vi.fn(async () => {}),
    create: vi.fn(async () => 'note-new'),
    move: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
    preserveSource: vi.fn(async () => {}),
    resolve: vi.fn(async () => {}),
    retry: vi.fn(async () => {}),
    ...overrides,
  }
}

beforeEach(() => {
  if (!URL.createObjectURL) {
    URL.createObjectURL = vi.fn()
  }
  if (!URL.revokeObjectURL) {
    URL.revokeObjectURL = vi.fn()
  }
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AttachmentPreview component', () => {
  it('renders loading state initially with accessible aria-live indicator', () => {
    let resolveBlob!: (blob: Blob) => void
    const fetchBlob = vi.fn(() => new Promise<Blob>(resolve => { resolveBlob = resolve }))

    render(<AttachmentPreview attachmentId="att-1" alt="预览图" fetchBlob={fetchBlob} />)

    expect(screen.getByRole('status')).toBeDefined()
    expect(screen.getByText('正在加载附件预览…')).toBeDefined()

    act(() => {
      resolveBlob(new Blob(['data'], { type: 'image/png' }))
    })
  })

  it('renders image on successful blob load and calls revokeObjectURL on unmount', async () => {
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-image-url')
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

    const fetchBlob = vi.fn(async () => new Blob(['fake image content'], { type: 'image/png' }))

    const { unmount } = render(
      <AttachmentPreview attachmentId="att-1" alt="示例图片.png" fetchBlob={fetchBlob} />
    )

    await waitFor(() => {
      const img = screen.getByTestId('attachment-image') as HTMLImageElement
      expect(img).toBeDefined()
      expect(img.src).toBe('blob:mock-image-url')
      expect(img.alt).toBe('示例图片.png')
    })

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).not.toHaveBeenCalled()

    // Unmount and verify object URL is revoked to prevent memory leak
    unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-image-url')
  })

  it('renders error alert when blob fetch fails', async () => {
    const fetchBlob = vi.fn(async () => {
      throw new Error('网络连接不可用')
    })

    render(<AttachmentPreview attachmentId="att-err" alt="损坏附件" fetchBlob={fetchBlob} />)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeDefined()
      expect(screen.getByText('附件预览加载失败')).toBeDefined()
    })
  })

  it('renders PDF preview with iframe and download link', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-pdf-url')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

    const fetchBlob = vi.fn(async () => new Blob(['pdf binary'], { type: 'application/pdf' }))

    render(<AttachmentPreview attachmentId="att-pdf" alt="报告.pdf" fetchBlob={fetchBlob} />)

    await waitFor(() => {
      expect(screen.getByTestId('attachment-pdf')).toBeDefined()
      expect(screen.getByText('下载 PDF')).toBeDefined()
    })
  })
})

describe('AttachmentUpload component', () => {
  it('disables upload button and shows explicit reason when read-only', async () => {
    const port = mockAttachmentPort()
    render(<AttachmentUpload port={port} readOnly={true} onInsert={vi.fn()} />)

    const btn = screen.getByTestId('upload-attachment-button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)

    const reason = await screen.findByTestId('upload-disabled-reason')
    expect(reason.textContent).toContain('只读')
  })

  it('disables upload button and shows explicit reason when offline', async () => {
    const port = mockAttachmentPort({
      canUpload: vi.fn(async (): Promise<CanUploadResult> => ({
        allowed: false,
        reason: 'offline' as const,
        message: '当前处于离线状态，无法上传附件。',
      })),
    })

    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)

    render(<AttachmentUpload port={port} readOnly={false} onInsert={vi.fn()} />)

    const btn = screen.getByTestId('upload-attachment-button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)

    const reason = await screen.findByTestId('upload-disabled-reason')
    expect(reason.textContent).toContain('离线状态')
  })

  it('disables upload button when server capabilities indicate attachments disabled', async () => {
    const port = mockAttachmentPort({
      getCapabilities: vi.fn(async () => ({ enabled: false, maxBytes: 0, acceptedMime: [] })),
      canUpload: vi.fn(async (): Promise<CanUploadResult> => ({
        allowed: false,
        reason: 'disabled' as const,
        message: '当前服务未启用附件功能。',
      })),
    })

    render(<AttachmentUpload port={port} readOnly={false} onInsert={vi.fn()} />)

    const btn = await screen.findByTestId('upload-attachment-button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)

    const reason = await screen.findByTestId('upload-disabled-reason')
    expect(reason.textContent).toContain('未启用附件功能')
  })

  it('shows error alert when user selects a file with unsupported MIME', async () => {
    const port = mockAttachmentPort()
    render(<AttachmentUpload port={port} readOnly={false} onInsert={vi.fn()} />)

    await waitFor(() => {
      const btn = screen.getByTestId('upload-attachment-button') as HTMLButtonElement
      expect(btn.disabled).toBe(false)
    })

    const input = screen.getByTestId('attachment-file-input')
    const badFile = new File(['content'], 'test.exe', { type: 'application/x-msdownload' })

    // Simulate selecting file
    vi.mocked(port.upload).mockRejectedValueOnce({ code: 'attachment_unsupported_type' })
    fireEvent.change(input, { target: { files: [badFile] } })

    const alert = await screen.findByTestId('upload-error-alert')
    expect(alert.textContent).toContain('不支持该文件格式')
  })

  it('shows progress bar during upload and calls onInsert on success', async () => {
    let progressCb!: (p: UploadProgress) => void
    let resolveUpload!: (res: { attachmentId: string; markdownSnippet: string }) => void

    const port = mockAttachmentPort({
      upload: vi.fn(async (
        _file: File,
        opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal }
      ): Promise<{ attachmentId: string; markdownSnippet: string }> => {
        if (opts?.onProgress) progressCb = opts.onProgress
        return new Promise(resolve => { resolveUpload = resolve })
      }),
    })

    const onInsert = vi.fn()
    render(<AttachmentUpload port={port} readOnly={false} onInsert={onInsert} />)

    await waitFor(() => {
      expect((screen.getByTestId('upload-attachment-button') as HTMLButtonElement).disabled).toBe(false)
    })

    const input = screen.getByTestId('attachment-file-input')
    const goodFile = new File(['image binary data'], 'chart.png', { type: 'image/png' })

    fireEvent.change(input, { target: { files: [goodFile] } })

    // Progress bar should appear
    await waitFor(() => {
      expect(screen.getByRole('progressbar')).toBeDefined()
    })

    // Simulate progress updates
    act(() => {
      progressCb({ loaded: 50, total: 100 })
    })
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('50')

    // Simulate upload completion
    act(() => {
      resolveUpload({
        attachmentId: '22222222-2222-2222-2222-222222222222',
        markdownSnippet: '![chart.png](/api/v1/attachments/22222222-2222-2222-2222-222222222222/content)',
      })
    })

    await waitFor(() => {
      expect(onInsert).toHaveBeenCalledWith(
        '![chart.png](/api/v1/attachments/22222222-2222-2222-2222-222222222222/content)'
      )
    })
  })

  it('cancels upload when cancel button is clicked', async () => {
    let aborted = false
    const port = mockAttachmentPort({
      upload: vi.fn(async (
        _file: File,
        opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal }
      ): Promise<{ attachmentId: string; markdownSnippet: string }> => {
        return new Promise((_, reject) => {
          opts?.signal?.addEventListener('abort', () => {
            aborted = true
            reject(new Error('aborted'))
          })
        })
      }),
    })

    render(<AttachmentUpload port={port} readOnly={false} onInsert={vi.fn()} />)

    await waitFor(() => {
      expect((screen.getByTestId('upload-attachment-button') as HTMLButtonElement).disabled).toBe(false)
    })

    const input = screen.getByTestId('attachment-file-input')
    fireEvent.change(input, { target: { files: [new File(['data'], 'pic.png', { type: 'image/png' })] } })

    const cancelBtn = await screen.findByRole('button', { name: '取消上传' })
    fireEvent.click(cancelBtn)

    expect(aborted).toBe(true)
  })
})

describe('EditorPane attachment integration', () => {
  it('renders upload button in toolbar and preserves surrounding text on reference insertion', async () => {
    const wsPort = mockWorkspacePort()
    const attPort = mockAttachmentPort()
    const note = {
      id: 'note-1',
      title: '工作区笔记',
      bodyMd: '# 笔记标题\n\n正文前段\n正文后段',
      parentId: null,
    }

    render(<EditorPane port={wsPort} note={note} attachmentPort={attPort} />)

    expect(screen.getByRole('button', { name: '上传附件' })).toBeDefined()
    const textarea = screen.getByLabelText('Markdown 正文') as HTMLTextAreaElement
    expect(textarea.value).toBe(note.bodyMd)

    // Set cursor selection between "正文前段\n" and "正文后段"
    const insertPos = note.bodyMd.indexOf('正文后段')
    textarea.selectionStart = insertPos
    textarea.selectionEnd = insertPos

    // Upload an attachment
    const input = screen.getByTestId('attachment-file-input')
    const file = new File(['pic'], 'demo.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => {
      expect(wsPort.save).toHaveBeenCalled()
    })

    const savedBody = vi.mocked(wsPort.save).mock.calls[0][0].bodyMd
    expect(savedBody).toContain('正文前段\n![demo.png](/api/v1/attachments/11111111-1111-1111-1111-111111111111/content)正文后段')
    expect(savedBody.startsWith('# 笔记标题\n\n正文前段\n')).toBe(true)
    expect(savedBody.endsWith('正文后段')).toBe(true)
  })

  it('renders attachment preview section when note contains attachment references', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-url')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

    const wsPort = mockWorkspacePort()
    const attPort = mockAttachmentPort()
    const noteWithAttachment = {
      id: 'note-2',
      title: '带有附件的笔记',
      bodyMd: '# 笔记\n\n![架构图](/api/v1/attachments/33333333-3333-3333-3333-333333333333/content)',
      parentId: null,
    }

    render(<EditorPane port={wsPort} note={noteWithAttachment} attachmentPort={attPort} />)

    expect(screen.getByRole('region', { name: '笔记附件预览区' })).toBeDefined()
    expect(screen.getByText('附件预览 (1)')).toBeDefined()
  })
})
