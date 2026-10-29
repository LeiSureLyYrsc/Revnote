import { createContext } from 'react'
import type { ClientRuntime } from './runtime'

export const RuntimeContext = createContext<ClientRuntime | null>(null)
