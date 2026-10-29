import type { Draft, WorkspaceState } from '../domain/contracts'
import { entityKey } from '../domain/ids'

/** Present every active cached object, including children whose parent is not cached yet. */
export function visibleEntities(state: WorkspaceState): Draft[] {
  const entities = new Map<string, Draft>()
  for (const object of Object.values(state.serverObjects)) {
    if (object.deleted || object.deleted_at) continue
    const key = entityKey(object.entity_type, object.id)
    entities.set(key, { key, entityType: object.entity_type, entityId: object.id, title: object.name ?? object.title ?? '', bodyMd: object.body_md ?? '', parentId: object.parent_id ?? object.folder_id ?? null, version: '0', acknowledgedVersion: '0', base: object, dirty: false, deleted: false })
  }
  for (const draft of Object.values(state.drafts)) {
    if (draft.deleted || draft.blocked === 'remote_deleted') entities.delete(draft.key)
    else entities.set(draft.key, draft)
  }
  return [...entities.values()]
}

export function folderOptions(folders: Draft[], excludedId?: string): Draft[] {
  const excluded = new Set(excludedId ? [excludedId] : [])
  let changed = true
  while (changed) {
    changed = false
    for (const folder of folders) if (folder.parentId && excluded.has(folder.parentId) && !excluded.has(folder.entityId)) { excluded.add(folder.entityId); changed = true }
  }
  return folders.filter(folder => !excluded.has(folder.entityId))
}

export function folderDepth(folder: Draft, folders: Draft[]): number {
  let parent = folder.parentId
  const seen = new Set([folder.entityId])
  let depth = 0
  while (parent && !seen.has(parent)) {
    seen.add(parent)
    const ancestor = folders.find(item => item.entityId === parent)
    if (!ancestor) break
    depth++
    parent = ancestor.parentId
  }
  return Math.min(depth, 8)
}

export function orderedFolders(folders: Draft[]): Draft[] {
  const result: Draft[] = []
  const visited = new Set<string>()
  const add = (folder: Draft) => {
    if (visited.has(folder.entityId)) return
    visited.add(folder.entityId)
    result.push(folder)
    for (const child of folders.filter(item => item.parentId === folder.entityId)) add(child)
  }
  for (const root of folders.filter(folder => !folder.parentId || !folders.some(parent => parent.entityId === folder.parentId))) add(root)
  // Malformed or temporarily incomplete ancestry must never hide an object.
  for (const folder of folders) add(folder)
  return result
}
