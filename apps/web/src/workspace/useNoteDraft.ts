import { useEffect, useMemo, useRef, useState } from 'react'
import type { DraftInput } from '../domain/contracts'
import type { WorkspacePort } from './ports'

export type LocalSaveStatus = 'saved' | 'writing' | 'failed'
export interface NoteDocument { id: string; title: string; bodyMd: string; parentId: string | null }
const unsavedBuffers = new WeakMap<WorkspacePort, Map<string, NoteDocument>>()
const saveQueues = new WeakMap<WorkspacePort, Map<string, Promise<void>>>()

/** Conflict decisions wait for all UI input already queued for this partition. */
export async function flushNoteWrites(port: WorkspacePort): Promise<void> {
  const queues = saveQueues.get(port)
  if (queues) await Promise.all([...queues.values()])
  if (unsavedBuffers.get(port)?.size) throw new Error('Uncommitted editor input must be saved or exported first')
}

/** Mounted with a partition/entity/ownership key. A late completion cannot mark a newer edit saved. */
export function useNoteDraft(port: WorkspacePort, initial: NoteDocument) {
  const buffers = useMemo(() => {
    const records = unsavedBuffers.get(port) ?? new Map<string, NoteDocument>()
    unsavedBuffers.set(port, records)
    return records
  }, [port])
  const buffer = buffers.get(initial.id)
  const [document, setDocument] = useState(buffer ?? initial)
  const [status, setStatus] = useState<LocalSaveStatus>(buffer ? 'failed' : 'saved')
  const current = useRef(buffer ?? initial)
  const version = useRef(0)
  const queues = saveQueues.get(port) ?? new Map<string, Promise<void>>()
  if (!saveQueues.has(port)) saveQueues.set(port, queues)
  const mounted = useRef(true)
  const generation = port.ownershipGeneration
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (buffers.size) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [buffers])
  useEffect(() => {
    // Remote updates may refresh a clean editor; never replace uncommitted input.
    if (!buffers.has(initial.id)) {
      const loaded = { id: initial.id, title: initial.title, bodyMd: initial.bodyMd, parentId: initial.parentId }
      current.current = loaded; setDocument(loaded)
    }
  }, [buffers, initial.id, initial.title, initial.bodyMd, initial.parentId])

  function persist(next: NoteDocument) {
    if (port.readOnly) return
    current.current = next
    buffers.set(initial.id, next)
    setDocument(next)
    setStatus('writing')
    const capturedVersion = ++version.current
    const input: DraftInput = { entityType: 'note', entityId: initial.id, title: next.title, bodyMd: next.bodyMd, parentId: next.parentId }
    const capturedPort = port
    // Serial UI writes preserve input order; the repository still owns atomic versioning.
    const pending = (queues.get(initial.id) ?? Promise.resolve()).catch(() => undefined).then(async () => {
      await capturedPort.save(input)
    }).then(() => {
      if (buffers.get(initial.id) === next) buffers.delete(initial.id)
      if (mounted.current && capturedVersion === version.current && generation === capturedPort.ownershipGeneration) setStatus('saved')
    }, () => {
      if (mounted.current && capturedVersion === version.current) setStatus('failed')
    })
    queues.set(initial.id, pending)
  }

  return { document, status, updateTitle: (title: string) => persist({ ...current.current, title }), updateBody: (bodyMd: string) => persist({ ...current.current, bodyMd }), retry: () => persist(current.current), current }
}
