# DeepSeek Implementation Task — Multi-Server Poster & State Propagation

**Role:** You are the implementing engineer. The architecture below is final. Implement it exactly.

## Hard Constraints (read first)
- **Only touch code files.** Permitted files:
  - `mediaspektor.py`
  - `test_mediaspektor.py`
  - `static/index.html` and `static/app.js` — **only** for the single new TMDB API-key field described in Part 0.4. No other frontend changes.
- **Do NOT edit** any docs/changelog/spec/config: `README.md`, `CHANGELOG.md`, `*_spec.md`, this file, `WORKFLOW.md`, `config.yaml*`, `.gitignore`, etc. Version bumps and changelog entries are the architect's job — do not touch them.
- Aside from the TMDB key field, no other frontend changes are needed (per-server DB rows make the status badge propagate automatically — see §6). If you think another frontend change is required, STOP and flag it.
- Match existing code style. Keep the diff scoped to what's described.

---

## Problem
MediaSpektor users often run **Plex + Jellyfin + Emby against the same physical media library** (one set of files on disk). Today, archiving an item via one server:
- replaces the file on disk once (correct — the file is shared), but
- only badges the poster and records archived state on **that one server**.

The other servers keep their original posters and show the item as `original`.

## Goal
When an item is archived (or restored), **propagate the poster overlay and the archived/restored state to every enabled server** that has the same physical item. Replace the file on disk exactly once.

## Locked Design Decisions
1. **Cross-server matching: path-first, external-ID fallback.**
   - First try exact `file_path` match (works when all servers mount the library at the same path).
   - If no path match, fall back to external IDs in priority **TMDB → IMDB → TVDB**.
2. **DB: one row per server.** Insert one `archived_items` row per server where the item was matched and badged. The existing `/api/movies` per-server `db.get_item(server_type, id)` lookup then flips the badge on every server automatically.
3. **Episode scope:** Episodes match on **file_path only**. If an episode's path doesn't match on a given server, **skip that server with a warning** (do not attempt ID-based episode matching in this task).
4. **Best-effort propagation:** The file is always dummied once. Poster propagation is per-server best-effort: a server with no match (or an error) is logged and skipped — never hard-fail the whole operation, never badge the wrong item.

## Why a TMDB key is needed (ID-system mismatch)
Plex and Jellyfin/Emby don't always store the **same** external-ID system for a given title — e.g. Plex may only expose an IMDB id while Jellyfin exposes only TMDB. So a naive "do any of tmdb/imdb/tvdb match?" comparison can miss a real match. The fix is to **normalize IDs through TMDB**: TMDB can resolve any one of {imdb, tvdb} → tmdb (via `/find`) and tmdb → {imdb, tvdb} (via `/movie/{id}/external_ids`). With a TMDB key configured, the orchestrator expands the source item's IDs to all three systems before matching, so the expanded set overlaps whatever system each target server happens to store.

---

## Part 0 — TMDB ID Bridge (optional, key-gated)

### 0.1 Config
Add an optional TMDB key under `integrations` (read-only in backend; the architect maintains `config.yaml.example` separately):
```yaml
integrations:
  tmdb:
    api_key: ""   # optional; enables cross-server ID normalization. Supports v3 key or v4 bearer token.
```
Backend reads `self.config.get("integrations", {}).get("tmdb", {}).get("api_key", "")`. Also accept the `TMDB_API_KEY` env var as a fallback.

### 0.2 `TmdbClient` class
Add a small client to `mediaspektor.py`, modeled on `/home/jakwgrav/Projects/Linearr/tmdb_client.py` (do not import from Linearr — reimplement here, self-contained):
```python
class TmdbClient:
    BASE = "https://api.themoviedb.org/3"
    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()
        self._cache: dict[tuple, dict] = {}   # in-memory bridge cache

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, **params) -> dict:
        # v4 bearer tokens start with "eyJ"; otherwise v3 api_key query param
        if self.api_key.startswith("eyJ"):
            headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
            resp = requests.get(f"{self.BASE}{path}", params=params, headers=headers, timeout=10)
        else:
            resp = requests.get(f"{self.BASE}{path}", params={"api_key": self.api_key, **params}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def find_tmdb_id(self, external_source: str, external_id: str, media_type: str) -> str | None:
        """external_source in {'imdb_id','tvdb_id'}; returns the TMDB id as str or None."""
        data = self._get(f"/find/{external_id}", external_source=external_source)
        key = "movie_results" if media_type == "movie" else "tv_results"
        results = data.get(key) or []
        return str(results[0]["id"]) if results else None

    def external_ids(self, media_type: str, tmdb_id: str) -> dict[str, str | None]:
        """tmdb id -> {'imdb','tvdb'} (None where TMDB has none)."""
        path = f"/movie/{tmdb_id}/external_ids" if media_type == "movie" else f"/tv/{tmdb_id}/external_ids"
        data = self._get(path)
        tvdb = data.get("tvdb_id")
        return {"imdb": data.get("imdb_id") or None, "tvdb": str(tvdb) if tvdb else None}
```
All network calls must be wrapped so any failure logs a warning and degrades gracefully (treat as "no bridge available"), never raising into the archive flow.

