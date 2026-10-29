import { useState } from 'react'
import { buttonClass, inputClass } from '../ui'

export interface LoginFormProps {
  readOnly?: boolean
  busy?: boolean
  error?: string
  onLogin(email: string, password: string, remember: boolean): Promise<void>
}

export function LoginForm({ readOnly = false, busy = false, error, onLogin }: LoginFormProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [failed, setFailed] = useState(false)
  return <form aria-label="登录 Revnote" className="mx-auto w-full max-w-sm space-y-4 p-5" onSubmit={async event => {
    event.preventDefault()
    if (readOnly || busy || submitting) return
    setSubmitting(true)
    setFailed(false)
    try { await onLogin(email, password, remember); setPassword('') }
    catch { setFailed(true); setPassword('') }
    finally { setSubmitting(false) }
  }}>
    <div><h1 className="text-xl font-semibold">登录 Revnote</h1><p className="mt-2 text-sm leading-relaxed text-zinc-400">连接你的工作区。草稿先保存到此设备，再同步到服务器。</p></div>
    <fieldset disabled={readOnly || busy || submitting} className="space-y-4">
      <label className="block space-y-1.5 text-sm">登录邮箱<input className={inputClass} type="email" autoComplete="username" required value={email} onChange={event => setEmail(event.target.value)} /></label>
      <label className="block space-y-1.5 text-sm">登录密码<input className={inputClass} type="password" autoComplete="current-password" required value={password} onChange={event => setPassword(event.target.value)} /></label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={remember} onChange={event => setRemember(event.target.checked)} aria-describedby="remember-risk" />记住登录</label>
      <p id="remember-risk" className="text-xs leading-relaxed text-zinc-400">仅勾选后才在此设备保存续期凭据。同源脚本可能读取它；共享设备请勿勾选。密码不会保存。本地缓存未加密。</p>
      <button className={`${buttonClass} w-full border-indigo-500/40 bg-indigo-600/30`} type="submit">{busy || submitting ? '正在登录…' : '登录'}</button>
    </fieldset>
    {readOnly && <p role="status" className="text-xs text-amber-300">此标签页只读，请在可写标签页登录。</p>}
    {(error || failed) && <p role="alert" className="text-sm text-rose-300">{error || '登录未完成，请检查连接与登录信息后重试。'}</p>}
  </form>
}
