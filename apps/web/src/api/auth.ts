import type { MeResponse, TokenPair } from './contracts'
import { ApiClient, apiClient, decodeMeResponse, decodeTokenPair } from './client'

export interface AuthApi {
  login(email: string, password: string): Promise<TokenPair>
  me(accessToken: string): Promise<MeResponse>
  refresh(refreshToken: string): Promise<TokenPair>
  logout(accessToken: string): Promise<void>
}

export function createAuthApi(client: ApiClient = apiClient): AuthApi {
  return {
    login: (email, password) => client.request('/api/v1/auth/login', { method: 'POST', body: { email, password }, decode: decodeTokenPair }),
    me: accessToken => client.request('/api/v1/me', { accessToken, decode: decodeMeResponse }),
    refresh: refreshToken => client.request('/api/v1/auth/refresh', { method: 'POST', body: { refresh_token: refreshToken }, decode: decodeTokenPair }),
    logout: accessToken => client.request('/api/v1/auth/logout', { method: 'POST', accessToken, decode: () => undefined }),
  }
}