### 0.3 Orchestrator wiring
In `MediaSpektor.__init__`, instantiate `self.tmdb = TmdbClient(<key from config or env>)`.

Add `MediaSpektor._expand_external_ids(self, media_type: str, ids: dict) -> dict`:
- Only meaningful for **movies** (episodes are path-only). For episodes, return `ids` unchanged.
- If `self.tmdb` is not enabled, return `ids` unchanged.
- Determine a canonical TMDB id: use `ids["tmdb"]` if present; else `find_tmdb_id("imdb_id", ids["imdb"], "movie")`; else `find_tmdb_id("tvdb_id", ids["tvdb"], "movie")`.
- If a tmdb id is found, call `external_ids("movie", tmdb_id)` and **merge** the discovered imdb/tvdb into a copy of `ids` (don't overwrite values already present). Set `tmdb` too.
- Cache by `(media_type, frozenset(non-null id items))` to avoid repeat calls within a run. Return the merged dict.

### 0.4 Settings UI (the only permitted frontend change)
Add a single input for the TMDB API key in the **Integrations** section of `static/index.html`, consistent with the existing Radarr/Sonarr key fields (e.g. `id="int-tmdb-key"`, `type="password"`). In `static/app.js`:
- In `loadSettings()`, populate it: `document.getElementById("int-tmdb-key").value = (config.integrations?.tmdb?.api_key) || "";`
- In the save handler (`#btn-save-settings`), include it in the built config under `integrations.tmdb = { api_key: document.getElementById("int-tmdb-key").value.trim() }`.
Do not change anything else in the frontend.

---

## Part A — External ID extraction (all three connectors)

Add module-level helpers near the top of `mediaspektor.py` (after the existing helpers), mirroring the approach proven in `/home/jakwgrav/Projects/Linearr/plex_client.py` and `jellyfin_client.py`:

```python
def _plex_external_ids(guids) -> dict[str, str | None]:
    """Extract {tmdb, imdb, tvdb} from a list of plexapi Guid objects."""
    ids: dict[str, str | None] = {"tmdb": None, "imdb": None, "tvdb": None}
    for g in (guids or []):
        gid = getattr(g, "id", "") or ""
        if gid.startswith("tmdb://"):
            ids["tmdb"] = gid[len("tmdb://"):].split("?")[0]
        elif gid.startswith("imdb://"):
            ids["imdb"] = gid[len("imdb://"):].split("?")[0]
        elif gid.startswith("tvdb://"):
            ids["tvdb"] = gid[len("tvdb://"):].split("?")[0]
    return ids


def _provider_external_ids(provider_ids: dict) -> dict[str, str | None]:
    """Extract {tmdb, imdb, tvdb} from a Jellyfin/Emby ProviderIds dict (case-insensitive keys)."""
    p = {k.lower(): v for k, v in (provider_ids or {}).items()}
    return {
        "tmdb": str(p["tmdb"]) if p.get("tmdb") else None,
        "imdb": str(p["imdb"]) if p.get("imdb") else None,
        "tvdb": str(p["tvdb"]) if p.get("tvdb") else None,
    }
```

Then add an `"external_ids"` field to the dict returned by **`get_item_metadata`** in all three connectors:
- **PlexConnector.get_item_metadata:** the fetched `item` is a plexapi object; use `_plex_external_ids(getattr(item, "guids", None))`. (Use `fetchItem(self._resolve_id(item_id))`, which already returns guids for movies/episodes.)
- **JellyfinConnector.get_item_metadata:** the item JSON has `item.get("ProviderIds")`; use `_provider_external_ids(item.get("ProviderIds", {}))`.
- **EmbyConnector.get_item_metadata:** same as Jellyfin — request `ProviderIds` in `Fields` if not already, and use `_provider_external_ids(...)`.

Result shape (add the one key, keep all existing keys):
```python
return {
    "id": ...,
    "title": ...,
    "type": ...,            # "movie" | "episode"
    "file_path": ...,
    "original_size": ...,
    ...,
    "external_ids": {"tmdb": ..., "imdb": ..., "tvdb": ...},
}
```

---

## Part B — `find_item` matcher (new method on every connector)

Add an abstract method to `BaseMediaServer`:
```python
@abstractmethod
def find_item(self, file_path: str, external_ids: dict, media_type: str) -> dict | None:
    """Locate this server's local item matching the given physical media.
    Returns this server's metadata dict (same shape as get_item_metadata, including
    local 'id'), or None if no confident match. Path match first, then TMDB/IMDB/TVDB."""
```

> **`external_ids` is the orchestrator-expanded set** (see Part 0.3) — it may contain all of tmdb/imdb/tvdb even if the source server only stored one. Connectors therefore only need **direct overlap** (no TMDB calls inside connectors): a match is any system where both sides have a non-null, equal value.

Implement on each connector:

### PlexConnector.find_item
- Iterate the configured libraries' sections (`self.config.get("libraries", [])`). Use `section.search(libtype="movie")` for movies and `section.search(libtype="episode")` for episodes (call with `includeGuids=1` so guids are populated for the ID fallback).
- **Path match (movies & episodes):** compare `item.media[0].parts[0].file == file_path`. On hit, build and return the metadata dict (reuse the same builder as `get_item_metadata`).
- **ID fallback (movies only):** if no path hit and `media_type == "movie"`, compare `_plex_external_ids(item.guids)` against `external_ids` (TMDB → IMDB → TVDB, first non-None on both sides that matches wins). For episodes, return `None` if path didn't match.

### JellyfinConnector.find_item / EmbyConnector.find_item
- **Auth / user API key requirement (important):** these queries MUST be **user-scoped and authenticated**, because the anonymous `/Items` endpoint can return empty `Path`/`MediaSources` and won't reflect the user's library. Query `GET /Users/{user_id}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path,ProviderIds,MediaSources` with the user token in the header.
  - **Jellyfin:** call `self._ensure_auth()` first; use `self.user_id` and `self.headers` (token) populated by `authenticate()` (from `username`/`password`). If `self.user_id` is still falsy after auth, log an error and return `None` (skip this server).
  - **Emby:** requires `config["user_id"]` and `config["api_key"]` (the user-scoped API key). If either is missing/empty, log a clear error (`"Emby: user_id/api_key required for cross-server matching"`) and return `None` — do not raise.
- **Path match:** compare each item's `Path` (or `MediaSources[0].Path`) to `file_path`.
- **ID fallback (movies only):** `_provider_external_ids(item["ProviderIds"])` vs `external_ids`, same priority. Episodes: path-only.
- Return the metadata dict (reuse `get_item_metadata`-style builder, or call `get_item_metadata(item["Id"])`).

> Keep matching client-side and tolerant: skip items missing a path; never raise out of `find_item` — on any exception log a warning and return `None`.

---

## Part C — Database helper for sibling rows

`archived_items` already has PK `(server_type, server_item_id)`. To restore all servers for one physical item, add a lookup by the shared host path.

In `Database`, add:
```python
def get_items_by_path(self, original_path: str, status: str | None = "archived") -> list[dict]:
    """Return all rows whose original_path matches, optionally filtered by status."""
```
(Use a parameterized SELECT; return list of dict rows like `get_item`.)

No schema change is required — `original_path` already exists and will store the host file path for every per-server row.

---

## Part D — Refactor `MediaSpektor.archive_item` to propagate

Rewrite `archive_item(self, server_type, item_id)` so it:

1. Resolves the **source** connector and `item = source.get_item_metadata(item_id)` (now includes `external_ids`). Derive `file_path`, `original_size`, `media_type`, `ext`, `gb_saved`, `title` as today.
2. Guard: if `self.db.item_exists(server_type, item_id)` → return `{"success": False, "error": "...already archived"}` (unchanged).
3. **Replace the file on disk exactly once** using the source `file_path` (the existing backup-or-delete + write-dummy logic). Do this **before** the per-server poster loop so a poster failure never leaves the file untouched. Keep the dummy-template / backup-original-media behavior identical to today.
4. **Expand the source IDs once** (before the loop): `expanded_ids = self._expand_external_ids(media_type, item["external_ids"])` (Part 0.3). This is a no-op without a TMDB key.
5. **Per-server propagation loop** over `self.servers`:
   - For the source server, the target is `item` itself (id known).
   - For every other server, `target = server.find_item(file_path, expanded_ids, media_type)`. If `None`: `logger.warning("No %s match for '%s' — skipping poster", server.server_type, title)` and continue.
   - For each matched server: download its current poster → back it up (`{server_type}_{local_id}_poster_original.jpg`) → apply overlay → upload → `server.trigger_library_scan()` → `self.db.insert(server_type=server.server_type, server_item_id=target["id"], title=title, media_type=media_type, original_path=file_path, original_size_bytes=original_size, dummy_size_bytes=len(dummy_bytes), backup_poster_path=..., backup_media_path=<only on source if media was moved, else None>, status="archived")`.
   - Per-server poster failures are caught, logged, and do not abort other servers. (Only the source row should carry `backup_media_path` since the file was moved/deleted once.)
6. Unmonitor in Radarr/Sonarr **once** (not per server), as today.
7. Return `{"success": True}` if the file was replaced (even if some servers had no poster match); include a `"warnings"` list naming servers that were skipped. If the file replacement itself fails, roll back as today and return `{"success": False, "error": ...}`.

> Keep the existing single-server rollback semantics for the **file** operation. The new per-server poster steps are independent and best-effort.

## Part E — Refactor `MediaSpektor.restore` to fan out

Rewrite `restore(self, server_type, item_id)` so it:
1. Looks up the clicked row (`db.get_item`). If missing → error as today.
2. Finds **all sibling rows** via `db.get_items_by_path(record["original_path"], status="archived")` (this includes the clicked one).
3. **Restores the file once:** if a `backup_media_path` exists on any sibling row and the file is present, move it back to `original_path`; otherwise log the existing "please restore manually" warning once.
4. For each sibling row, find its connector by `server_type`, upload the backed-up original poster (`backup_poster_path`) if present, trigger that server's scan, and `db.update_status(row.server_type, row.server_item_id, "restored")`.
5. Return `True` if at least the DB state was updated.

---

## Part F — Tests (`test_mediaspektor.py`)

Add focused tests. Use the existing `MagicMock` connector patterns.

1. **`test_find_item_path_then_id`** (per connector or at least Plex + Jellyfin): given a mock library, assert `find_item` returns the right local id when (a) the path matches, and (b) only the TMDB id matches (movie). Assert episodes with a non-matching path return `None`.
2. **`test_archive_item_propagates_to_all_servers`**: build a `MediaSpektor` with **two** mock connectors (e.g. plex + jellyfin) sharing one `file_path`. Patch filesystem/dummy writes (mock `open`, `os.unlink`, `shutil`, `base64`) and posters. Assert: the file write happens once, `find_item` is called on the non-source server, `upload_poster` is called on **both** servers, and **two** DB rows are inserted (one per server) both with `status="archived"`.
3. **`test_archive_item_skips_unmatched_server`**: second connector's `find_item` returns `None`; assert the source is still archived (1 row), the unmatched server is skipped (no upload, no row), and `success` is still `True` with a warning recorded.
4. **`test_restore_fans_out`**: seed two archived rows sharing `original_path`; call `restore` on one; assert both rows end up `status="restored"` and `upload_poster` is attempted for each connector present.
5. **`test_expand_external_ids_bridges`**: with a `TmdbClient` patched (mock `requests.get` / its `_get`) so `find_tmdb_id` and `external_ids` return known values, assert `_expand_external_ids("movie", {"imdb": "tt123", "tmdb": None, "tvdb": None})` returns a dict with tmdb and tvdb filled in. Also assert that when `self.tmdb.enabled` is False, the input is returned unchanged (no network call). Episodes return input unchanged.

All existing tests plus these must pass.

---

## Part G — Verification
```bash
python3 -m unittest test_mediaspektor.py
```
All green. Then STOP — the architect will review, bump the version (this is **v0.7**), and update the changelog/footers. Do not modify any version string or doc.

---

## Summary of edits
| File | Change |
|------|--------|
| `mediaspektor.py` | Add `TmdbClient` (key-gated ID bridge) + `MediaSpektor._expand_external_ids`; add `_plex_external_ids` / `_provider_external_ids` helpers; add `external_ids` to all three `get_item_metadata`; add abstract + concrete `find_item` (path-first, direct-overlap ID-fallback for movies on the expanded id set); add `Database.get_items_by_path`; refactor `archive_item` to expand ids + replace file once + propagate posters/rows to all matched servers; refactor `restore` to fan out across sibling rows. |
| `test_mediaspektor.py` | Add matcher, propagation, skip-unmatched, restore-fanout, and TMDB-bridge tests. |
| `static/index.html`, `static/app.js` | **Only** the new TMDB API-key field in the Integrations settings section (load + save). No other frontend changes. |
