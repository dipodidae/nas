import { defineEventHandler } from 'h3'
import { getProfiles } from '../../utils/lidarr'

export default defineEventHandler(() => getProfiles())
