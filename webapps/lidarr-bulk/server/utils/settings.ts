import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import type { AppSettings } from '~~/shared/types'
import { loadEnv } from './env'

const DEFAULTS: AppSettings = {
  rootFolderPath: '/music',
  qualityProfileId: 1,
  metadataProfileId: 1,
  monitorMode: 'all',
}

function settingsPath(): string {
  return join(loadEnv().CONFIG_DIR, 'settings.json')
}

export async function loadSettings(): Promise<AppSettings> {
  try {
    const buf = await readFile(settingsPath(), 'utf8')
    return { ...DEFAULTS, ...JSON.parse(buf) }
  }
  catch (err: unknown) {
    if (isNotFound(err))
      return { ...DEFAULTS }
    throw err
  }
}

export async function saveSettings(next: AppSettings): Promise<void> {
  const path = settingsPath()
  await mkdir(dirname(path), { recursive: true })
  await writeFile(path, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
}

function isNotFound(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: string }).code === 'ENOENT'
}
