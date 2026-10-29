import { cleanup } from '@testing-library/react'
import 'fake-indexeddb/auto'
import { IDBFactory, IDBKeyRange } from 'fake-indexeddb'
import { afterEach, beforeEach } from 'vitest'

// Every case gets a new database namespace; no order-dependent persistent state.
beforeEach(() => {
  Object.defineProperty(globalThis, 'indexedDB', {
    configurable: true,
    value: new IDBFactory(),
  })
  Object.defineProperty(globalThis, 'IDBKeyRange', {
    configurable: true,
    value: IDBKeyRange,
  })
})

afterEach(() => {
  cleanup()
})
