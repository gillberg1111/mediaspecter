# DeepSeek Implementation Task — Plex ID Resolution, Poster Cache Headers & Frontend List Caching

**Role:** You are the implementing engineer. The architecture below is final. Implement it exactly as specified.

## Hard Constraints (read first)
- **Only touch code files.** The files you are permitted to edit are:
  - `mediaspektor.py`
  - `static/app.js`
  - `test_mediaspektor.py`
- **Do NOT edit** `README.md`, `mediaspektor_specification.md`, `deepseek_prompt.md`, this task doc, `config.yaml`, `config.yaml.example`, `.gitignore`, or any other documentation/changelog/non-code file. Do not create new docs.
- Match the existing code style (indentation, naming, comment density) in each file.
- Do not refactor unrelated code. Keep the diff minimal and scoped to the items below.

---

## Goal
Three coordinated fixes:
1. **Backend:** Plex `fetchItem` fails when given string `ratingKey`s that are all digits — they must be cast to `int`. (Already implemented — verify only.)
2. **Backend:** Proxied posters should carry an HTTP `Cache-Control` header so the browser caches them. (Already implemented — verify only.)
3. **Frontend:** Cache the Movies and TV Shows lists in JS memory so swapping tabs does not trigger a redundant loading screen / API fetch. Bust the cache only when the user explicitly refreshes, when an archive/restore action runs, or when settings are saved. Cache-bust individual posters only when an item's status actually changes.

---

## Part 1 — Backend (`mediaspektor.py`) — VERIFY ONLY

These were already implemented in the current working tree. **Do not re-add or duplicate them.** Confirm each is present and correct; if any is missing, add it to match:

1. `PlexConnector._resolve_id(self, item_id)` helper exists and converts an all-digit string to `int`, otherwise returns the value unchanged:
   ```python
   def _resolve_id(self, item_id: str | int) -> int | str:
       if isinstance(item_id, str) and item_id.isdigit():
           return int(item_id)
       return item_id
   ```
2. `_resolve_id(...)` wraps the `item_id` passed to `self._server.fetchItem(...)` in **all** of these `PlexConnector` methods: `download_poster`, `upload_poster`, `get_seasons`, `get_episodes`, `get_item_metadata`.
3. In the `/api/posterproxy` route (`poster_proxy`), the Plex branch casts the id to `int` before `server._server.fetchItem(...)` (all-digit strings only).
4. Both the Plex branch and the Jellyfin/Emby branch of `poster_proxy` return their `StreamingResponse` with `headers={"Cache-Control": "public, max-age=86400"}`.

**No backend code changes are expected.** If all four are already present (they should be), leave `mediaspektor.py` untouched.

---

## Part 2 — Frontend (`static/app.js`) — IMPLEMENT

State variables `currentMovies` and `currentShows` (declared near the top of the `DOMContentLoaded` callback) already exist and serve as the caches.

### 2.1 `loadMovies` — add caching with a `force` parameter
Change the signature to `loadMovies(force = false)`. If `force` is falsy and `currentMovies` is non-empty, render from cache and return immediately (no spinner, no fetch). Otherwise show the spinner and fetch as before.

Replace the start of the function:
```javascript
    function loadMovies() {
        const grid = document.getElementById("movies-grid");
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning movies library...</p></div>';

        fetch("/api/movies")
```
with:
```javascript
    function loadMovies(force = false) {
        const grid = document.getElementById("movies-grid");

        // Serve from in-memory cache instantly unless a forced refresh is requested
        if (!force && currentMovies.length > 0) {
            renderMovies(currentMovies);
            return;
        }

        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning movies library...</p></div>';

        fetch("/api/movies")
```
(The rest of the function — `.then(movies => { currentMovies = movies; renderMovies(movies); })` etc. — stays unchanged.)

