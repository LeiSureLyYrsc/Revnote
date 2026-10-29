import type { FullConfig, FullResult, Reporter, Suite } from '@playwright/test/reporter'
import { caseId, labelsIn, reportScope, writeReport, type SafeReport } from '../scripts/check-test-reports'

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

  onBegin(_config: FullConfig, suite: Suite): void {
    void _config
    this.suite = suite
    writeReport(this.report)
  }

  onError(): void {
    this.report.errors += 1
  }

  onEnd(result: FullResult): void {
    this.report.cases = (this.suite?.allTests() ?? []).map((test) => {
      const last = test.results.at(-1)
      return {
        id: caseId(test.id),
        labels: labelsIn([...test.titlePath(), ...test.tags].join(' ')),
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
