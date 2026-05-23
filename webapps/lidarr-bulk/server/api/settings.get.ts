import { defineEventHandler } from 'h3'
import { loadSettings } from '../utils/settings'

export default defineEventHandler(() => loadSettings())
