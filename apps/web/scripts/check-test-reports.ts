import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const UNIT_LABELS = ['contracts', 'proxy', 'storage', 'auth', 'sync', 'components'] as const
export const E2E_LABELS = [
  'workspace_flow',
  'draft_reload',
  'api_restart_recovery',
  'offline_recovery',
  'lost_response_replay',
  'ack_preserves_later_edit',
  'manual_conflict',
  'remote_delete_recovery',
  'single_writer_takeover',
  'refresh_rotation',
  'refresh_uncertainty',
  'session_identity_isolation',
  'resync_preservation',
  'cursor_integrity',
  'markdown_preservation',
  'mobile_layout',
] as const

export type Runner = 'vitest' | 'playwright'
export type Scope = 'full' | 'focused'
export type CaseStatus = 'passed' | 'failed' | 'skipped' | 'todo' | 'pending'
export interface SafeCase {
  id: string
  labels: string[]
  status: CaseStatus
  attempts: number
}
export interface SafeReport {
  schemaVersion: 1
  runner: Runner
  scope: Scope
  runId: string
  complete: boolean
  errors: number
  cases: SafeCase[]
}

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const allLabels = new Set<string>([...UNIT_LABELS, ...E2E_LABELS])

export function caseId(name: string): string {
  return createHash('sha256').update(name).digest('hex')
}

export function reportScope(args: string[], runner: Runner): Scope {
  // An unfamiliar option is focused too. A caller cannot bless a selected run
  // by setting an environment variable. Full is the plain stable script only.
  const plain = runner === 'vitest' ? ['run'] : ['test']
  return args.every((arg) => plain.includes(arg) || arg === '--') ? 'full' : 'focused'
}

export function labelsIn(title: string): string[] {
  return E2E_LABELS.filter((label) => new RegExp(`(?:^|[\\s@\\[])${label}(?=$|[\\s\\]])`).test(title))
}

export function junit(report: SafeReport): string {
  const failed = report.cases.filter((item) => item.status === 'failed').length
  const skipped = report.cases.filter((item) => !['passed', 'failed'].includes(item.status)).length
  const cases = report.cases.map((item) => {
    const properties = item.labels.map((label) => `<property name="label" value="${label}"/>`).join('')
    const outcome = item.status === 'failed' ? '<failure message="redacted"/>'
      : item.status !== 'passed' ? '<skipped/>' : ''
    return `<testcase classname="${report.runner}" name="case_${item.id}"><properties>${properties}</properties>${outcome}</testcase>`
  }).join('')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<testsuites tests="${report.cases.length}" failures="${failed}" errors="${report.errors}" skipped="${skipped}"><testsuite name="${report.runner}_${report.scope}" tests="${report.cases.length}" failures="${failed}" errors="${report.errors}" skipped="${skipped}">${cases}</testsuite></testsuites>\n`
}

export function writeReport(report: SafeReport): void {
  const dir = path.join(root, 'reports', report.scope)
  mkdirSync(dir, { recursive: true })
  // Only fixed labels, hashes and counts go to disk. Never failure messages,
  // stack traces, request bodies, arbitrary annotations or browser attachments.
  const jsonPath = path.join(dir, `${report.runner}.json`)
  const xmlPath = path.join(dir, `${report.runner}.xml`)
  writeFileSync(`${jsonPath}.tmp`, `${JSON.stringify(report, null, 2)}\n`)
  writeFileSync(`${xmlPath}.tmp`, junit(report))
  renameSync(`${jsonPath}.tmp`, jsonPath)
  renameSync(`${xmlPath}.tmp`, xmlPath)
}

export function validateReport(value: unknown, runner: Runner, scope: Scope, required: string[]): SafeReport {
  if (!value || typeof value !== 'object') throw new Error('Missing report object')
  const report = value as SafeReport
  if (report.schemaVersion !== 1 || report.runner !== runner || report.scope !== scope
    || !report.complete || report.errors !== 0 || !/^[a-zA-Z0-9_-]{8,100}$/.test(report.runId)
    || !Array.isArray(report.cases) || report.cases.length === 0) {
    throw new Error('Incomplete, empty or unsuccessful report')
  }
  const ids = new Set<string>()
  const labels = new Set<string>()
  for (const item of report.cases) {
    if (!item || !/^[a-f0-9]{64}$/.test(item.id) || ids.has(item.id)
      || item.status !== 'passed' || item.attempts !== 1 || !Array.isArray(item.labels)
      || item.labels.some((label) => !allLabels.has(label))) {
      throw new Error('A case failed, was omitted/retried, or has invalid metadata')
    }
    ids.add(item.id)
    item.labels.forEach((label) => labels.add(label))
  }
  if (required.some((label) => !labels.has(label))) throw new Error('Required acceptance labels are missing')
  return report
}

export function parseReportArgs(args: string[]): { scope: Scope; selected: Runner | 'all'; required: string[] } {
  let scope: Scope = 'full'
  let selected: Runner | 'all' = 'all'
  let required: string[] = []
  for (let index = 0; index < args.length; index += 1) {
    const option = args[index]
    const value = args[index + 1]
    if (option === '--') continue
    if (option === '--unit' || option === '--e2e') {
      const runner = option === '--unit' ? 'vitest' : 'playwright'
      if (selected !== 'all' && selected !== runner) throw new Error('Conflicting report selection')
      selected = runner
    } else if (option === '--scope' && (value === 'full' || value === 'focused')) {
      scope = value
      index += 1
    } else if (option === '--runner' && ['vitest', 'playwright', 'all'].includes(value)) {
      if (selected !== 'all' && selected !== value) throw new Error('Conflicting report selection')
      selected = value as Runner | 'all'
      index += 1
    } else if (option === '--require' && value) {
      required = value.split(',')
      index += 1
    } else throw new Error('Usage: check-test-reports.ts [--unit|--e2e] [--scope full|focused] [--runner vitest|playwright|all] [--require label,...]')
  }
  if (required.some((label) => !allLabels.has(label))) throw new Error('Unknown acceptance label')
  return { scope, selected, required }
}

function main(): void {
  const { scope, selected, required } = parseReportArgs(process.argv.slice(2))
  for (const runner of selected === 'all' ? ['vitest', 'playwright'] as const : [selected]) {
    const base = path.join(root, 'reports', scope, runner)
    const mandatory = scope === 'full' ? runner === 'vitest' ? [...UNIT_LABELS] : [...E2E_LABELS] : []
    const report = validateReport(JSON.parse(readFileSync(`${base}.json`, 'utf8')), runner, scope, [...mandatory, ...required])
    // Exact comparison to our restricted JUnit serialization is stronger than
    // trusting counters or regex-parsing arbitrary XML. Both formats must agree.
    if (readFileSync(`${base}.xml`, 'utf8') !== junit(report)) throw new Error('JUnit/JSON reports disagree')
    console.log(`${runner}/${scope}: ${report.cases.length} passed; zero skips, retries or errors`)
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main()
  } catch {
    console.error('Web report guard failed: missing, incomplete or rejected acceptance evidence.')
    process.exitCode = 1
  }
}
