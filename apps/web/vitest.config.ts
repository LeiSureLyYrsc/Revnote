import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

const root = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  resolve: { alias: { '@': path.join(root, 'src') } },
  test: {
    root,
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/{unit,components}/**/*.{test,spec}.{ts,tsx}'],
    allowOnly: false,
    passWithNoTests: false,
    retry: 0,
    clearMocks: true,
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
    isolate: true,
    reporters: ['./e2e/vitest-reporter.ts'],
  },
})
