// Shared between client (app/) and server/ — Nuxt 4 auto-imports from shared/.

export type Kind = 'artist' | 'album'

export interface ParsedItem {
  raw: string
  kind: Kind
  // Album rows expose split fields if we recognized a shape.
  artist?: string
  title?: string
  // Anything we couldn't confidently split lands here for the user to review.
  needsReview?: boolean
}

export interface LidarrImage {
  coverType: string
  url?: string
  remoteUrl?: string
}

export interface LidarrArtistCandidate {
  foreignArtistId: string
  artistName: string
  disambiguation?: string
  overview?: string
  artistType?: string
  status?: string
  images?: LidarrImage[]
}

export interface LidarrAlbumCandidate {
  foreignAlbumId: string
  title: string
  albumType?: string
  releaseDate?: string
  artist?: { artistName?: string, foreignArtistId?: string } | string
  images?: LidarrImage[]
}

export type Candidate =
  | { kind: 'artist', value: LidarrArtistCandidate }
  | { kind: 'album', value: LidarrAlbumCandidate }

export type ItemStatus =
  | 'parsed'
  | 'searching'
  | 'needs-choice'
  | 'matched'
  | 'adding'
  | 'searching-on-lidarr'
  | 'done'
  | 'already-added'
  | 'would-add' // dry-run: search succeeded, would have added but didn't
  | 'not-found'
  | 'error'
  | 'skipped'

export interface JobItem {
  id: string
  parsed: ParsedItem
  status: ItemStatus
  message?: string
  candidates?: Candidate[]
  chosen?: Candidate
}

export interface JobSnapshot {
  id: string
  createdAt: number
  kind: Kind
  monitorMode: 'all' | 'future'
  dryRun: boolean
  metadataProfileId?: number // override of settings default
  qualityProfileId?: number
  items: JobItem[]
  done: boolean
}

export interface HistoryEntry {
  id: string
  createdAt: number
  finishedAt: number
  kind: Kind
  dryRun: boolean
  counts: Partial<Record<ItemStatus, number>>
  items: Pick<JobItem, 'id' | 'parsed' | 'status' | 'message' | 'chosen'>[]
}

export interface AppSettings {
  rootFolderPath: string
  qualityProfileId: number
  metadataProfileId: number
  monitorMode: 'all' | 'future'
}

export interface LidarrProfilesResponse {
  rootFolders: { id: number, path: string, accessible: boolean, freeSpace?: number }[]
  qualityProfiles: { id: number, name: string }[]
  metadataProfiles: { id: number, name: string }[]
}
