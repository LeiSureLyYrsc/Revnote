// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { resolveConfig, type ProxyOptions } from 'vite'

async function proxyRules(): Promise<Record<string, string | ProxyOptions>> {
  const config = await resolveConfig({ mode: 'e2e', envFile: false }, 'serve')
  return config.server.proxy ?? {}
}

function matchingRule(rules: Record<string, string | ProxyOptions>, requestPath: string): ProxyOptions | undefined {
  const entry = Object.entries(rules).find(([pattern]) => pattern.startsWith('^')
    ? new RegExp(pattern).test(requestPath) : requestPath.startsWith(pattern))
  return entry && typeof entry[1] !== 'string' ? entry[1] : undefined
}

describe('native API proxy contract', () => {
  it('forwards business API v1 paths without stripping the versioned prefix', async () => {
    const rules = await proxyRules()
    for (const endpoint of ['/api/v1/auth/login', '/api/v1/me', '/api/v1/workspaces/id/sync/snapshot']) {
      const rule = matchingRule(rules, endpoint)
      expect(rule).toBeDefined()
      expect(rule?.rewrite?.(endpoint) ?? endpoint).toBe(endpoint)
    }
  })

  it('rewrites only the exact health path and never a health-like business path', async () => {
    const rules = await proxyRules()
    expect(matchingRule(rules, '/api/health')?.rewrite?.('/api/health')).toBe('/health')
    for (const endpoint of ['/api/health/extra', '/api/healthcheck', '/api/other', '/v1/me']) {
      expect(matchingRule(rules, endpoint)).toBeUndefined()
    }
    expect(Object.keys(rules)).not.toContain('/api')
  })
})
