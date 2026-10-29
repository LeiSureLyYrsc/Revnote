import type { DBSchema } from 'idb'
import type { Credential, Profile, WorkspaceState } from '../domain/contracts'

export const DATABASE_NAME = 'revnote-local'
export const DATABASE_VERSION = 1
export const FACT_STORES = ['server_objects', 'drafts', 'intents', 'operations', 'recovery_records'] as const
export type FactStore = typeof FACT_STORES[number]
export interface FactRecord { key: string; partition: string; value: unknown }

/**
 * workspaces contains the authoritative, partitioned aggregate. The five fact
 * stores are transactional projections, not independent caches: they are always
 * updated in the same transaction as the aggregate. This lets one synchronous
 * mutator atomically preserve draft+intent, ACK+successors and pull+cursor.
 * Profiles and credentials deliberately live outside recovery exports.
 */
export interface LocalSchema extends DBSchema {
  profiles: { key: string; value: Profile }
  credentials: { key: string; value: Credential }
  workspaces: { key: string; value: WorkspaceState }
  server_objects: { key: string; value: FactRecord; indexes: { partition: string } }
  drafts: { key: string; value: FactRecord; indexes: { partition: string } }
  intents: { key: string; value: FactRecord; indexes: { partition: string } }
  operations: { key: string; value: FactRecord; indexes: { partition: string } }
  recovery_records: { key: string; value: FactRecord; indexes: { partition: string } }
}
