// Validates env on boot.

import { loadEnv } from '../utils/env'

export default defineNitroPlugin(() => {
  try {
    const env = loadEnv()
    console.log(
      `[lidarr-bulk] up. lidarr=${env.LIDARR_URL} config=${env.CONFIG_DIR}`,
    )
  }
  catch (err) {
    console.error('[lidarr-bulk] boot failed:', (err as Error).message)
  }
})
