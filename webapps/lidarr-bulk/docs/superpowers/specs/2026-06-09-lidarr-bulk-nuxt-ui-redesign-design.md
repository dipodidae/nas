# lidarr-bulk — Nuxt UI Redesign

**Date:** 2026-06-09
**Status:** Approved design, pending implementation plan

## Goal

Completely redesign the lidarr-bulk frontend on top of **Nuxt UI v4** (free
edition). Replace every hand-rolled UI pattern with its Nuxt UI equivalent,
delete the custom CSS, and refactor the monolithic pages into focused custom
components that carry their own logic. Behaviour is preserved; UX is upgraded
where Nuxt UI offers a clearly better pattern (toasts, alerts, forms).

### Decisions (locked)

- **Edition:** Free `@nuxt/ui` (MIT, no Pro license).
- **Scope:** Reskin **+ better UX** — preserve all flows and API calls; adopt
  Nuxt UI's better patterns (toasts for transient errors, alerts for persistent
  states, `UForm` validation, color-mode toggle).
- **Theme:** Violet/indigo modern — `primary: violet`, `neutral: slate`,
  dark-mode default with light support.
- **Navigation:** Keep the three-tab structure (Artists / Albums / Discover ✨)
  via `UTabs`; keep the Add / History / Settings top-level nav.

## Current state (what we are replacing)

- `app/assets/css/main.css` — 198 lines of CSS custom properties and bespoke
  classes (`.panel`, `.tab`, `.status`, `.candidate`, `.kbd`, `nav.top`).
- `app/app.vue` — top nav chrome (logo + history/settings links + sign-out).
- `app/pages/index.vue` — 476 lines: 3 tabs, paste-textarea, dry-run + monitor
  + advanced profile pickers, external-prompt builder, live job-progress view
  split into needs-attention / working / settled groups.
- `app/pages/login.vue`, `app/pages/history.vue` (with replay), `app/pages/settings.vue`.
- `app/components/CandidateRow.vue` (disambiguation picker).
- `app/composables/useJob.ts` — SSE streaming composable (logic, **kept as-is**).

## Architecture

### Foundation & theming

1. `pnpm add @nuxt/ui tailwindcss`.
2. `nuxt.config.ts`: add `'@nuxt/ui'` to `modules`; keep `colorMode` (dark
   default); drop the manual `color-scheme` meta (handled by color-mode).
3. Replace `main.css` entirely with:
   ```css
   @import "tailwindcss";
   @import "@nuxt/ui";
   ```
   plus a `@theme` block for any font/token tweaks.
4. `app.config.ts` sets UI defaults:
   `ui: { colors: { primary: 'violet', neutral: 'slate' } }` and a default
   button size.
5. `app.vue` becomes:
   ```vue
   <template>
     <UApp>
       <NuxtLayout><NuxtPage /></NuxtLayout>
     </UApp>
   </template>
   ```

### Layout & nav chrome

- New `app/layouts/default.vue`: `UHeader` + `UNavigationMenu` (Add / History /
  Settings) + `UColorModeButton` (new light/dark toggle) + `UDropdownMenu`
  avatar menu for the signed-in user with "Sign out". The `/api/me` fetch and
  `logout()` logic move here.
- Login page keeps `definePageMeta({ layout: false })`.
- Page bodies wrapped in `UContainer`; panels become `UCard`.

### Component extraction (logic baked in)

`index.vue` is gutted into focused components, each owning its own logic:

| New component | Replaces | Owns |
|---|---|---|
| `BulkAddForm.vue` | paste textarea + controls + advanced + Add-all | parse → `useJob().start`, dry-run/monitor (`USwitch`/`USelect`), advanced profiles in a `UCollapsible`, lazy profile fetch |
| `AiDiscoverPanel.vue` | the entire `tab === 'ai'` block | GPT generate (`/api/ai/suggest`), AI status fetch, the external-prompt `UCollapsible` with `UCheckbox` flavors, copy-to-clipboard |
| `JobMonitor.vue` | needs-attention / working / settled panels + summary | renders a `JobSnapshot`, derives counts, groups items |
| `CandidatePicker.vue` | `CandidateRow.vue` | candidate card with `UAvatar`, pick/skip `UButton`s |
| `JobItemRow.vue` | the repeated `.item` rows | one item line (label + message + status badge) |
| `StatusBadge.vue` | the `badge()` function + `.status` CSS | renders a `UBadge` from `ItemStatus` |
| `HistoryEntryCard.vue` | a history entry block | entry summary + `UCollapsible` detail + replay buttons |

After extraction, `index.vue` is just: `UTabs` (Artists / Albums / Discover ✨)
→ the active panel component → `<JobMonitor>`.

`useJob.ts` is unchanged.

### Pure logic extraction (testable)

`StatusBadge` must not bury its mapping in the template. Extract a pure function
to `app/utils/status.ts` (or `shared/`):

```ts
export function statusBadge(status: ItemStatus): { label: string, color: BadgeColor, icon?: string }
```

`StatusBadge.vue` is a thin wrapper around it. This is the one unit covered by a
new test.

### Form-element & feedback mapping

Every raw element maps to a Nuxt UI component:

| Today | Nuxt UI |
|---|---|
| `<textarea>` | `UTextarea` |
| `<input>` | `UInput` |
| `<select>` | `USelect` |
| checkbox | `UCheckbox` / `USwitch` |
| `<button>` | `UButton` (with `:loading` for "starting…" / "asking GPT…" / "saving…") |
| `<details>`/`<summary>` | `UCollapsible` |
| `.tabs` / `.tab` | `UTabs` |
| `.panel` | `UCard` |
| `.status` badge | `UBadge` |
| `.kbd` | `UKbd` |
| candidate thumbnail | `UAvatar` |

**Feedback upgrade:**
- Inline error `<span>`s (AI generation, login) and the `alert()` in
  history-replay → `useToast()` notifications.
- Persistent states (Settings "Can't reach Lidarr") → `UAlert`.
- Login + Settings forms → `UForm` + `UFormField` with validation.

### History, Settings, Login

- `history.vue`: entries → `HistoryEntryCard` (`UCard` + `UCollapsible` detail);
  replay buttons → `UButton :loading`; status counts → `UBadge`s; refresh →
  `UButton`.
- `settings.vue`: `UForm` / `UFormField` + `USelect`s; save `UButton :loading`;
  toast on success; `UAlert` when Lidarr is unreachable.
- `login.vue`: centered `UCard` + `UForm` + `UInput`/`UButton`.

## Error handling

- Transient failures (network, generation, replay, save) → `useToast()` with an
  `error` color and the server's `statusMessage` when present (preserve the
  existing error-unwrapping logic).
- Hard, page-blocking states (Lidarr unreachable on Settings) → `UAlert` so the
  message persists.

## Testing & verification

- Existing server-side vitest suites (`lidarr.test.ts`, `openai.test.ts`,
  `external-prompt.test.ts`) stay green and untouched.
- New: one unit test for `statusBadge()` pure mapping (covers every
  `ItemStatus`).
- No `@nuxt/test-utils` component-test harness — out of scope by decision.
- Gates before done: `pnpm typecheck`, `pnpm build`, `pnpm test` all pass; manual
  smoke of Add (artist/album/AI), candidate-pick, history replay, settings save,
  login.

## Out of scope (YAGNI)

- Nuxt UI Pro components / dashboard templates.
- Reorganizing navigation beyond the existing tab/nav structure.
- Full component-test coverage.
- Server / API changes — this is frontend-only.