### 2.2 `loadShows` — add caching with a `force` parameter
Apply the same pattern, mirroring 2.1 but for `currentShows` / `renderShows` / the `shows-grid` element. Replace the start of the function:
```javascript
    function loadShows() {
        const grid = document.getElementById("shows-grid");
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning TV shows library...</p></div>';

        fetch("/api/shows")
```
with:
```javascript
    function loadShows(force = false) {
        const grid = document.getElementById("shows-grid");

        // Serve from in-memory cache instantly unless a forced refresh is requested
        if (!force && currentShows.length > 0) {
            renderShows(currentShows);
            return;
        }

        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 1rem;">Scanning TV shows library...</p></div>';

        fetch("/api/shows")
```

### 2.3 `handleTabActivation` — no change required
It already calls `loadMovies()` and `loadShows()` with no argument, which defaults `force` to `false` — exactly the cached behavior we want when switching tabs. **Leave the `movies` and `shows` cases as-is.** Do not pass `true` here.

### 2.4 Refresh button (`btnRefresh`) — force a hard refresh
The manual Refresh button must always bypass the cache for the active list. Replace:
```javascript
    btnRefresh.addEventListener("click", () => {
        const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
        handleTabActivation(activeTab);
        showToast("Refreshed data from server", "success");
    });
```
with:
```javascript
    btnRefresh.addEventListener("click", () => {
        const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
        if (activeTab === "movies") {
            loadMovies(true);
        } else if (activeTab === "shows") {
            loadShows(true);
        } else {
            handleTabActivation(activeTab);
        }
        showToast("Refreshed data from server", "success");
    });
```

### 2.5 Post-action reload — force a hard refresh
In the confirmed-action handler (`#btn-confirm-execute` click → `.then(data => {...})`), the `setTimeout` currently calls `handleTabActivation(activeTab)`, which would hit the cache. After a Spektor/Restore the library status has changed, so it must bypass the cache. Replace:
```javascript
                // Reload active grid after a short delay
                setTimeout(() => {
                    const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
                    handleTabActivation(activeTab);
                }, 2000);
```
with:
```javascript
                // Reload active grid after a short delay, bypassing the cache so the new status shows
                setTimeout(() => {
                    const activeTab = document.querySelector(".nav-item.active").getAttribute("data-tab");
                    if (activeTab === "movies") {
                        loadMovies(true);
                    } else if (activeTab === "shows") {
                        loadShows(true);
                    } else {
                        handleTabActivation(activeTab);
                    }
                }, 2000);
```

### 2.6 Clear caches on successful settings save
In the `#btn-save-settings` click handler, inside the `.then(data => { if (data.success) {...} })` success branch, clear both caches so the next visit to Movies/Shows refetches against the new config. Change:
```javascript
            if (data.success) {
                showToast("Configuration saved and reloaded successfully!", "success");
            } else {
```
to:
```javascript
            if (data.success) {
                showToast("Configuration saved and reloaded successfully!", "success");
                // Invalidate cached lists so they refetch against the new config
                currentMovies = [];
                currentShows = [];
            } else {
```

### 2.7 Poster cache-busting by status — `renderMovies`
Posters get a glassmorphic overlay after archiving. Because the proxy now sends `Cache-Control`, the browser would otherwise show the stale (pre-overlay) poster. Append the item status as a query param so the URL changes only when status changes. In `renderMovies`, change:
```javascript
            const posterUrl = `/api/posterproxy?server_type=${movie.server_type}&item_id=${movie.id}`;
```
to:
```javascript
            const posterUrl = `/api/posterproxy?server_type=${movie.server_type}&item_id=${movie.id}&status=${movie.status}`;
```

### 2.8 Poster cache-busting by status — `renderShows`
Show objects have no `status` field (only episodes are archived), so guard with a fallback to keep the URL stable. In `renderShows`, change:
```javascript
            const posterUrl = `/api/posterproxy?server_type=${show.server_type}&item_id=${show.id}`;
```
to:
```javascript
            const posterUrl = `/api/posterproxy?server_type=${show.server_type}&item_id=${show.id}&status=${show.status || 'original'}`;
```

> Note: The backend `poster_proxy` route only reads `server_type` and `item_id`; the extra `status` query param is ignored server-side and exists purely for browser cache-busting. Do not add a `status` parameter to the backend route.

