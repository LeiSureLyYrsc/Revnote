import { randomUUID } from 'node:crypto'
import type { Reporter, TestModule } from 'vitest/node'
import { caseId, reportScope, UNIT_LABELS, writeReport, type SafeCase, type SafeReport } from '../scripts/check-test-reports'

export default class SafeVitestReporter implements Reporter {
  private report: SafeReport = {
    schemaVersion: 1,
    runner: 'vitest',
    scope: reportScope(process.argv.slice(2), 'vitest'),
    runId: randomUUID(),
    complete: false,
    errors: 0,
    cases: [],
  }

  onTestRunStart(): void {
    // Replace any earlier green report before collection/execution can fail.
    writeReport(this.report)
  }

  onTestRunEnd(modules: ReadonlyArray<TestModule>, errors: ReadonlyArray<unknown>): void {
    const cases: SafeCase[] = []
    let suiteErrors = 0
    for (const module of modules) {
      suiteErrors += module.errors().length
      for (const suite of module.children.allSuites()) suiteErrors += suite.errors().length
      const file = module.moduleId.replaceAll('\\', '/')
      const group = file.includes('/tests/components/') ? 'components'
        : file.match(/\/tests\/unit\/([^/]+)\//)?.[1]
      for (const test of module.children.allTests()) {
        const result = test.result()
        const status = result.state === 'passed' ? 'passed'
          : result.state === 'failed' ? 'failed'
            : test.options.mode === 'todo' ? 'todo' : 'skipped'
        cases.push({
          id: caseId(`${file}:${test.fullName}`),
          labels: group && UNIT_LABELS.some((label) => label === group) ? [group] : [],
          status,
          attempts: 1 + (test.diagnostic()?.retryCount ?? 0),
        })
      }
    }
    this.report = { ...this.report, cases, complete: true, errors: errors.length + suiteErrors }
    writeReport(this.report)
    console.log(`vitest/${this.report.scope}: cases=${cases.length}; errors=${this.report.errors}; details redacted`)
  }
}
