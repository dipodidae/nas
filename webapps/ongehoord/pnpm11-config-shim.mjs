#!/usr/bin/env node
// pnpm 11 config shim — run inside the Docker build against the app source
// (cwd = the submodule checkout) right before `pnpm install`.
//
// Upstream (dipodidae/ongehoord-ui-content) bumped its `packageManager` to
// pnpm 11 but still ships pnpm-10-shaped config. pnpm 11 dropped two of those
// settings, so a frozen install fails:
//
//   1. `pnpm.overrides` in package.json is no longer read (overrides moved to
//      pnpm-workspace.yaml). pnpm resolves zero overrides while pnpm-lock.yaml
//      still records them → ERR_PNPM_LOCKFILE_CONFIG_MISMATCH.
//   2. `onlyBuiltDependencies` was replaced by `allowBuilds` (pnpm 11.0). The
//      old key is ignored, so every native build script (better-sqlite3,
//      sharp, esbuild, …) counts as unreviewed and, with strictDepBuilds
//      defaulting to true, the install hard-fails with ERR_PNPM_IGNORED_BUILDS.
//
// This shim mirrors both settings into their pnpm 11 homes in
// pnpm-workspace.yaml, reading the values straight from the upstream config so
// it tracks upstream changes. Each migration is a no-op once the target key
// already exists, so it disappears cleanly when upstream finishes its own
// migration. It rewrites only the in-image copy; the ./src submodule checkout
// on the host is left untouched.
import { existsSync, readFileSync, writeFileSync } from 'node:fs'

const WS = 'pnpm-workspace.yaml'
const pkg = JSON.parse(readFileSync('package.json', 'utf8'))
let ws = existsSync(WS) ? readFileSync(WS, 'utf8') : ''
let changed = false

// 1) package.json `pnpm.overrides` → pnpm-workspace.yaml `overrides:`
const overrides = (pkg.pnpm && pkg.pnpm.overrides) || {}
if (Object.keys(overrides).length && !/^overrides:/m.test(ws)) {
  const body = Object.entries(overrides).map(([k, v]) => `  "${k}": "${v}"`).join('\n')
  ws = `${ws.replace(/\s*$/, '')}\n\noverrides:\n${body}\n`
  changed = true
  console.log('[ongehoord] migrated pnpm.overrides -> pnpm-workspace.yaml overrides')
}

// 2) pnpm-workspace.yaml `onlyBuiltDependencies:` (list) → `allowBuilds:` (map)
if (!/^allowBuilds:/m.test(ws)) {
  const block = ws.match(/^onlyBuiltDependencies:[ \t]*\n((?:[ \t]+-[ \t].*\n?)+)/m)
  const deps = block
    ? block[1].split('\n')
        .map(l => l.replace(/^[ \t]+-[ \t]*/, '').trim().replace(/^['"]|['"]$/g, ''))
        .filter(Boolean)
    : []
  if (deps.length) {
    const body = deps.map(d => `  "${d}": true`).join('\n')
    ws = `${ws.replace(/\s*$/, '')}\n\nallowBuilds:\n${body}\n`
    changed = true
    console.log(`[ongehoord] migrated onlyBuiltDependencies -> allowBuilds (${deps.length} pkgs)`)
  }
}

if (changed)
  writeFileSync(WS, ws)
else
  console.log('[ongehoord] pnpm 11 config already current — no migration needed')
