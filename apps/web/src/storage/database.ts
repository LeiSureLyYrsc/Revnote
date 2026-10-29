import { openDB, type IDBPDatabase } from 'idb'
import { DATABASE_NAME, DATABASE_VERSION, FACT_STORES, type LocalSchema } from './schema'

export type StorageErrorCode = 'blocked' | 'versionchange' | 'quota' | 'aborted' | 'unavailable' | 'invalid_mutation' | 'lease'

export class StorageError extends Error {
  readonly code: StorageErrorCode
  constructor(code: StorageErrorCode, message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'StorageError'
    this.code = code
  }
}

export function storageError(error: unknown): StorageError {
  if (error instanceof StorageError) return error
  const name = error instanceof Error || error instanceof DOMException ? error.name : ''
  if (name === 'QuotaExceededError') return new StorageError('quota', 'Local storage is full. Preserve or export your edits; no records were removed.', { cause: error })
  if (name === 'AbortError') return new StorageError('aborted', 'Local transaction was aborted; changes were not saved.', { cause: error })
  if (name === 'VersionError') return new StorageError('versionchange', 'This local database requires a newer client. No data was cleared.', { cause: error })
  return new StorageError('unavailable', 'Local storage is unavailable; changes were not saved.', { cause: error })
}

export interface DatabaseOptions { onLifecycleError?: (error: StorageError) => void }

/** Never deletes or recreates an incompatible database. */
export async function openDatabase(name = DATABASE_NAME, options: DatabaseOptions = {}): Promise<IDBPDatabase<LocalSchema>> {
  let abandoned = false
  let rejectBlocked!: (error: StorageError) => void
  const blocked = new Promise<never>((_, reject) => { rejectBlocked = reject })
  try {
    const pending = openDB<LocalSchema>(name, DATABASE_VERSION, {
      upgrade(db) {
        db.createObjectStore('profiles', { keyPath: 'key' })
        db.createObjectStore('credentials', { keyPath: 'key' })
        // The workspace key is an encoded tuple, never an ambiguous join.
        db.createObjectStore('workspaces')
        for (const store of FACT_STORES) {
          db.createObjectStore(store, { keyPath: 'key' }).createIndex('partition', 'partition')
        }
      },
      blocked() {
        abandoned = true
        const error = new StorageError('blocked', 'Local database upgrade is blocked by another tab. Close that tab and retry; no data was cleared.')
        options.onLifecycleError?.(error)
        rejectBlocked(error)
      },
      blocking() {
        // Stop old writers immediately and allow the newer client to upgrade.
        options.onLifecycleError?.(new StorageError('versionchange', 'A newer client changed the local database version. Reload before writing.'))
        void pending.then(db => db.close())
      },
      terminated() {
        options.onLifecycleError?.(new StorageError('aborted', 'The local database connection ended unexpectedly. Reload before writing.'))
      },
    })
    // An IDB open request cannot be cancelled; close a late success after blocked.
    void pending.then(db => { if (abandoned) db.close() }, () => {})
    return await Promise.race([pending, blocked])
  } catch (error) {
    throw storageError(error)
  }
}
