import { spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { junit, reportScope, UNIT_LABELS, validateReport, writeReport } from '../scripts/check-test-reports'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const args = process.argv.slice(2).filter((arg) => arg !== '--')
const scope = reportScope(['run', ...args], 'vitest')
// Replace even on configuration/import/collection failures, before Vitest loads.
writeReport({ schemaVersion: 1, runner: 'vitest', scope, runId: randomUUID(), complete: false, errors: 0, cases: [] })
// Whitelist selectors, not runner configuration. New Vitest switches therefore
// cannot silently weaken the fixed config or enable diagnostic artifacts.
let valid = true
for (let index = 0; index < args.length; index += 1) {
  if (args[index] === '-t' || args[index] === '--testNamePattern') {
    if (!args[index + 1]) valid = false
    index += 1
  } else if (!/^[a-zA-Z0-9_./:\\-]+$/.test(args[index]) || args[index].startsWith('-')
    || /^(?:list|watch|related|run)$/.test(args[index])) valid = false
}
if (!valid) {
  console.error('The stable test entry point does not accept report/gate overrides.')
  process.exitCode = 1
} else {
  const binary = path.join(root, 'node_modules', 'vitest', 'vitest.mjs')
  const result = spawnSync(process.execPath, [binary, 'run', ...args], {
    cwd: root,
    stdio: ['ignore', 'inherit', 'ignore'],
    env: process.env,
  })
  process.exitCode = result.status ?? 1
  if (process.exitCode === 0) {
    try {
      const base = path.join(root, 'reports', scope, 'vitest')
      const report = validateReport(JSON.parse(readFileSync(`${base}.json`, 'utf8')), 'vitest', scope,
        scope === 'full' ? [...UNIT_LABELS] : [])
      if (readFileSync(`${base}.xml`, 'utf8') !== junit(report)) throw new Error('Report mismatch')
    } catch {
      console.error('Vitest acceptance report rejected (empty, skip/todo, error, retry or missing group).')
      process.exitCode = 1
    }
  }
}
