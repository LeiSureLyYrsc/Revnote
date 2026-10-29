import type { FullConfig, FullResult, Reporter, Suite, TestCase } from '@playwright/test/reporter'
import { caseId, labelsIn, reportScope, safeSource, writeReport, type CaseStatus, type SafeReport } from '../scripts/check-test-reports'

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
    this.report.cases = (this.suite?.allTests() ?? []).map((test) => ({
      id: caseId(test.id),
      labels: labelsIn([...test.titlePath(), ...test.tags].join(' ')),
      ...collect(test),
      ...safeSource(test.location.file, test.location.line, test.title),
    }))
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
