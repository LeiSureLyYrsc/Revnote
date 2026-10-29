import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Read root .env from the repository root
  const rootEnvDir = path.resolve(__dirname, '../../')
  const env = loadEnv(mode, rootEnvDir, '')

  // API_PROXY_TARGET is read strictly on the Vite dev server side.
  // It is NOT prefixed with VITE_, so it is never exposed in the client-side bundle.
  const apiProxyTarget =
    env.API_PROXY_TARGET || process.env.API_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    envDir: rootEnvDir,
    plugins: [
      react(),
      tailwindcss(),
    ],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 5173,
      proxy: {
        // Health is the only root route; business API v1 keeps its prefix.
        '^/api/health$': {
          target: apiProxyTarget,
          changeOrigin: true,
          rewrite: () => '/health',
        },
        '/api/v1': {
          target: apiProxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
