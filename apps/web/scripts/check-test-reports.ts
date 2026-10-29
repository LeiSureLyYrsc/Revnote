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

/**
 * Frozen vocabulary of bounded failure-stage markers.
 *
 * A case can fail *outside* any assertion: a renderer that never reports its
 * crash, a writer lock that is still held, a helper poll that timed out. Such
 * a failure has no in-file location, so the useful evidence is which helper
 * phase was reached. Only these identifiers may be recorded, and only these
 * may be released: the value is test-authored (an annotation description, or
 * a case key in a report), so the guard re-validates it instead of trusting it.
 * `run_web_e2e.py` mirrors this set when printing diagnostics.
 */
export const STAGES = [
  'unknown',
  'login_view',
  'login_ok',
  'note_create_enter',
  'note_created',
  'note_open',
  'idle_enter',
  'idle_ok',
  'edit_enter',
  'edit_filled',
  'edit_durable',
  'retry_enter',
  'retry_clicked',
  'follower_enter',
  'follower_ready',
  'crash_enter',
  'crash_sent',
  'crash_confirmed',
  'crash_closed',
  'lock_free_enter',
  'lock_free_ok',
  'lock_free_timeout',
  'server_body_enter',
  'server_body_ok',
] as const
export type StageName = (typeof STAGES)[number]
/** Annotation type carrying one stage marker from a worker to the reporter. */
export const STAGE_ANNOTATION = 'stage'
const stagePattern = /^[a-z][a-z0-9_]{0,63}$/
const stageNames = new Set<string>(STAGES)

/** Fixed stage identifier, or `unknown`; the input itself is never echoed. */
export function stageName(value: unknown): StageName {
  return typeof value === 'string' && stageNames.has(value) ? (value as StageName) : 'unknown'
}

/** A report-ready stage entry, or nothing when the value is not in the vocabulary. */
export function safeStage(value: unknown): { stage: StageName } | Record<string, never> {
  if (typeof value !== 'string' || !stagePattern.test(value) || !stageNames.has(value)) return {}
  return { stage: value as StageName }
}

