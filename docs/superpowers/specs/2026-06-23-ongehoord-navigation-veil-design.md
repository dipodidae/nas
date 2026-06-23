# Ongehoord navigation veil + tuned loading bar — design

**Date:** 2026-06-23
**Scope:** `webapps/ongehoord/src` (Nuxt app, submodule `dipodidae/ongehoord-ui-content`, branch `acceptance`)
**Status:** approved, implementing

## Problem

Navigating client-side into a heavy investigation page (`onderzoek/[slug]`) feels frozen for
seconds with no clear feedback. Two root causes:

1. **Perceived freeze.** The investigation page does a top-level `await promise`, making it a
   Suspense boundary. With `app.pageTransition` mode `out-in`, Nuxt keeps the **old page fully
   visible** until the new page's data resolves. On a slow payload fetch the user stares at the
   previous page, with only the 3px `NuxtLoadingIndicator` at the very top as a signal — easy to
   miss. (The page's own `ApplicationLoading v-if="pending"` is effectively dead on client nav,
   because `await promise` means `pending` is already `false` before it could render.)
2. **Indicator gets hidden.** `experimental.viewTransition: true` runs the native View Transitions
   API *alongside* the CSS `page`/`layout` transitions. Native view transitions paint an old-page
   snapshot in the browser's **top layer**, above `position: fixed`, so the loading bar is painted
   over during the swap. View Transitions are Chromium-only, which explains the dev-vs-build
   difference the owner sensed.

This change makes the wait **legible and on-brand**. It is feedback-only — it does not make the
heavy page load faster (that would be a separate skeleton/lazy-data refactor).

## Design

### 1. Resolve the transition conflict
Remove `experimental.viewTransition` from `nuxt.config.ts`. Keep the existing CSS `page`/`layout`
transitions (tuned, cross-browser, already `prefers-reduced-motion`-aware). One transition
mechanism, so nothing paints over fixed/teleported overlays.

### 2. `NavigationVeil` component — `app/components/application/NavigationVeil.vue`
- **Teleported to `<body>`** so it escapes every transform/overflow/portal context.
  `position: fixed; inset: 0;`, high z-index, `bg-blue-900` ground.
- **Centered white logo mark** (`~/assets/icons/logo/white.svg`) with a slow opacity "breathe"
  (≈0.6↔1.0), plus a thin **blue-400 sweep line** beneath it as indeterminate progress.
- **Localized caption** via `$t('loading.navigating')`.
- **Delay gate (~200 ms):** driven by Nuxt's `useLoadingIndicator()` (`isLoading`). On load-start,
  arm a 200 ms timer; show the veil only if the nav is still loading when it fires — fast navs
  never flash. On load-end, clear the timer and fade out (~250 ms).
- **A11y:** `role="status"`, `aria-live="polite"`, `aria-busy`; caption text is the announced
  content.
- **Reduced motion:** under `prefers-reduced-motion`, no breathe/sweep and no fade — static logo +
  caption, shown/hidden instantly.
- `data-test="navigation-veil"` (repo convention; targeted by tests).

### 3. Tuned top bar
Keep `NuxtLoadingIndicator` as the instant (<200 ms) signal before the veil threshold. Bump
`height` 3 → 4; keep `color="var(--color-blue-400)"` and `throttle: 100`. Bar covers quick navs;
veil covers long investigation loads.

### 4. Wiring
`NavigationVeil` mounts in `app.vue` as a top-level sibling alongside `NuxtLoadingIndicator`
(outside `UApp`). It teleports to body regardless of mount point.

### 5. i18n
Add `loading.navigating` to `i18n/locales/nl.json` ("Even geduld…") and `en.json`
("One moment…").

### 6. Tests — `app/components/application/NavigationVeil.nuxt.test.ts`
- Hidden by default (not loading).
- Shown after the ~200 ms threshold while `isLoading` is true.
- Hidden again on load finish.
- Caption renders the localized string.
- Target elements via `data-test`, mock `useLoadingIndicator` via `mockNuxtImport`.

## Out of scope
- Making the investigation payload itself faster (lazy data + route-aware skeleton). Tracked as a
  possible follow-up spec.

## Deploy
Code lives in the `src` submodule. `redeploy.sh` refuses to deploy with uncommitted submodule
changes and pulls `--ff-only`, so changes are committed **locally** on `acceptance` (no push to
the external GitHub remote), then `pnpm ongehoord:deploy` builds the image from the working tree
and recreates the container at `ongehoord.4eva.me`.