### 2.9 Leave search handlers alone
The `movie-search` / `show-search` input handlers already filter `currentMovies` / `currentShows` and call `renderMovies(filtered)` / `renderShows(filtered)`. These keep working unchanged because the caches are populated on first load.

---

## Part 3 — Tests (`test_mediaspektor.py`) — IMPLEMENT

Add a single new test method `test_poster_proxy` to the existing `TestFastAPI` class. It must validate:
1. Plex string-ID → int conversion (i.e. `fetchItem` is called with the integer `1`, not the string `"1"`, when the request uses `item_id=1`).
2. The response status code is `200`.
3. The `Cache-Control` response header equals `public, max-age=86400`.

### Implementation guidance
`TestFastAPI.setUp` already builds `self.mock_connector` (a `MagicMock` with `server_type = "plex"`) and registers it as the only active server, and `self.client` is a `TestClient(app)`. The `poster_proxy` Plex branch executes:
```python
parsed_id = int(item_id) if isinstance(item_id, str) and item_id.isdigit() else item_id
item = server._server.fetchItem(parsed_id)
if not item.posterUrl: ...
url = item.posterUrl
if url.startswith("/"): url = ...    # build absolute URL
resp = requests.get(url, timeout=30, stream=True)
resp.raise_for_status()
return StreamingResponse(resp.iter_content(...), media_type=..., headers={"Cache-Control": "public, max-age=86400"})
```
So in the test you must:
- Configure the mock connector's Plex client so `item.posterUrl` is a **real absolute string** (e.g. `"http://mock-plex/poster.jpg"`) — a bare `MagicMock` would break `url.startswith(...)`. Set:
  ```python
  fake_item = MagicMock()
  fake_item.posterUrl = "http://mock-plex/poster.jpg"
  self.mock_connector._server.fetchItem.return_value = fake_item
  ```
- Patch `mediaspektor.requests.get` to return a fake response whose `.iter_content` yields some bytes, `.raise_for_status()` is a no-op, and `.headers` is a dict with a `Content-Type` (e.g. `{"Content-Type": "image/jpeg"}`). Use `unittest.mock.patch`.
- Call `self.client.get("/api/posterproxy?server_type=plex&item_id=1")`.
- Assert `resp.status_code == 200`.
- Assert `resp.headers["Cache-Control"] == "public, max-age=86400"`.
- Assert `self.mock_connector._server.fetchItem.assert_called_with(1)` (the integer), proving string→int conversion.

Keep the test self-contained and consistent with the existing patch/mock style already used in this file (`from unittest.mock import MagicMock, patch`, decorator or context-manager patching of `mediaspektor.requests.get`).

---

## Part 4 — Verification (run before declaring done)

1. **Automated tests:**
   ```bash
   python3 -m unittest test_mediaspektor.py
   ```
   All existing tests plus the new `test_poster_proxy` must pass.

2. **Static sanity check** on `static/app.js` — confirm:
   - `loadMovies` and `loadShows` both accept `force = false` and short-circuit to render-from-cache when not forced and the cache is non-empty.
   - `btnRefresh` and the post-action `setTimeout` force-refresh the active list.
   - Settings-save success clears both caches.
   - Both `renderMovies` and `renderShows` poster URLs include the `&status=` param.

3. **Manual (informational — for the human reviewer, not required of you):**
   - Swap between Movies and TV Shows tabs: no loading spinner / no refetch after the first load.
   - Run a Spektor/Restore: grid auto-reloads after ~2s and the poster overlay updates.
   - Click Refresh: spinner appears and fresh data is fetched.

---

## Summary of edits
| File | Change |
|------|--------|
| `mediaspektor.py` | **Verify only** — Plex `_resolve_id` + poster-proxy int cast + `Cache-Control` headers (already present; no edits expected). |
| `static/app.js` | `loadMovies(force)` / `loadShows(force)` caching; force-refresh on Refresh button + post-action; clear caches on settings save; add `&status=` to movie & show poster URLs. |
| `test_mediaspektor.py` | Add `test_poster_proxy` to `TestFastAPI` (string→int id, 200, `Cache-Control`). |
