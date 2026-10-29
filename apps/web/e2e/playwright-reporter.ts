import type { FullConfig, FullResult, Reporter, Suite, TestCase } from '@playwright/test/reporter'
import {
  caseId, labelsIn, reportScope, safeAssertion, safeSource, safeStage, STAGE_ANNOTATION, writeReport,
  type CaseStatus, type SafeReport,
} from '../scripts/check-test-reports'

export default class SafePlaywrightReporter implements Reporter {
  private suite?: Suite
  private report: SafeReport = {
    schemaVersion: 1,
    runner: 'playwright',
    scope: reportScope(process.argv.slice(2), 'playwright'),
    runId: process.env.WEB_E2E_RUN_ID ?? 'unowned-run',
    complete: false,
    errors: 0,
    cases: [],
  }

  /** Only hashed ids, frozen labels and whitelisted test locations leave here. */
  private locate(collect: (test: TestCase) => { status: CaseStatus; attempts: number }): void {
    this.report.cases = (this.suite?.allTests() ?? []).map((test) => {
      const collected = collect(test)
      return {
        id: caseId(test.id),
        labels: labelsIn([...test.titlePath(), ...test.tags].join(' ')),
        ...collected,
        ...safeSource(test.location.file, test.location.line, test.title),
        ...failureLocation(test, collected.status),
      }
    })
  }

  onBegin(_config: FullConfig, suite: Suite): void {
    void _config
    this.suite = suite
    // Collect at load time so a timeout, crash or interrupted worker still leaves
    // label + source:line + case hash locatable without any error text.
    this.locate(() => ({ status: 'pending', attempts: 1 }))
    writeReport(this.report)
  }

  onError(): void {
    // Never persist the error value: only the count is safe.
    this.report.errors += 1
  }

  onEnd(result: FullResult): void {
    this.locate((test) => {
      const last = test.results.at(-1)
      return {
        status: last?.status === 'passed' && test.expectedStatus === 'passed' ? 'passed'
          : last?.status === 'skipped' ? 'skipped' : 'failed',
        attempts: test.results.length,
      }
    })
    this.report.complete = true
    if (result.status !== 'passed') this.report.errors += 1
    writeReport(this.report)
    console.log(`playwright/${this.report.scope}: cases=${this.report.cases.length}; errors=${this.report.errors}; details redacted`)
  }
}

/**
 * What can be said about *where a failing case got stuck*.
 *
 * Two mutually exclusive kinds of evidence:
 * - a real assertion location (`source:line` inside a test-owned spec), or
 * - the last bounded stage marker when Playwright reports no in-file location
 *   at all (timeout, fixture phase, a helper's own guard).
 *
 * The assertion wins when present: it is strictly more precise. A stage marker
 * is never mixed with a location, and a passing case carries neither. Message,
 * stack, value, snippet and attachments are never read.
 */
function failureLocation(test: TestCase, status: CaseStatus): ReturnType<typeof safeAssertion> | ReturnType<typeof safeStage> {
  if (status !== 'failed') return {}
  const assertion = failingAssertion(test)
  if ('assertion' in assertion) return assertion
  return failingStage(test)
}

/**
 * File + line of the assertion that failed, or nothing.
 *
 * Deliberately reads only `error.location`: `message`, `stack`, `value` and
 * `attachments` live on the same objects and must never be touched here. The
 * value is unchecked on the way out, so the report guard re-validates the
 * whitelisted file and the real on-disk line before it is persisted.
 */
function failingAssertion(test: TestCase): ReturnType<typeof safeAssertion> {
  const failure = test.results.at(-1)?.error
  const location = failure?.location
  if (!location) return {}
  return safeAssertion(location.file, location.line)
}

/**
 * The last stage marker the worker announced, or nothing.
 *
 * `fixtures.stage()` appends exactly one `stage` annotation to the running
 * test, and Playwright ships annotations to the reporter as structured data
 * (`{ type, description }`), unlike the error text this reporter never reads.
 * The description is re-validated against the frozen vocabulary: a marker that
 * is not in it yields no stage rather than an arbitrary string in the report.
 */
function failingStage(test: TestCase): ReturnType<typeof safeStage> {
  const annotations = test.results.at(-1)?.annotations ?? []
  const marker = annotations.filter((annotation) => annotation.type === STAGE_ANNOTATION).at(-1)
  if (marker === undefined) return {}
  return safeStage(marker.description)
}
