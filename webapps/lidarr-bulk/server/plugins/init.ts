// Validates env on boot. The session signing key (NUXT_SESSION_PASSWORD) is
// bootstrapped by docker-entrypoint.sh before node starts, because
// nuxt-auth-utils reads it at module-init time.

import { loadEnv } from '../utils/env'

export default defineNitroPlugin(() => {
  try {
    const env = loadEnv()
    const authOn = Boolean(env.APP_USERNAME && env.APP_PASSWORD)
    console.log(
      `[lidarr-bulk] up. lidarr=${env.LIDARR_URL} auth=${authOn ? 'on' : 'off'} config=${env.CONFIG_DIR}`,
    )
  }
  catch (err) {
    console.error('[lidarr-bulk] boot failed:', (err as Error).message)
  }
})
