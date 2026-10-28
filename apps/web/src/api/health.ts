import { useQuery } from '@tanstack/react-query'

export interface HealthResponse {
  status: string
  [key: string]: unknown
}

export interface HealthCheckResult {
  ok: boolean
  data?: HealthResponse
  statusText: string
  statusCode?: number
  latencyMs?: number
  errorMessage?: string
  checkedAt: Date
}

// Default to /api, which Vite proxies to API_PROXY_TARGET (127.0.0.1:8000) stripping the /api prefix
const API_BASE_PATH = import.meta.env.VITE_API_BASE_PATH || '/api'

export async function fetchHealth(): Promise<HealthCheckResult> {
  const startTime = performance.now()
  const checkedAt = new Date()
  const controller = new AbortController()
  const timeoutId = setTimeout(() => {
    controller.abort(new Error('请求超时（5 秒未响应）'))
  }, 5000)

  try {
    const url = `${API_BASE_PATH.replace(/\/$/, '')}/health`
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
      },
      signal: controller.signal,
    })

    const latencyMs = Math.round(performance.now() - startTime)

    if (!response.ok) {
      return {
        ok: false,
        statusCode: response.status,
        statusText: `HTTP ${response.status} ${response.statusText}`,
        latencyMs,
        errorMessage: `服务端返回非正常状态码: ${response.status}`,
        checkedAt,
      }
    }

    let data: unknown
    try {
      data = await response.json()
    } catch {
      return {
        ok: false,
        statusCode: response.status,
        statusText: '响应格式无效',
        latencyMs,
        errorMessage: '服务端返回的内容无法解析为有效 JSON',
        checkedAt,
      }
    }

    // Validate API contract: status must be strictly 'ok'
    if (
      typeof data !== 'object' ||
      data === null ||
      (data as Record<string, unknown>).status !== 'ok'
    ) {
      return {
        ok: false,
        statusCode: response.status,
        statusText: '契约校验失败',
        latencyMs,
        errorMessage: `返回数据不符合 API 契约（期望 status='ok'，实际: ${JSON.stringify(data)}）`,
        checkedAt,
      }
    }

    const validData = data as HealthResponse
    return {
      ok: true,
      data: validData,
      statusCode: response.status,
      statusText: '服务运行正常',
      latencyMs,
      checkedAt,
    }
  } catch (err: unknown) {
    const latencyMs = Math.round(performance.now() - startTime)
    const message = err instanceof Error ? err.message : '网络连接失败'
    return {
      ok: false,
      statusText: '后端服务未连接',
      latencyMs,
      errorMessage: `无法连接至 ${API_BASE_PATH}/health (${message})。`,
      checkedAt,
    }
  } finally {
    clearTimeout(timeoutId)
  }
}

export function useHealthCheck() {
  return useQuery({
    queryKey: ['api-health'],
    queryFn: fetchHealth,
    retry: 1,
    refetchOnWindowFocus: false,
    staleTime: 10_000,
  })
}
