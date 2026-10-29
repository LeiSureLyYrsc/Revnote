import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  caseId, E2E_LABELS, junit, labelsIn, parseReportArgs, reportScope, safeSource, UNIT_LABELS, validateReport, type SafeReport,
} from '../../../scripts/check-test-reports'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const specDir = path.join(webRoot, 'e2e')
const specFiles = readdirSync(specDir).filter((name) => name.endsWith('.spec.ts'))
const specSources = specFiles.map((name) => readFileSync(path.join(specDir, name), 'utf8'))
const specTitles = specSources.flatMap((text) => [...text.matchAll(/^test\(\s*'([^']*)'/gm)].map((match) => match[1]))

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
  it('permits bounded test source locations but never arbitrary diagnostic paths', () => {
    expect(safeSource(path.resolve('e2e/workspace.spec.ts'), 7)).toEqual({ source: 'e2e/workspace.spec.ts', line: 7 })
    expect(safeSource(path.resolve('../secret_value'), 7)).toEqual({})
    expect(safeSource(path.resolve('src/App.tsx'), 7)).toEqual({})
    expect(safeSource(path.resolve('e2e/workspace.spec.ts'), 0)).toEqual({})
    expect(safeSource(path.resolve('e2e/workspace.spec.ts'), 1.5)).toEqual({})
  })

  it('keeps locating metadata inside test-owned files and normalizes Windows separators', () => {
    const unit = path.join(webRoot, 'tests', 'unit', 'sync', 'engine.test.ts')
    // The native form and the forward-slash form name the same absolute file on
    // every platform, so both must normalize to one portable report entry.
    expect(safeSource(unit, 42)).toEqual({ source: 'tests/unit/sync/engine.test.ts', line: 42 })
    expect(safeSource(unit.replaceAll('\\', '/'), 42)).toEqual({ source: 'tests/unit/sync/engine.test.ts', line: 42 })
    // Backslash paths are absolute only on win32. On a POSIX host the same
    // string is a *relative* path, which the guard must refuse rather than
    // reinterpret: resolving it would silently read an unrelated file.
    const backslash = unit.replaceAll('/', '\\')
    if (process.platform === 'win32') {
      expect(safeSource(backslash, 42)).toEqual({ source: 'tests/unit/sync/engine.test.ts', line: 42 })
    } else {
      expect(path.isAbsolute(backslash)).toBe(false)
      expect(safeSource(backslash, 42)).toEqual({})
    }
    const component = path.join(webRoot, 'tests', 'components', 'identity.test.tsx')
    expect(safeSource(component, 3)).toEqual({ source: 'tests/components/identity.test.tsx', line: 3 })
    // Everything below is rejected: scripts, sources, configs, escapes and non-spec files.
    for (const file of [
      path.join(webRoot, 'scripts', 'check-test-reports.ts'),
      path.join(webRoot, 'src', 'App.tsx'),
      path.join(webRoot, 'e2e', 'fixtures.ts'),
      path.join(webRoot, 'e2e', 'nested', 'workspace.spec.ts'),
      path.join(webRoot, 'e2e', '..', 'scripts', 'check-test-reports.ts'),
      path.join(webRoot, 'tests', 'unit', '..', '..', 'src', 'App.tsx'),
      'e2e/workspace.spec.ts',
      'https://example.test/path.spec.ts',
      '',
    ]) expect(safeSource(file, 1)).toEqual({})
    // Line numbers are bounded integers, never NaN/Infinity/strings.
    for (const line of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 1_000_001, Number.MAX_SAFE_INTEGER + 1]) {
      expect(safeSource(path.resolve('e2e/workspace.spec.ts'), line)).toEqual({})
    }
    // The persisted line must also exist in the real file; a huge index is
    // rejected rather than written as a plausible-looking location.
    expect(safeSource(path.resolve('e2e/workspace.spec.ts'), 1_000_000)).toEqual({})
    expect(safeSource(path.resolve('e2e/workspace.spec.ts'), 1)).toEqual({ source: 'e2e/workspace.spec.ts', line: 1 })
  })

  it('locates a browser case at its real on-disk declaration instead of a drifted runner index', () => {
    // Runners can report an index that does not name a line of the real file.
    // The exact declared title is read from disk, and a runner index that points
    // beyond the file is discarded rather than persisted. The expected line is
    // derived from the file itself so that editing a spec cannot silently turn
    // an unrelated change red, while a wrong lookup still fails.
    const file = path.join(specDir, 'workspace.spec.ts')
    const lines = readFileSync(file, 'utf8').split('\n')
    const declared = (fullTitle: string) => lines.findIndex((text) => text.startsWith(`test('${fullTitle}'`)) + 1
    for (const title of ['workspace_flow', 'draft_reload', 'markdown_preservation', 'mobile_layout'] as const) {
      const full = specTitles.find((value) => value.startsWith(`${title} `))!
      const line = declared(full)
      expect(line, `${title} must exist in the real file`).toBeGreaterThan(0)
      // A bogus runner index is ignored in favour of the on-disk declaration.
      expect(safeSource(file, 999_999, full), title).toEqual({ source: 'e2e/workspace.spec.ts', line })
      expect(safeSource(file, 2, full), title).toEqual({ source: 'e2e/workspace.spec.ts', line })
    }
    // Without a matching title the out-of-range index is refused, not clamped.
    expect(safeSource(file, 999_999)).toEqual({})
    expect(safeSource(file, 0)).toEqual({})
  })

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

  it('rejects any persisted field that could carry messages, stacks or request bodies', () => {
    for (const change of [
      { cases: [{ ...report().cases[0], message: 'note body' }] },
      { cases: [{ ...report().cases[0], error: 'stack trace' }] },
      { cases: [{ ...report().cases[0], stack: 'at <anonymous>' }] },
      { cases: [{ ...report().cases[0], attachments: ['trace.zip'] }] },
      { cases: [{ ...report().cases[0], source: 'src/App.tsx', line: 4 }] },
      { cases: [{ ...report().cases[0], source: 'e2e/../../secret.ts', line: 4 }] },
      { cases: [{ ...report().cases[0], source: 'e2e/workspace.spec.ts' }] },
      { cases: [{ ...report().cases[0], line: 4 }] },
      { cases: [{ ...report().cases[0], source: 'e2e/workspace.spec.ts', line: 0 }] },
    ]) expect(() => validateReport({ ...report(), ...change }, 'vitest', 'focused', [])).toThrow()
    const located = { ...report(), cases: [{ ...report().cases[0], source: 'e2e/workspace.spec.ts', line: 12 }] }
    expect(validateReport(located, 'vitest', 'focused', []).cases[0]).toMatchObject({ source: 'e2e/workspace.spec.ts', line: 12 })
  })

  it('enforces the frozen load-bearing inventory without a hardcoded total count', () => {
    expect(() => validateReport(report(), 'vitest', 'focused', [...UNIT_LABELS])).toThrow()
    const browser = { ...report(), runner: 'playwright' as const }
    expect(() => validateReport(browser, 'playwright', 'focused', [...E2E_LABELS])).toThrow()
    browser.cases = E2E_LABELS.map((label) => ({
      id: caseId(`browser ${label}`), labels: [label], status: 'passed' as const, attempts: 1,
    }))
    expect(validateReport(browser, 'playwright', 'focused', [...E2E_LABELS])).toBe(browser)
    // A case can only belong to its own runner's inventory, exactly one label.
    expect(() => validateReport({ ...report(), cases: [{ ...report().cases[0], labels: ['workspace_flow'] }] }, 'vitest', 'focused', [])).toThrow()
    expect(() => validateReport({ ...browser, cases: [{ ...browser.cases[0], labels: ['proxy'] }] }, 'playwright', 'focused', [])).toThrow()
    expect(() => validateReport({ ...browser, cases: [{ ...browser.cases[0], labels: [] }] }, 'playwright', 'focused', [])).toThrow()
    expect(() => validateReport({ ...browser, cases: [{ ...browser.cases[0], labels: ['workspace_flow', 'draft_reload'] }] }, 'playwright', 'focused', [])).toThrow()
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

  it('keeps one real browser case per frozen label and no placeholder tests', () => {
    // Every browser test must be load-bearing: one frozen label in its title,
    // no untagged filler, no skip/todo/only/fixme anywhere in the suite.
    expect(specTitles.length).toBeGreaterThanOrEqual(E2E_LABELS.length)
    for (const source of specSources) {
      expect(source).not.toMatch(/test\.(?:skip|fixme|only|todo|fail|slow)\s*\(/)
      expect(source).not.toMatch(/describe\.(?:skip|only)\s*\(/)
      expect(source).not.toMatch(/\bxtest\s*\(/)
    }
    const tagged = specTitles.map((title) => labelsIn(title))
    for (const [index, labels] of tagged.entries()) {
      expect(labels.length, `exactly one frozen label in: ${specTitles[index]}`).toBe(1)
    }
    for (const label of E2E_LABELS) {
      expect(tagged.filter((labels) => labels[0] === label).length, `cases for ${label}`).toBe(1)
    }
    expect(new Set(tagged.flat())).toEqual(new Set(E2E_LABELS))
  })
})
