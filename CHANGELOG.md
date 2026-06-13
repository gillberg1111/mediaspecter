# Changelog

All notable changes to **MediaSpektor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a simple `v0.x` release line.

## [v0.6] - 2026-06-12

### Fixed
- **Plex data fetching with string IDs** — `PlexConnector` now casts all-digit string `ratingKey`s to `int` via a `_resolve_id` helper before calling `fetchItem`, across `download_poster`, `upload_poster`, `get_seasons`, `get_episodes`, and `get_item_metadata`. The `/api/posterproxy` route applies the same cast. This resolves Plex retrieval errors caused by string-based IDs.

### Added
- **Poster HTTP caching** — `/api/posterproxy` now returns a `Cache-Control: public, max-age=86400` header for Plex, Jellyfin, and Emby posters so browsers cache proxied artwork.
- **Frontend in-memory list caching** — Movies and TV Shows lists are cached in JS (`currentMovies` / `currentShows`). Swapping tabs renders instantly from cache instead of re-fetching and showing a loading spinner. `loadMovies(force)` / `loadShows(force)` accept a force flag to bypass the cache.
- **Status-based poster cache-busting** — Movie and show poster URLs include a `&status=` query param so the browser only re-fetches a poster when an item's archived/restored status changes (e.g. after a glassmorphic overlay is applied).
- **Test coverage** — Added `test_poster_proxy` validating Plex string→int ID conversion, the `200` response, and the `Cache-Control` header.

### Changed
- Cache is force-refreshed (bypassed) on the manual **Refresh** button, after a **Spektor/Restore** action completes, and both lists are cleared when **settings are saved successfully**, so updated library status is immediately visible.

## [v0.5]

### Added
- **Login security** with username/password authentication and session-cookie gating of API endpoints (toggled via `security.enabled`).
- **Username/password-based Jellyfin authentication** (in addition to API key flows).
- **Custom dummy videos** and a **premium logo** asset set.
- **`safety.allow_automated_archival` toggle** — automated `--archive` runs are forced into dry-run unless this is explicitly enabled, with a warning and a confirmation popup in the settings UI.
- **Structured settings UI** — per-section page version footers and a floating save button.

### Fixed
- Aligned `logo.svg` geometry with the circular-feet retro ghost shape used in `logo.png`.
- Fixed search icon overlap in the media grids.

## [v0.4]

### Fixed
- Restored a missing closing brace for the confirmation modal click listener in `app.js`.

## [v0.3]

### Changed
- Replaced the raw JSON config editor with a structured HTML settings form.

## [v0.2]

### Changed
- Linked `app.js` to the HTML and elevated the design system with a premium glassmorphic theme.

## [v0.1]

### Added
- Initial release: MediaSpektor self-hosted watch-state storage archiver dashboard, with Plex/Jellyfin/Emby connectors, SQLite state tracking, Pillow poster overlays, Radarr/Sonarr integration, dummy-video generation, and a FastAPI web dashboard.
