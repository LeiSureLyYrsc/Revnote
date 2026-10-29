import { describe, expect, it, vi } from 'vitest'
import { DEPLOYMENT, entityKey, newId, partitionKey, type Partition } from '../../../src/domain/ids'

describe('client identity and partition keys', () => {
  it('uses platform cryptographic UUIDs without time or sequence prediction', () => {
    const first = '12345678-1234-4234-8234-123456789abc'
    const second = 'abcdefab-1234-4234-8234-123456789abc'
    const generate = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(second)
    vi.stubGlobal('crypto', { randomUUID: generate })
    expect(newId()).toBe(first)
    expect(newId()).toBe(second)
    expect(generate).toHaveBeenCalledTimes(2)
    expect(generate).toHaveBeenCalledWith()
  })

  it('keeps note and folder with the same UUID in different entity namespaces', () => {
    const id = '12345678-1234-4234-8234-123456789abc'
    expect(entityKey('note', id)).toBe(`note:${id}`)
    expect(entityKey('folder', id)).toBe(`folder:${id}`)
    expect(entityKey('note', id)).not.toBe(entityKey('folder', id))
  })

  it('includes deployment, user and workspace in deterministic isolated keys', () => {
    const partition: Partition = { deployment: DEPLOYMENT, userId: 'user-a', workspaceId: 'workspace-a' }
    expect(DEPLOYMENT).toBe('revnote-same-origin-v1')
    expect(partitionKey({ ...partition })).toBe(partitionKey(partition))
    expect(JSON.parse(partitionKey(partition))).toEqual([DEPLOYMENT, 'user-a', 'workspace-a'])
    for (const change of [
      { deployment: 'another-deployment' }, { userId: 'user-b' }, { workspaceId: 'workspace-b' },
    ]) expect(partitionKey({ ...partition, ...change })).not.toBe(partitionKey(partition))
  })

  it('cannot collide by placing separators or quotes in different partition fields', () => {
    const left = { deployment: 'deploy', userId: 'user:workspace', workspaceId: 'id' }
    const right = { deployment: 'deploy', userId: 'user', workspaceId: 'workspace:id' }
    expect(partitionKey(left)).not.toBe(partitionKey(right))
    const escaped = { deployment: 'deploy', userId: 'user","workspace', workspaceId: 'id\\end' }
    expect(JSON.parse(partitionKey(escaped))).toEqual(Object.values(escaped))
  })
})