export interface SafeCase {
  id: string
  labels: string[]
  status: CaseStatus
  attempts: number
  /** Declaration location of the test itself (always present for a located case). */
  source?: string
  line?: number
  /** Location of the failing assertion, when the runner reported a test-owned one. */
  assertion?: string
  assertionLine?: number
  /** Last bounded helper stage reached, when the failure had no assertion location. */
  stage?: StageName
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
// A report may only claim labels from its own runner's frozen inventory, so a
// browser run cannot borrow a unit group (or the reverse) to look complete.
const runnerLabels: Record<Runner, ReadonlySet<string>> = {
  vitest: new Set<string>(UNIT_LABELS),
  playwright: new Set<string>(E2E_LABELS),
}
// Test-owned filenames only: no arbitrary paths, source excerpts, annotations,
// messages, stacks or request bodies may ever reach a persisted report.
const safeSourcePattern = /^(?:e2e\/[a-zA-Z0-9_-]+\.spec\.ts|tests\/(?:unit|components)\/[a-zA-Z0-9_./-]+\.(?:test|spec)\.tsx?)$/
// A *failing assertion* may additionally live in the shared browser helpers.
// Playwright reports the deepest user frame that threw, and the bounded guards
// this report exists for — the 20s crash timer, the writer-lock poll, the
// follower visibility check — are in `e2e/fixtures.ts`, not in a spec. Refusing
// that file turned a known failing line into `assertion=unknown`. The pattern
// still names one exact file inside `e2e/`, so nothing is opened up: no
// directory, no `..`, no arbitrary helper, no product source.
const safeAssertionPattern = /^(?:e2e\/[a-zA-Z0-9_-]+\.spec\.ts|e2e\/fixtures\.ts|tests\/(?:unit|components)\/[a-zA-Z0-9_./-]+\.(?:test|spec)\.tsx?)$/
const reportKeys = new Set(['schemaVersion', 'runner', 'scope', 'runId', 'complete', 'errors', 'cases'])
const caseKeys = new Set(['id', 'labels', 'status', 'attempts', 'source', 'line', 'assertion', 'assertionLine', 'stage'])

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

/**
 * Resolves an absolute path onto a test-owned file, or refuses it outright.
 * Requiring an absolute path keeps the result independent of the cwd; the
 * remaining rules keep arbitrary paths, source excerpts and dynamically
 * supplied annotations out of a persisted report.
 */
function testOwnedLocation(
  file: unknown,
  pattern: RegExp = safeSourcePattern,
): { absolute: string; relative: string } | undefined {
  // Only reporters feed this, and they always pass an absolute module path.
  if (typeof file !== 'string' || !path.isAbsolute(file)) return undefined
  const relative = path.relative(root, file).replaceAll('\\', '/')
  if (!pattern.test(relative) || relative.split('/').includes('..')) return undefined
  return { absolute: file, relative }
}

export function safeSource(file: string, line: number, title?: string): { source: string; line: number } | Record<string, never> {
  const owned = testOwnedLocation(file)
  if (owned === undefined) return {}
  // Exact declaration line read from the file wins: a runner-reported index can
  // drift from the on-disk source. The runner index is only a fallback, and only
  // when it can name a real line in this file; otherwise metadata is omitted.
  const text = readTestFile(owned.absolute)
  if (text === undefined) return {}
  const candidate = text.lineOf(title)
    ?? (Number.isSafeInteger(line) && line >= 1 && line <= text.lineCount ? line : undefined)
  if (candidate === undefined || candidate < 1 || candidate > 1_000_000) return {}
  return { source: owned.relative, line: candidate }
}

/**
 * Keeps only the *file and line of a failing assertion*, never its message.
 *
 * Playwright hands the location out inside the same error object as `message`,
 * `stack` and `value`; this function reads exactly two properties so the rest
 * cannot be carried along by accident. The file must pass the assertion
 * whitelist — a spec, a unit test, or the shared `e2e/fixtures.ts` helpers,
 * which is where a bounded guard throws — and the line must really exist in
 * that file: a drifted or invented index would point the reader at unrelated
 * content, which is worse than no evidence at all.
 */
export function safeAssertion(file: unknown, line: unknown): { assertion: string; assertionLine: number } | Record<string, never> {
  const owned = testOwnedLocation(file, safeAssertionPattern)
  if (owned === undefined) return {}
  if (typeof line !== 'number' || !Number.isSafeInteger(line) || line < 1 || line > 1_000_000) return {}
  const text = readTestFile(owned.absolute)
  if (text === undefined || line > text.lineCount) return {}
  return { assertion: owned.relative, assertionLine: line }
}

/** Reads a test-owned file once per call: these files are small and bounded. */
function readTestFile(file: string): { lineCount: number; lineOf(title?: string): number | undefined } | undefined {
  let lines: string[]
  try {
    lines = readFileSync(file, 'utf8').split('\n')
  } catch {
    return undefined
  }
  return {
    lineCount: lines.length,
    lineOf(title) {
      if (!title) return undefined
      const matches = lines.flatMap((text, index) => {
        for (const quote of ["'", '"', '`']) {
          if (text.includes('(' + quote + title + quote)) return [index + 1]
        }
        return []
      })
      return matches.length === 1 ? matches[0] : undefined
    },
  }
}

/** True only when a report-supplied relative test path really has that line. */
function lineExists(relative: string, line: number): boolean {
  const text = readTestFile(path.join(root, relative))
  return text !== undefined && line <= text.lineCount
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
  // Only fixed labels, hashes, counts and bounded test source locations go to disk. Never failure messages,
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
    || !Array.isArray(report.cases) || report.cases.length === 0
    || Object.keys(report).some((key) => !reportKeys.has(key))) {
    throw new Error('Incomplete, empty or unsuccessful report')
  }
  const ids = new Set<string>()
  const labels = new Set<string>()
  const owned = runnerLabels[runner]
  for (const item of report.cases) {
    if (!item || typeof item !== 'object' || Object.keys(item).some((key) => !caseKeys.has(key))
      || !/^[a-f0-9]{64}$/.test(item.id) || ids.has(item.id)
      || item.status !== 'passed' || item.attempts !== 1 || !Array.isArray(item.labels)
      // Exactly one acceptance label per case: a case cannot inflate coverage by
      // claiming several groups, and empty labels prove nothing.
      || item.labels.length !== 1 || item.labels.some((label) => !owned.has(label))
      // Optional locating metadata must still be a whitelisted test file.
      || (item.source !== undefined && (
        typeof item.source !== 'string' || !safeSourcePattern.test(item.source)
        || item.source.split('/').includes('..')))
      || (item.source === undefined) !== (item.line === undefined)
      || (item.line !== undefined && (!Number.isSafeInteger(item.line) || item.line < 1 || item.line > 1_000_000))
      // A failing-assertion location is the same restricted kind of evidence: a
      // whitelisted test file (or the shared `e2e/fixtures.ts` helper module,
      // where the bounded guards throw) plus a line that really exists inside
      // it. It is all-or-nothing, so a half-populated location cannot look
      // trustworthy, and no message, stack, value, snippet or attachment may
      // ride along.
      || (item.assertion === undefined) !== (item.assertionLine === undefined)
      || (item.assertion !== undefined && (
        typeof item.assertion !== 'string' || !safeAssertionPattern.test(item.assertion)
        || item.assertion.split('/').includes('..')
        || typeof item.assertionLine !== 'number'
        || !Number.isSafeInteger(item.assertionLine) || item.assertionLine < 1 || item.assertionLine > 1_000_000
        || !lineExists(item.assertion, item.assertionLine)))
      // A stage marker is only ever a frozen identifier: the pattern rejects
      // anything that is not an identifier, and the vocabulary rejects an
      // identifier that no helper emits, so a test-authored string cannot carry
      // note text or a path into the report. It is the *fallback* locator, so it
      // may not accompany a real assertion location: claiming both would let a
      // report look more precise than the run actually was.
      || (item.stage !== undefined && (
        typeof item.stage !== 'string' || !stagePattern.test(item.stage) || !stageNames.has(item.stage)))
      || (item.stage !== undefined && item.assertion !== undefined)) {
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
