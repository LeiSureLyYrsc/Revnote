import { describe, expect, it } from 'vitest'
import {
  caseId, E2E_LABELS, junit, labelsIn, parseReportArgs, reportScope, UNIT_LABELS, validateReport, type SafeReport,
} from '../../../scripts/check-test-reports'

function report(): SafeReport {
  return {
    schemaVersion: 1,
    runner: 'vitest',
    scope: 'focused',
    runId: '12345678-1234-1234-1234-123456789abc',
    complete: true,
    errors: 0,
    cases: [{ id: caseId('safe case'), labels: ['proxy'], status: 'passed', attempts: 1 }],
  }
}

describe('acceptance evidence guard', () => {
  it('keeps CI unit and e2e aliases full by default while no arguments require both', () => {
    expect(parseReportArgs([])).toEqual({ scope: 'full', selected: 'all', required: [] })
    expect(parseReportArgs(['--unit'])).toEqual({ scope: 'full', selected: 'vitest', required: [] })
    expect(parseReportArgs(['--', '--e2e'])).toEqual({ scope: 'full', selected: 'playwright', required: [] })
    expect(parseReportArgs(['--unit', '--scope', 'focused', '--require', 'proxy'])).toEqual({
      scope: 'focused', selected: 'vitest', required: ['proxy'],
    })
    expect(() => parseReportArgs(['--unit', '--e2e'])).toThrow()
    expect(() => parseReportArgs(['--e2e', '--runner', 'vitest'])).toThrow()
  })

  it('accepts a nonempty successful focused report only as focused evidence', () => {
    expect(validateReport(report(), 'vitest', 'focused', ['proxy']).cases).toHaveLength(1)
    expect(() => validateReport(report(), 'vitest', 'full', ['proxy'])).toThrow()
    expect(() => validateReport(report(), 'playwright', 'focused', ['proxy'])).toThrow()
  })

  it('rejects empty, incomplete, failed, skipped, todo, retried or uncollected evidence', () => {
    for (const change of [
      { cases: [] }, { complete: false }, { errors: 1 }, { runId: '' },
      ...(['failed', 'skipped', 'todo', 'pending'] as const).map((status) => ({
        cases: [{ ...report().cases[0], status }],
      })),
      { cases: [{ ...report().cases[0], attempts: 2 }] },
      { cases: [{ ...report().cases[0], labels: ['unreviewed'] }] },
      { cases: [report().cases[0], report().cases[0]] },
    ]) expect(() => validateReport({ ...report(), ...change }, 'vitest', 'focused', [])).toThrow()
  })

  it('enforces the frozen load-bearing inventory without a hardcoded total count', () => {
    expect(() => validateReport(report(), 'vitest', 'focused', [...UNIT_LABELS])).toThrow()
    const browser = { ...report(), runner: 'playwright' as const }
    expect(() => validateReport(browser, 'playwright', 'focused', [...E2E_LABELS])).toThrow()
    browser.cases[0].labels = [...E2E_LABELS]
    expect(validateReport(browser, 'playwright', 'focused', [...E2E_LABELS])).toBe(browser)
  })

  it('never calls a selected invocation full despite an environment override', () => {
    expect(reportScope(['run'], 'vitest')).toBe('full')
    expect(reportScope(['run', 'tests/unit/proxy'], 'vitest')).toBe('focused')
    expect(reportScope(['test'], 'playwright')).toBe('full')
    expect(reportScope(['test', '--grep', 'workspace_flow'], 'playwright')).toBe('focused')
    expect(reportScope(['test', '--list'], 'playwright')).toBe('focused')
  })

  it('recognizes fixed title tags only and emits redacted JUnit case identifiers', () => {
    expect(labelsIn('case @workspace_flow [markdown_preservation] mobile_layout')).toEqual([
      'workspace_flow', 'markdown_preservation', 'mobile_layout',
    ])
    expect(labelsIn('not_workspace_flow_suffix')).toEqual([])
    const xml = junit(report())
    expect(xml).toContain('tests="1"')
    expect(xml).toContain('value="proxy"')
    expect(xml).not.toContain('safe case')
  })
})
