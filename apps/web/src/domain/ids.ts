export type EntityType = 'note' | 'folder'

export function newId(): string {
  return crypto.randomUUID()
}

export function entityKey(type: EntityType, id: string): string {
  return `${type}:${id}`
}

/** Fixed deployment namespace: arbitrary backend switching is not supported. */
export const DEPLOYMENT = 'revnote-same-origin-v1'

export interface Partition {
  deployment: string
  userId: string
  workspaceId: string
}

export function partitionKey(partition: Partition): string {
  return JSON.stringify([partition.deployment, partition.userId, partition.workspaceId])
}
