import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, devices } from '@playwright/test'

const root = path.dirname(fileURLToPath(import.meta.url))
const baseURL = process.env.WEB_E2E_BASE_URL
const apiURL = process.env.WEB_E2E_API_URL

if (!baseURL || !apiURL || !process.env.WEB_E2E_RUN_ID) {
  throw new Error('Run Playwright through the owned API/PostgreSQL lifecycle runner.')
}
for (const endpoint of [baseURL, apiURL]) {
  const url = new URL(endpoint)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password) {
    throw new Error('E2E servers must be runner-owned loopback HTTP endpoints.')
  }
}

export default defineConfig({
  testDir: path.join(root, 'e2e'),
  testMatch: '**/*.spec.ts',
  forbidOnly: true,
  retries: 0,
  workers: 1,
  fullyParallel: false,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['./e2e/playwright-reporter.ts']],
  outputDir: path.join(root, 'reports', 'browser-output'),
  preserveOutput: 'never',
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    serviceWorkers: 'block',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  // No webServer/reuseExistingServer: only run_web_e2e.py owns server lifetimes.
})
