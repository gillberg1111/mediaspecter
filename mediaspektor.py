#!/usr/bin/env python3
"""MediaSpektor - Reclaim disk space by replacing watched media with tiny dummy video files.

Supports Plex, Jellyfin, and Emby media servers. Applies poster overlays to mark
archived content and integrates with Radarr/Sonarr to prevent re-downloads.
"""

import argparse
import base64
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("mediaspektor")


def _parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        clean_str = date_str.split(".")[0].rstrip("Z")
        return datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
    except Exception as exc:
        logger.debug("Failed to parse ISO date string '%s': %s", date_str, exc)
        return None


# ---------------------------------------------------------------------------
# Optional plexapi import
# ---------------------------------------------------------------------------
try:
    from plexapi.server import PlexServer

    HAS_PLEXAPI = True
except ImportError:
    HAS_PLEXAPI = False


# ---------------------------------------------------------------------------
# Minimal dummy video template builders
# ---------------------------------------------------------------------------
def _make_isom_box(box_type: bytes, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return struct.pack(">I", size) + box_type + payload


def _make_isom_fullbox(
    box_type: bytes, version: int, flags: int, payload: bytes = b""
) -> bytes:
    size = 12 + len(payload)
    return (
        struct.pack(">I", size)
        + box_type
        + bytes([version])
        + struct.pack(">I", flags)[1:]
        + payload
    )


def _build_minimal_mp4() -> bytes:
    """Build a minimal valid MP4 container (no actual decode-able frames)."""
    parts: list[bytes] = []

    # -- ftyp ---------------------------------------------------------------
    parts.append(_make_isom_box(b"ftyp", b"mp42\x00\x00\x00\x01mp42isom"))

    # -- moov / mvhd --------------------------------------------------------
    mvhd = _make_isom_fullbox(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, 3000)  # creation  # modification
        + struct.pack(">Ih", 0x00010000, 0x0100)  # timescale  # duration 3s
        + b"\x00" * 10  # rate  # volume
        + struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)  # reserved
        + b"\x00" * 24  # matrix (identity)
        + struct.pack(">I", 1)  # pre-defined
    )  # next track id

    # -- trak ---------------------------------------------------------------
    trak_payload: list[bytes] = []

    # tkhd
    tkhd = _make_isom_fullbox(
        b"tkhd",
        0,
        0x0F,
        struct.pack(">IIII", 1, 0, 0, 0)
        + struct.pack(">IIh", 0, 0, 0)
        + struct.pack(">h", 0)
        + struct.pack(">hh", 0, 0)
        + struct.pack(
            ">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )
        + struct.pack(">II", 320 * 0x10000, 240 * 0x10000),
    )
    trak_payload.append(tkhd)

    # mdia
    mdia_payload: list[bytes] = []

    # mdhd
    mdhd = _make_isom_fullbox(
        b"mdhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, 3000)
        + struct.pack(">h", 0x55C4)  # language "und"
        + struct.pack(">h", 0),
    )
    mdia_payload.append(mdhd)

    # hdlr
    hdlr_payload = (
        b"\x00\x00\x00\x00"
        + b"vide"
        + b"\x00" * 12
        + b"VideoHandler\x00"
    )
    hdlr = _make_isom_fullbox(b"hdlr", 0, 0, hdlr_payload)
    mdia_payload.append(hdlr)

    # minf
    minf_payload: list[bytes] = []

    # vmhd
    vmhd = _make_isom_fullbox(
        b"vmhd", 0, 1, struct.pack(">HH", 0, 0) + struct.pack(">HHH", 0, 0, 0)
    )
    minf_payload.append(vmhd)

    # dinf
    dref_entry = struct.pack(">I", 12) + b"url " + b"\x00\x00\x00\x01"
    dref_box = _make_isom_fullbox(b"dref", 0, 0, struct.pack(">I", 1) + dref_entry)
    dinf = _make_isom_box(b"dinf", dref_box)
    minf_payload.append(dinf)

    # stbl
    stbl_payload: list[bytes] = []

    # stsd — mp4v sample entry (86 bytes entry)
    mp4v_entry = (
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # reserved
        + struct.pack(">HH", 320, 240)  # width, height
        + b"\x00\x48\x00\x00\x00\x48\x00\x00"  # h/v resolution
        + b"\x00\x00\x00\x00\x00\x01"  # frame count + compressor name len
        + b"\x00" * 31  # compressor name
        + b"\x00\x18\x00\xff\xff"  # depth + predef
    )
    stsd = _make_isom_fullbox(
        b"stsd", 0, 0, struct.pack(">I", 1) + b"mp4v" + mp4v_entry
    )
    stbl_payload.append(stsd)

    # stts — 1 sample, duration 3000
    stts = _make_isom_fullbox(
        b"stts", 0, 0, struct.pack(">I", 1) + struct.pack(">II", 1, 3000)
    )
    stbl_payload.append(stts)

    # stsc — 1 chunk, 1 sample/chunk, desc index 1
    stsc = _make_isom_fullbox(
        b"stsc", 0, 0, struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1)
    )
    stbl_payload.append(stsc)

    # stsz — sample size 10
    stsz = _make_isom_fullbox(
        b"stsz", 0, 0, struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", 10)
    )
    stbl_payload.append(stsz)

    # stco — chunk offset = byte position of mdat
    # (we'll fix up later via a "co64" trick — for simplicity, point into mdat)
    stco = _make_isom_fullbox(
        b"stco", 0, 0, struct.pack(">I", 1) + struct.pack(">I", 0)  # placeholder
    )
    stbl_payload.append(stco)

    stbl = _make_isom_box(b"stbl", b"".join(stbl_payload))
    minf_payload.append(stbl)

    minf = _make_isom_box(b"minf", b"".join(minf_payload))
    mdia_payload.append(minf)
    mdia = _make_isom_box(b"mdia", b"".join(mdia_payload))
    trak_payload.append(mdia)
    trak = _make_isom_box(b"trak", b"".join(trak_payload))

    moov_body = mvhd + trak
    moov = _make_isom_box(b"moov", moov_body)

    # mdat — 10 zero bytes as fake "media data"
    mdat = struct.pack(">I", 10 + 8) + b"mdat" + b"\x00" * 10

    # fix stco chunk offset to point at mdat payload (skip 8-byte box header)
    pre_mdat = b"".join(parts) + moov
    stco_pos = pre_mdat.rindex(b"stco")
    prefix = pre_mdat[: stco_pos + 16]
    postfix = pre_mdat[stco_pos + 16 + 4 :]
    pre_mdat_fixed = prefix + struct.pack(">I", len(pre_mdat) + 8) + postfix

    return pre_mdat_fixed + mdat


def _build_minimal_mkv() -> bytes:
    """Build a minimal valid Matroska container (EBML + Segment)."""

    def _ebml_id(value: int) -> bytes:
        """Encode a variable-length EBML element ID."""
        if value < 0x80:
            return bytes([value])
        # simple fixed-length encoding for known IDs
        enc = []
        while value:
            enc.insert(0, value & 0xFF)
            value >>= 8
        # set the leading bit marker
        length = len(enc)
        if length == 1:
            enc[0] |= 0x80
        elif length == 2:
            enc[0] = 0x40 | (enc[0] & 0x3F)
        elif length == 3:
            enc[0] = 0x20 | (enc[0] & 0x1F)
        elif length == 4:
            enc[0] = 0x10 | (enc[0] & 0x0F)
        return bytes(enc)

    def _ebml_size(value: int) -> bytes:
        """Encode EBML variable-length size."""
        if value < 0x7F:
            return bytes([0x80 | value])
        # simple encoding
        needed = max(1, (value.bit_length() + 7) // 8)
        raw = value.to_bytes(needed, "big")
        marker = 0x80 >> (needed - 1) if needed <= 8 else 0x01
        return bytes([marker | raw[0]]) + raw[1:]

    def _ebml_element(eid: int, payload: bytes) -> bytes:
        return _ebml_id(eid) + _ebml_size(len(payload)) + payload

    # EBML header
    ebml_version = _ebml_element(0x4286, b"\x01")
    ebml_read_version = _ebml_element(0x42F7, b"\x01")
    ebml_max_id_length = _ebml_element(0x42F2, b"\x04")
    ebml_max_size_length = _ebml_element(0x42F3, b"\x08")
    doc_type = _ebml_element(0x4282, b"matroska")
    doc_type_version = _ebml_element(0x4287, b"\x04")
    doc_type_read_version = _ebml_element(0x4285, b"\x02")
    ebml_header = _ebml_element(
        0x1A45DFA3,
        ebml_version
        + ebml_read_version
        + ebml_max_id_length
        + ebml_max_size_length
        + doc_type
        + doc_type_version
        + doc_type_read_version,
    )

    # Segment content
    # Info
    timescale = _ebml_element(0x2AD7B1, struct.pack(">I", 1000000))
    duration = _ebml_element(0x4489, struct.pack(">f", 3.0))  # 3 seconds
    info = _ebml_element(0x1549A966, timescale + duration)

    # Tracks
    track_number = _ebml_element(0xD7, b"\x01")
    track_uid = _ebml_element(0x73C5, b"\x01")
    track_type = _ebml_element(0x83, b"\x01")  # video
    codec_id = _ebml_element(0x86, b"V_MPEG4/ISO/AVC")
    video = _ebml_element(0xE0, b"")
    track_entry = _ebml_element(
        0xAE, track_number + track_uid + track_type + codec_id + video
    )
    tracks = _ebml_element(0x1654AE6B, track_entry)

    # Cluster (empty — no actual frames)
    cluster_timecode = _ebml_element(0xE7, b"\x00\x00")
    cluster = _ebml_element(0x1F43B675, cluster_timecode)

    segment = _ebml_element(0x18538067, info + tracks + cluster)

    return ebml_header + segment


def _build_minimal_avi() -> bytes:
    """Build a minimal valid AVI container."""
    # RIFF header helpers
    def _chunk(fourcc: bytes, data: bytes) -> bytes:
        return fourcc + struct.pack("<I", len(data)) + data

    def _list(list_type: bytes, data: bytes) -> bytes:
        return b"LIST" + struct.pack("<I", len(data) + 4) + list_type + data

    # Main avih
    avih = b"avih" + struct.pack(
        "<IIIIIIIIIIIIII",
        1000000 // 30,  # dwMicroSecPerFrame (~30 fps)
        0,  # dwMaxBytesPerSec
        0,  # dwPaddingGranularity
        0x10,  # dwFlags (has index)
        1,  # dwTotalFrames
        0,  # dwInitialFrames
        1,  # dwStreams
        0,  # dwSuggestedBufferSize
        320,  # dwWidth
        240,  # dwHeight
        0,  # dwReserved[0..3]
        0,
        0,
        0,
    )

    # strl for video stream
    strh = b"strh" + struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"vids",  # fccType
        b"mp4v",  # fccHandler
        0,  # dwFlags
        0,  # wPriority
        0,  # wLanguage
        0,  # dwInitialFrames
        1000,  # dwScale
        30000,  # dwRate (30 fps)
        0,  # dwStart
        1,  # dwLength
        0,  # dwSuggestedBufferSize
        0,  # dwQuality
        0,  # dwSampleSize
        0,  # rcFrame.left
        0,  # rcFrame.top
        320,  # rcFrame.right
        240,  # rcFrame.bottom
    )

    # BITMAPINFOHEADER for strf
    strf = b"strf" + struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        320,  # biWidth
        240,  # biHeight
        1,  # biPlanes
        24,  # biBitCount
        0x00000000,  # biCompression (BI_RGB = 0)
        320 * 240 * 3,  # biSizeImage
        0,  # biXPelsPerMeter
        0,  # biYPelsPerMeter
        0,  # biClrUsed
        0,  # biClrImportant
    )

    strl = _list(b"strl", strh + strf)
    hdrl = _list(b"hdrl", avih + strl)

    # movi list — minimal dummy frame
    dummy_frame = b"\x00\x00\x00\x00"  # 4 byte dummy
    movi_entry = b"00db" + struct.pack("<I", len(dummy_frame)) + dummy_frame
    movi = _list(b"movi", movi_entry)

    # idx1 — 1 index entry
    idx1_entry = struct.pack(
        "<4sIII",
        b"00db",  # ckid
        0x10,  # flags (AVIIF_KEYFRAME)
        len(hdrl) + 12,  # offset from movi start
        len(dummy_frame),  # size
    )
    idx1 = b"idx1" + struct.pack("<I", 16) + idx1_entry

    riff_data = hdrl + movi + idx1
    riff = _chunk(b"RIFF", b"AVI " + riff_data)
    return riff


# ---------------------------------------------------------------------------
# Base64-encoded dummy video templates
# ---------------------------------------------------------------------------
DUMMY_VIDEOS: dict[str, str] = {
    ".mp4": base64.b64encode(_build_minimal_mp4()).decode(),
    ".mkv": base64.b64encode(_build_minimal_mkv()).decode(),
    ".avi": base64.b64encode(_build_minimal_avi()).decode(),
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    """SQLite state database for tracking archived items."""

    def __init__(self, db_path: str = "mediaspektor.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS archived_items (
                    server_type    TEXT NOT NULL,
                    server_item_id TEXT NOT NULL,
                    title          TEXT NOT NULL,
                    media_type     TEXT NOT NULL,
                    original_path  TEXT NOT NULL,
                    original_size_bytes INTEGER NOT NULL,
                    dummy_size_bytes     INTEGER NOT NULL,
                    archived_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    backup_poster_path   TEXT,
                    backup_media_path    TEXT,
                    status         TEXT DEFAULT 'archived',
                    PRIMARY KEY (server_type, server_item_id)
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def insert(self, **kwargs: Any) -> None:
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO archived_items ({columns}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_item(
        self, server_type: str, item_id: str
    ) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM archived_items WHERE server_type=? AND server_item_id=?",
                (server_type, item_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_status(self, server_type: str, item_id: str, status: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE archived_items SET status=? WHERE server_type=? AND server_item_id=?",
                (status, server_type, item_id),
            )
            conn.commit()
        finally:
            conn.close()

    def item_exists(self, server_type: str, item_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM archived_items WHERE server_type=? AND server_item_id=?",
                (server_type, item_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT COUNT(*) as total_items,
                          COALESCE(SUM(original_size_bytes), 0) as total_original,
                          COALESCE(SUM(dummy_size_bytes), 0) as total_dummy,
                          COALESCE(SUM(original_size_bytes - dummy_size_bytes), 0) as total_saved
                   FROM archived_items WHERE status='archived'"""
            ).fetchone()
            return {
                "total_items": row[0],
                "total_saved_bytes": row[3],
                "total_saved_gb": row[3] / (1024**3),
                "total_original_bytes": row[1],
                "total_dummy_bytes": row[2],
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Abstract Media Server Connector
# ---------------------------------------------------------------------------
class BaseMediaServer(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.server_type: str = ""

    @abstractmethod
    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def download_poster(self, item_id: str, target_path: str) -> bool: ...

    @abstractmethod
    def upload_poster(self, item_id: str, source_path: str) -> bool: ...

    @abstractmethod
    def trigger_library_scan(self) -> None: ...

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        return []

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        return []

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        return []

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        return []

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        return {}


# ---------------------------------------------------------------------------
# Plex Connector
# ---------------------------------------------------------------------------
class PlexConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "plex"
        if not HAS_PLEXAPI:
            raise RuntimeError(
                "plexapi library is required for Plex. Install with: pip install plexapi"
            )
        self._server = PlexServer(config["url"], config["token"])

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
            except Exception as exc:
                logger.warning("Plex: could not find library '%s': %s", lib_name, exc)
                continue

            if section.type == "movie":
                for movie in section.search():
                    if movie.isWatched:
                        media = movie.media[0] if movie.media else None
                        parts_list = media.parts if media else []
                        file_path = parts_list[0].file if parts_list else ""
                        size = parts_list[0].size if parts_list else 0
                        if file_path:
                            labels = [l.tag.lower() for l in movie.labels] if hasattr(movie, "labels") and movie.labels else []
                            genres = [g.tag.lower() for g in movie.genres] if hasattr(movie, "genres") and movie.genres else []
                            results.append(
                                {
                                    "id": movie.ratingKey,
                                    "title": movie.title,
                                    "type": "movie",
                                    "file_path": file_path,
                                    "original_size": size,
                                    "last_watched": movie.lastViewedAt if hasattr(movie, "lastViewedAt") else None,
                                    "genres": genres,
                                    "labels": labels,
                                }
                            )
            elif section.type == "show":
                for episode in section.search(libtype="episode"):
                    if episode.isWatched:
                        media = episode.media[0] if episode.media else None
                        parts_list = media.parts if media else []
                        file_path = parts_list[0].file if parts_list else ""
                        size = parts_list[0].size if parts_list else 0
                        if file_path:
                            labels = []
                            genres = []
                            try:
                                show = episode.show()
                                if show:
                                    if hasattr(show, "labels") and show.labels:
                                        labels = [l.tag.lower() for l in show.labels]
                                    if hasattr(show, "genres") and show.genres:
                                        genres = [g.tag.lower() for g in show.genres]
                            except Exception:
                                pass
                            results.append(
                                {
                                    "id": episode.ratingKey,
                                    "title": (episode.grandparentTitle or "Unknown Show")
                                    + " - "
                                    + (episode.title or "Unknown Episode"),
                                    "type": "episode",
                                    "file_path": file_path,
                                    "original_size": size,
                                    "last_watched": episode.lastViewedAt if hasattr(episode, "lastViewedAt") else None,
                                    "genres": genres,
                                    "labels": labels,
                                }
                            )
        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            item = self._server.fetchItem(item_id)
            if item.posterUrl:
                url = item.posterUrl
                # Plex posterUrl may be relative; build full URL
                if url.startswith("/"):
                    url = self.config["url"].rstrip("/") + url + "?X-Plex-Token=" + self.config["token"]
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                return True
            logger.warning("Plex item %s has no poster", item_id)
            return False
        except Exception as exc:
            logger.error("Plex: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            item = self._server.fetchItem(item_id)
            item.uploadPoster(filepath=source_path)
            return True
        except Exception as exc:
            logger.error("Plex: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self) -> None:
        try:
            for lib_name in self.config.get("libraries", []):
                section = self._server.library.section(lib_name)
                section.update()
            logger.info("Plex: library scan triggered")
        except Exception as exc:
            logger.error("Plex: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
                if section.type == "movie":
                    for movie in section.search():
                        media = movie.media[0] if movie.media else None
                        parts = media.parts if media else []
                        file_path = parts[0].file if parts else ""
                        size = parts[0].size if parts else 0
                        labels = [l.tag.lower() for l in movie.labels] if hasattr(movie, "labels") and movie.labels else []
                        genres = [g.tag.lower() for g in movie.genres] if hasattr(movie, "genres") and movie.genres else []
                        results.append({
                            "id": movie.ratingKey,
                            "title": movie.title,
                            "year": movie.year,
                            "file_path": file_path,
                            "original_size": size,
                            "last_watched": movie.lastViewedAt if hasattr(movie, "lastViewedAt") else None,
                            "is_watched": movie.isWatched,
                            "genres": genres,
                            "labels": labels,
                            "poster_path": movie.thumb
                        })
            except Exception as exc:
                logger.error("Plex get_movies error: %s", exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
                if section.type == "show":
                    for show in section.search():
                        labels = [l.tag.lower() for l in show.labels] if hasattr(show, "labels") and show.labels else []
                        genres = [g.tag.lower() for g in show.genres] if hasattr(show, "genres") and show.genres else []
                        results.append({
                            "id": show.ratingKey,
                            "title": show.title,
                            "year": show.year,
                            "is_watched": show.isWatched,
                            "genres": genres,
                            "labels": labels,
                            "poster_path": show.thumb
                        })
            except Exception as exc:
                logger.error("Plex get_shows error: %s", exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            show = self._server.fetchItem(show_id)
            for season in show.seasons():
                results.append({
                    "id": season.ratingKey,
                    "season_number": season.index,
                    "title": season.title,
                    "is_watched": season.isWatched,
                    "poster_path": season.thumb
                })
        except Exception as exc:
            logger.error("Plex get_seasons error: %s", exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            season = self._server.fetchItem(season_id)
            for episode in season.episodes():
                media = episode.media[0] if episode.media else None
                parts = media.parts if media else []
                file_path = parts[0].file if parts else ""
                size = parts[0].size if parts else 0
                results.append({
                    "id": episode.ratingKey,
                    "episode_number": episode.index,
                    "title": episode.title,
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": episode.isWatched,
                    "last_watched": episode.lastViewedAt if hasattr(episode, "lastViewedAt") else None,
                    "poster_path": episode.thumb
                })
        except Exception as exc:
            logger.error("Plex get_episodes error: %s", exc)
        return results

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        item = self._server.fetchItem(item_id)
        if item.type == "movie":
            media = item.media[0] if item.media else None
            parts = media.parts if media else []
            file_path = parts[0].file if parts else ""
            size = parts[0].size if parts else 0
            labels = [l.tag.lower() for l in item.labels] if hasattr(item, "labels") and item.labels else []
            genres = [g.tag.lower() for g in item.genres] if hasattr(item, "genres") and item.genres else []
            return {
                "id": item.ratingKey,
                "title": item.title,
                "type": "movie",
                "file_path": file_path,
                "original_size": size,
                "last_watched": item.lastViewedAt if hasattr(item, "lastViewedAt") else None,
                "genres": genres,
                "labels": labels
            }
        elif item.type == "episode":
            media = item.media[0] if item.media else None
            parts = media.parts if media else []
            file_path = parts[0].file if parts else ""
            size = parts[0].size if parts else 0
            labels = []
            genres = []
            try:
                show = item.show()
                if show:
                    if hasattr(show, "labels") and show.labels:
                        labels = [l.tag.lower() for l in show.labels]
                    if hasattr(show, "genres") and show.genres:
                        genres = [g.tag.lower() for g in show.genres]
            except Exception:
                pass
            return {
                "id": item.ratingKey,
                "title": (item.grandparentTitle or "Unknown Show") + " - " + (item.title or "Unknown Episode"),
                "type": "episode",
                "file_path": file_path,
                "original_size": size,
                "last_watched": item.lastViewedAt if hasattr(item, "lastViewedAt") else None,
                "genres": genres,
                "labels": labels
            }
        raise ValueError(f"Unsupported Plex item type: {item.type}")


# ---------------------------------------------------------------------------
# Jellyfin Connector
# ---------------------------------------------------------------------------
class JellyfinConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "jellyfin"
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.user_id = config["user_id"]
        self.headers: dict[str, str] = {
            "X-MediaBrowser-Token": self.api_key,
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = requests.get(url, headers=self.headers, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        # Get views (library folders)
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
        except Exception as exc:
            logger.error("Jellyfin: failed to fetch views: %s", exc)
            return results

        name_to_id: dict[str, str] = {v["Name"]: v["Id"] for v in views}

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                logger.warning("Jellyfin: library '%s' not found", lib_name)
                continue

            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "Filters": "IsPlayed",
                        "IncludeItemTypes": "Movie,Episode",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = 0
                    if media_sources:
                        size = media_sources[0].get("Size", 0)
                    if file_path:
                        user_data = item.get("UserData", {})
                        last_played_str = user_data.get("LastPlayedDate")
                        last_watched = _parse_iso_date(last_played_str)
                        labels = [t.lower() for t in item.get("Tags", [])]
                        genres = [g.lower() for g in item.get("Genres", [])]
                        results.append(
                            {
                                "id": item["Id"],
                                "title": item.get("Name", "Unknown"),
                                "type": (
                                    "movie"
                                    if item.get("Type") == "Movie"
                                    else "episode"
                                ),
                                "file_path": file_path,
                                "original_size": size,
                                "last_watched": last_watched,
                                "genres": genres,
                                "labels": labels,
                            }
                        )
            except Exception as exc:
                logger.error(
                    "Jellyfin: error fetching items from '%s': %s", lib_name, exc
                )

        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            resp = self._get(f"/Items/{item_id}/Images/Primary")
            with open(target_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            logger.error("Jellyfin: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            url = urljoin(
                self.base_url + "/", f"Items/{item_id}/Images/Primary"
            )
            with open(source_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers=self.headers,
                    data=f.read(),
                    params={"X-Emby-Client": "MediaSpektor"},
                    timeout=30,
                )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Jellyfin: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self) -> None:
        try:
            # Jellyfin uses /Library/Refresh
            url = urljoin(self.base_url + "/", "Library/Refresh")
            requests.post(url, headers=self.headers, timeout=30)
            logger.info("Jellyfin: library scan triggered")
        except Exception as exc:
            logger.error("Jellyfin: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Jellyfin views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Movie",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = media_sources[0].get("Size", 0) if media_sources else 0
                    user_data = item.get("UserData", {})
                    last_played_str = user_data.get("LastPlayedDate")
                    last_watched = _parse_iso_date(last_played_str)
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "file_path": file_path,
                        "original_size": size,
                        "last_watched": last_watched,
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Jellyfin get_movies from '%s': %s", lib_name, exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Jellyfin views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Series",
                        "Fields": "UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    user_data = item.get("UserData", {})
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Jellyfin get_shows from '%s': %s", lib_name, exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Seasons",
                params={"UserId": self.user_id, "Fields": "UserData"}
            )
            items = resp.json().get("Items", [])
            for item in items:
                user_data = item.get("UserData", {})
                results.append({
                    "id": item["Id"],
                    "season_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "is_watched": user_data.get("Played", False),
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Jellyfin get_seasons for %s: %s", show_id, exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Episodes",
                params={
                    "SeasonId": season_id,
                    "UserId": self.user_id,
                    "Fields": "Path,MediaSources,UserData"
                }
            )
            items = resp.json().get("Items", [])
            for item in items:
                media_sources = item.get("MediaSources", [])
                file_path = item.get("Path", "")
                size = media_sources[0].get("Size", 0) if media_sources else 0
                user_data = item.get("UserData", {})
                last_played_str = user_data.get("LastPlayedDate")
                last_watched = _parse_iso_date(last_played_str)
                results.append({
                    "id": item["Id"],
                    "episode_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": user_data.get("Played", False),
                    "last_watched": last_watched,
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Jellyfin get_episodes for season %s: %s", season_id, exc)
        return results

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        item = self._get(f"/Users/{self.user_id}/Items/{item_id}").json()
        media_sources = item.get("MediaSources", [])
        file_path = item.get("Path", "")
        size = media_sources[0].get("Size", 0) if media_sources else 0
        user_data = item.get("UserData", {})
        last_played_str = user_data.get("LastPlayedDate")
        last_watched = _parse_iso_date(last_played_str)
        labels = [t.lower() for t in item.get("Tags", [])]
        genres = [g.lower() for g in item.get("Genres", [])]
        
        item_type = item.get("Type", "")
        if item_type == "Movie":
            type_str = "movie"
            title_str = item.get("Name", "Unknown")
        elif item_type == "Episode":
            type_str = "episode"
            title_str = (item.get("SeriesName") or "Unknown Show") + " - " + (item.get("Name") or "Unknown Episode")
        else:
            type_str = item_type.lower()
            title_str = item.get("Name", "Unknown")

        return {
            "id": item["Id"],
            "title": title_str,
            "type": type_str,
            "file_path": file_path,
            "original_size": size,
            "last_watched": last_watched,
            "genres": genres,
            "labels": labels
        }


# ---------------------------------------------------------------------------
# Emby Connector
# ---------------------------------------------------------------------------
class EmbyConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "emby"
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.user_id = config["user_id"]
        self.headers: dict[str, str] = {
            "X-MediaBrowser-Token": self.api_key,
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = requests.get(url, headers=self.headers, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
        except Exception as exc:
            logger.error("Emby: failed to fetch views: %s", exc)
            return results

        name_to_id: dict[str, str] = {v["Name"]: v["Id"] for v in views}

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                logger.warning("Emby: library '%s' not found", lib_name)
                continue

            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "Filters": "IsPlayed",
                        "IncludeItemTypes": "Movie,Episode",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = 0
                    if media_sources:
                        size = media_sources[0].get("Size", 0)
                    if file_path:
                        user_data = item.get("UserData", {})
                        last_played_str = user_data.get("LastPlayedDate")
                        last_watched = _parse_iso_date(last_played_str)
                        labels = [t.lower() for t in item.get("Tags", [])]
                        genres = [g.lower() for g in item.get("Genres", [])]
                        results.append(
                            {
                                "id": item["Id"],
                                "title": item.get("Name", "Unknown"),
                                "type": (
                                    "movie"
                                    if item.get("Type") == "Movie"
                                    else "episode"
                                ),
                                "file_path": file_path,
                                "original_size": size,
                                "last_watched": last_watched,
                                "genres": genres,
                                "labels": labels,
                            }
                        )
            except Exception as exc:
                logger.error(
                    "Emby: error fetching items from '%s': %s", lib_name, exc
                )

        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            resp = self._get(f"/Items/{item_id}/Images/Primary")
            with open(target_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            logger.error("Emby: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            url = urljoin(
                self.base_url + "/", f"Items/{item_id}/Images/Primary"
            )
            with open(source_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers=self.headers,
                    data=f.read(),
                    params={"X-Emby-Client": "MediaSpektor"},
                    timeout=30,
                )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Emby: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self) -> None:
        try:
            url = urljoin(self.base_url + "/", "Library/Refresh")
            requests.post(url, headers=self.headers, timeout=30)
            logger.info("Emby: library scan triggered")
        except Exception as exc:
            logger.error("Emby: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Emby views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Movie",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = media_sources[0].get("Size", 0) if media_sources else 0
                    user_data = item.get("UserData", {})
                    last_played_str = user_data.get("LastPlayedDate")
                    last_watched = _parse_iso_date(last_played_str)
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "file_path": file_path,
                        "original_size": size,
                        "last_watched": last_watched,
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Emby get_movies from '%s': %s", lib_name, exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Emby views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Series",
                        "Fields": "UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    user_data = item.get("UserData", {})
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Emby get_shows from '%s': %s", lib_name, exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Seasons",
                params={"UserId": self.user_id, "Fields": "UserData"}
            )
            items = resp.json().get("Items", [])
            for item in items:
                user_data = item.get("UserData", {})
                results.append({
                    "id": item["Id"],
                    "season_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "is_watched": user_data.get("Played", False),
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Emby get_seasons for %s: %s", show_id, exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Episodes",
                params={
                    "SeasonId": season_id,
                    "UserId": self.user_id,
                    "Fields": "Path,MediaSources,UserData"
                }
            )
            items = resp.json().get("Items", [])
            for item in items:
                media_sources = item.get("MediaSources", [])
                file_path = item.get("Path", "")
                size = media_sources[0].get("Size", 0) if media_sources else 0
                user_data = item.get("UserData", {})
                last_played_str = user_data.get("LastPlayedDate")
                last_watched = _parse_iso_date(last_played_str)
                results.append({
                    "id": item["Id"],
                    "episode_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": user_data.get("Played", False),
                    "last_watched": last_watched,
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Emby get_episodes for season %s: %s", season_id, exc)
        return results

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        item = self._get(f"/Users/{self.user_id}/Items/{item_id}").json()
        media_sources = item.get("MediaSources", [])
        file_path = item.get("Path", "")
        size = media_sources[0].get("Size", 0) if media_sources else 0
        user_data = item.get("UserData", {})
        last_played_str = user_data.get("LastPlayedDate")
        last_watched = _parse_iso_date(last_played_str)
        labels = [t.lower() for t in item.get("Tags", [])]
        genres = [g.lower() for g in item.get("Genres", [])]
        
        item_type = item.get("Type", "")
        if item_type == "Movie":
            type_str = "movie"
            title_str = item.get("Name", "Unknown")
        elif item_type == "Episode":
            type_str = "episode"
            title_str = (item.get("SeriesName") or "Unknown Show") + " - " + (item.get("Name") or "Unknown Episode")
        else:
            type_str = item_type.lower()
            title_str = item.get("Name", "Unknown")

        return {
            "id": item["Id"],
            "title": title_str,
            "type": type_str,
            "file_path": file_path,
            "original_size": size,
            "last_watched": last_watched,
            "genres": genres,
            "labels": labels
        }


# ---------------------------------------------------------------------------
# Radarr / Sonarr Integration
# ---------------------------------------------------------------------------
class RadarrClient:
    def __init__(self, config: dict) -> None:
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}

    def unmonitor_movie_by_path(self, file_path: str) -> bool:
        try:
            url = urljoin(self.base_url + "/", "api/v3/movie")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            movies = resp.json()
            norm_path = os.path.normpath(file_path).lower()
            for movie in movies:
                movie_path = (
                    os.path.normpath(movie.get("path", "")).lower()
                )
                folder = (
                    os.path.normpath(movie.get("folderName", "")).lower()
                )
                if movie_path and (
                    norm_path.startswith(movie_path)
                    or norm_path.startswith(folder)
                ):
                    movie["monitored"] = False
                    put_url = urljoin(
                        self.base_url + "/", f"api/v3/movie/{movie['id']}"
                    )
                    put_resp = requests.put(
                        put_url,
                        headers=self.headers,
                        json=movie,
                        timeout=30,
                    )
                    put_resp.raise_for_status()
                    logger.info(
                        "Radarr: unmonitored movie id=%s path=%s",
                        movie["id"],
                        file_path,
                    )
                    return True
            logger.warning(
                "Radarr: no matching movie found for path '%s'", file_path
            )
            return False
        except Exception as exc:
            logger.error("Radarr: error unmonitoring movie: %s", exc)
            return False


class SonarrClient:
    def __init__(self, config: dict) -> None:
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}

    def unmonitor_episode_by_path(self, file_path: str) -> bool:
        try:
            # Get all series
            url = urljoin(self.base_url + "/", "api/v3/series")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            series_list = resp.json()

            norm_path = os.path.normpath(file_path).lower()
            for series in series_list:
                series_path = (
                    os.path.normpath(series.get("path", "")).lower()
                )
                if series_path and norm_path.startswith(series_path):
                    series_id = series["id"]
                    
                    # Get episode files for this series to find matching file ID
                    file_url = urljoin(
                        self.base_url + "/",
                        f"api/v3/episodefile?seriesId={series_id}",
                    )
                    file_resp = requests.get(
                        file_url, headers=self.headers, timeout=30
                    )
                    file_resp.raise_for_status()
                    episode_files = file_resp.json()
                    
                    episode_file_id = None
                    for ep_file in episode_files:
                        ep_file_path = os.path.normpath(ep_file.get("path", "")).lower()
                        if ep_file_path == norm_path:
                            episode_file_id = ep_file.get("id")
                            break
                    
                    if not episode_file_id:
                        continue
                    
                    # Get episodes for this series
                    ep_url = urljoin(
                        self.base_url + "/",
                        f"api/v3/episode?seriesId={series_id}",
                    )
                    ep_resp = requests.get(
                        ep_url, headers=self.headers, timeout=30
                    )
                    ep_resp.raise_for_status()
                    episodes = ep_resp.json()
                    
                    found_any = False
                    for ep in episodes:
                        if ep.get("episodeFileId") == episode_file_id:
                            ep["monitored"] = False
                            put_url = urljoin(
                                self.base_url + "/",
                                f"api/v3/episode/{ep['id']}",
                            )
                            put_resp = requests.put(
                                put_url,
                                headers=self.headers,
                                json=ep,
                                timeout=30,
                            )
                            put_resp.raise_for_status()
                            logger.info(
                                "Sonarr: unmonitored episode id=%s path=%s",
                                ep["id"],
                                file_path,
                            )
                            found_any = True
                    
                    if found_any:
                        return True
            logger.warning(
                "Sonarr: no matching episode found for path '%s'", file_path
            )
            return False
        except Exception as exc:
            logger.error("Sonarr: error unmonitoring episode: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Poster Overlay Engine
# ---------------------------------------------------------------------------
class PosterOverlay:
    def __init__(self, config: dict) -> None:
        aest = config.get("aesthetics", {})
        self.enabled = aest.get("enable_poster_overlay", True)
        self.banner_color = tuple(aest.get("banner_color", [20, 20, 20, 204]))
        self.border_color = tuple(
            aest.get("border_color", [212, 175, 55, 255])
        )
        self.font_name = aest.get("font_name", "Arial")
        self.font_size_ratio = aest.get("font_size_ratio", 0.045)

    def apply_overlay(
        self, image_path: str, output_path: str, gb_saved: float
    ) -> bool:
        """Apply glassmorphic banner overlay to poster image."""
        if not self.enabled:
            shutil.copy2(image_path, output_path)
            return True
        try:
            img = Image.open(image_path).convert("RGBA")
            draw = ImageDraw.Draw(img)
            width, height = img.size

            # Banner dimensions
            banner_height = int(height * 0.15)
            y_start = height - banner_height

            # Draw glassmorphic background
            overlay = Image.new("RGBA", (width, banner_height), self.banner_color)
            img.paste(overlay, (0, y_start), overlay)

            # Draw 1px border at top of banner
            for x in range(width):
                img.putpixel((x, y_start), self.border_color)

            # Text
            text = f"ARCHIVED \u2022 {gb_saved:.1f} GB SAVED"
            font_size = int(height * self.font_size_ratio)
            try:
                font = ImageFont.truetype(self.font_name, font_size)
            except (OSError, IOError):
                logger.debug(
                    "Font '%s' not found, using default", self.font_name
                )
                font = ImageFont.load_default()

            # Center text
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            text_x = (width - text_width) // 2
            text_y = y_start + (banner_height - text_height) // 2 - bbox[1]

            draw.text(
                (text_x, text_y),
                text,
                fill=(255, 255, 255, 255),
                font=font,
            )

            img.save(output_path, "PNG")
            return True
        except Exception as exc:
            logger.error("Poster overlay failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# MediaSpektor Orchestrator
# ---------------------------------------------------------------------------
class MediaSpektor:
    def __init__(self, config_path: str = "config.yaml") -> None:
        with open(config_path, "r") as f:
            self.config: dict = yaml.safe_load(f)

        config_dir = os.path.dirname(os.path.abspath(config_path))
        db_path = os.path.join(config_dir, "mediaspektor.db")
        self.db = Database(db_path)

        self.overlay = PosterOverlay(self.config)
        backup_path = self.config.get("safety", {}).get(
            "backup_directory", os.path.join(config_dir, "backups")
        )
        self.backup_dir = Path(backup_path)
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            logger.warning(
                "Cannot create backup directory '%s': %s. Using ./backups instead.",
                backup_path, exc,
            )
            self.backup_dir = Path("./backups")
            self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Initialize server connectors
        self.servers: list[BaseMediaServer] = []
        for server_cfg in self.config.get("servers", []):
            if not server_cfg.get("enabled", False):
                continue
            connector = self._create_connector(server_cfg)
            if connector:
                self.servers.append(connector)

        # Initialize *Arr clients
        self.radarr: RadarrClient | None = None
        self.sonarr: SonarrClient | None = None
        integrations = self.config.get("integrations", {})
        if integrations.get("radarr", {}).get("enabled", False):
            self.radarr = RadarrClient(integrations["radarr"])
        if integrations.get("sonarr", {}).get("enabled", False):
            self.sonarr = SonarrClient(integrations["sonarr"])

    def _create_connector(
        self, cfg: dict
    ) -> BaseMediaServer | None:
        server_type = cfg.get("type", "").lower()
        try:
            if server_type == "plex":
                return PlexConnector(cfg)
            elif server_type == "jellyfin":
                return JellyfinConnector(cfg)
            elif server_type == "emby":
                return EmbyConnector(cfg)
            else:
                logger.warning("Unknown server type: %s", server_type)
                return None
        except Exception as exc:
            logger.warning("Failed to create %s connector: %s", server_type, exc)
            return None

    def _filter_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rules = self.config.get("rules", {})
        min_age_days = rules.get("min_age_days", 7)
        dummy_threshold_bytes = rules.get("dummy_threshold_mb", 15) * 1024 * 1024
        exclude_labels = [l.lower() for l in rules.get("exclude_labels", [])]
        exclude_genres = [g.lower() for g in rules.get("exclude_genres", [])]

        filtered: list[dict[str, Any]] = []
        for item in items:
            path = item.get("file_path", "")
            size = item.get("original_size", 0)

            if not path or not os.path.exists(path):
                continue
            if size < dummy_threshold_bytes:
                continue

            # 1. Check exclusions
            item_labels = [l.lower() for l in item.get("labels", [])]
            item_genres = [g.lower() for g in item.get("genres", [])]

            if any(l in item_labels for l in exclude_labels):
                logger.debug("Skipping '%s' due to excluded label", item.get("title"))
                continue

            if any(g in item_genres for g in exclude_genres):
                logger.debug("Skipping '%s' due to excluded genre", item.get("title"))
                continue

            # 2. Check watch age (retention grace period)
            last_watched = item.get("last_watched")
            if min_age_days > 0:
                if not last_watched:
                    # If we don't know when it was watched, skip it for safety
                    logger.debug("Skipping '%s' because watch date is unknown", item.get("title"))
                    continue
                if last_watched.tzinfo is not None:
                    cutoff = datetime.now(timezone.utc)
                else:
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
                if last_watched > cutoff - timedelta(days=min_age_days):
                    logger.debug("Skipping '%s' because it was watched recently (%s)", item.get("title"), last_watched)
                    continue

            filtered.append(item)

        return filtered

    def scan(self) -> dict[str, Any]:
        """Dry-run scan: report what would be archived without touching anything."""
        report: dict[str, Any] = {"servers": {}, "total_savings_gb": 0.0, "total_items": 0}
        for server in self.servers:
            cfg = server.config
            libs = cfg.get("libraries", [])
            items = server.get_watched_items(libs)
            filtered = self._filter_items(items)
            saved = sum(
                item.get("original_size", 0) for item in filtered
            ) - sum(
                len(base64.b64decode(DUMMY_VIDEOS.get(
                    os.path.splitext(item.get("file_path", ""))[1].lower(), ""
                ) or "AA=="))
                for item in filtered
            )
            server_name = f"{server.server_type} ({cfg.get('url', '?')})"
            report["servers"][server_name] = {
                "watched_found": len(items),
                "candidates": len(filtered),
                "estimated_savings_gb": saved / (1024**3),
            }
            report["total_items"] += len(filtered)
            report["total_savings_gb"] += saved / (1024**3)

        return report

    def archive(self, dry_run: bool = False) -> dict[str, Any]:
        """Run the full archival process."""
        results: dict[str, Any] = {"archived": [], "errors": [], "skipped": []}

        for server in self.servers:
            cfg = server.config
            libs = cfg.get("libraries", [])
            logger.info(
                "Processing %s server: %s", server.server_type, cfg["url"]
            )

            items = server.get_watched_items(libs)
            filtered = self._filter_items(items)

            for item in filtered:
                item_id = item["id"]
                title = item["title"]
                file_path = item["file_path"]
                original_size = item["original_size"]
                media_type = item["type"]
                ext = os.path.splitext(file_path)[1].lower()

                if self.db.item_exists(server.server_type, item_id):
                    logger.debug("Already archived: %s", title)
                    results["skipped"].append(title)
                    continue

                dummy_base64 = DUMMY_VIDEOS.get(ext)
                if not dummy_base64:
                    logger.warning(
                        "No dummy template for extension '%s' — skipping %s",
                        ext,
                        title,
                    )
                    results["skipped"].append(title)
                    continue

                gb_saved = (original_size - 20000) / (1024**3)  # approx

                if dry_run:
                    logger.info("[DRY-RUN] Would archive: %s (%.2f GB)", title, gb_saved)
                    results["archived"].append(title)
                    continue

                logger.info("Archiving: %s (%.2f GB)", title, gb_saved)
                backup_poster_path: str | None = None
                backup_media_path: str | None = None
                poster_success = False

                try:
                    # 1. Download poster
                    poster_tmp = f"/tmp/mediaspektor_poster_{item_id}.jpg"
                    if server.download_poster(item_id, poster_tmp):
                        # Backup original poster
                        poster_backup = (
                            self.backup_dir
                            / f"{server.server_type}_{item_id}_poster_original.jpg"
                        )
                        shutil.copy2(poster_tmp, str(poster_backup))
                        backup_poster_path = str(poster_backup)

                        # Apply overlay
                        poster_overlay = (
                            self.backup_dir
                            / f"{server.server_type}_{item_id}_poster_overlay.png"
                        )
                        self.overlay.apply_overlay(
                            poster_tmp, str(poster_overlay), gb_saved
                        )

                        # Upload modified poster
                        if server.upload_poster(item_id, str(poster_overlay)):
                            poster_success = True
                        else:
                            raise RuntimeError("Failed to upload poster")

                        # Clean up tmp
                        os.unlink(poster_tmp)
                    else:
                        logger.warning(
                            "No poster for %s — skipping overlay", title
                        )

                    # 2. Backup original media if configured
                    if self.config.get("safety", {}).get(
                        "backup_original_media", False
                    ):
                        backup_media = (
                            self.backup_dir
                            / f"{server.server_type}_{item_id}{ext}"
                        )
                        shutil.move(file_path, str(backup_media))
                        backup_media_path = str(backup_media)
                    else:
                        os.unlink(file_path)

                    # 3. Write dummy file
                    dummy_bytes = base64.b64decode(dummy_base64)
                    with open(file_path, "wb") as f:
                        f.write(dummy_bytes)

                    # 4. Log to database
                    self.db.insert(
                        server_type=server.server_type,
                        server_item_id=item_id,
                        title=title,
                        media_type=media_type,
                        original_path=file_path,
                        original_size_bytes=original_size,
                        dummy_size_bytes=len(dummy_bytes),
                        backup_poster_path=backup_poster_path,
                        backup_media_path=backup_media_path,
                        status="archived",
                    )

                    # 5. Unmonitor in *Arr
                    if media_type == "movie" and self.radarr:
                        self.radarr.unmonitor_movie_by_path(file_path)
                    elif media_type == "episode" and self.sonarr:
                        self.sonarr.unmonitor_episode_by_path(file_path)

                    results["archived"].append(title)

                except Exception as exc:
                    logger.error(
                        "Failed to archive '%s': %s — rolling back", title, exc
                    )
                    # Rollback: restore poster if uploaded
                    if poster_success and backup_poster_path:
                        try:
                            server.upload_poster(item_id, backup_poster_path)
                        except Exception as rb_exc:
                            logger.error(
                                "Rollback poster failed: %s", rb_exc
                            )
                    results["errors"].append({"title": title, "error": str(exc)})

            # Trigger scan after processing all items for this server
            if results["archived"] and not dry_run:
                server.trigger_library_scan()

        return results

    def restore(self, server_type: str, item_id: str) -> bool:
        """Restore a single archived item."""
        record = self.db.get_item(server_type, item_id)
        if not record:
            logger.error("No archived record for %s/%s", server_type, item_id)
            return False

        logger.info("Restoring: %s", record["title"])

        # Find the matching server connector
        server = None
        for s in self.servers:
            if s.server_type == server_type:
                server = s
                break

        if not server:
            logger.error(
                "No active %s server configured — cannot restore poster",
                server_type,
            )
        else:
            # Restore original poster
            backup_poster = record.get("backup_poster_path")
            if backup_poster and os.path.exists(backup_poster):
                try:
                    server.upload_poster(item_id, backup_poster)
                    logger.info("Restored poster for %s", record["title"])
                except Exception as exc:
                    logger.error(
                        "Failed to restore poster: %s", exc
                    )

            # Check if backup media exists
            backup_media = record.get("backup_media_path")
            if backup_media and os.path.exists(backup_media):
                original_path = record["original_path"]
                shutil.move(backup_media, original_path)
                logger.info(
                    "Restored media file to %s", original_path
                )
            else:
                logger.warning(
                    "Original media backup not found at %s — "
                    "please manually restore the file to: %s",
                    backup_media,
                    record["original_path"],
                )

        self.db.update_status(server_type, item_id, "restored")
        return True

    def stats(self) -> dict[str, Any]:
        return self.db.get_stats()

    def archive_item(self, server_type: str, item_id: str) -> dict[str, Any]:
        """Archive a single chosen movie or episode."""
        results: dict[str, Any] = {"success": False, "error": None}
        
        # 1. Find matching server
        server = None
        for s in self.servers:
            if s.server_type == server_type:
                server = s
                break
        
        if not server:
            results["error"] = f"No active {server_type} server configured."
            logger.error(results["error"])
            return results

        try:
            # 2. Get item metadata
            item = server.get_item_metadata(item_id)
            if not item:
                raise ValueError("Item metadata could not be retrieved from server.")

            title = item["title"]
            file_path = item["file_path"]
            original_size = item["original_size"]
            media_type = item["type"]
            ext = os.path.splitext(file_path)[1].lower()

            if self.db.item_exists(server_type, item_id):
                results["error"] = f"Item '{title}' is already archived."
                logger.warning(results["error"])
                return results

            dummy_base64 = DUMMY_VIDEOS.get(ext)
            if not dummy_base64:
                raise ValueError(f"No dummy template for extension '{ext}'")

            gb_saved = (original_size - 20000) / (1024**3)

            logger.info("Spektoring single item: %s (%.2f GB)", title, gb_saved)
            backup_poster_path: str | None = None
            backup_media_path: str | None = None
            poster_success = False

            # A. Download and Overlay Poster
            poster_tmp = f"/tmp/mediaspektor_poster_{item_id}.jpg"
            if server.download_poster(item_id, poster_tmp):
                # Backup original
                poster_backup = self.backup_dir / f"{server_type}_{item_id}_poster_original.jpg"
                shutil.copy2(poster_tmp, str(poster_backup))
                backup_poster_path = str(poster_backup)

                # Create and apply overlay
                poster_overlay = self.backup_dir / f"{server_type}_{item_id}_poster_overlay.png"
                self.overlay.apply_overlay(poster_tmp, str(poster_overlay), gb_saved)

                # Upload modified poster
                if server.upload_poster(item_id, str(poster_overlay)):
                    poster_success = True
                else:
                    raise RuntimeError("Failed to upload modified poster back to server.")
                
                if os.path.exists(poster_tmp):
                    os.unlink(poster_tmp)
            else:
                logger.warning("No poster found for %s — skipping overlay", title)

            # B. Backup original media if configured
            if self.config.get("safety", {}).get("backup_original_media", False):
                backup_media = self.backup_dir / f"{server_type}_{item_id}{ext}"
                shutil.move(file_path, str(backup_media))
                backup_media_path = str(backup_media)
            else:
                if os.path.exists(file_path):
                    os.unlink(file_path)

            # C. Write dummy file
            dummy_bytes = base64.b64decode(dummy_base64)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(dummy_bytes)

            # D. Insert to DB
            self.db.insert(
                server_type=server_type,
                server_item_id=item_id,
                title=title,
                media_type=media_type,
                original_path=file_path,
                original_size_bytes=original_size,
                dummy_size_bytes=len(dummy_bytes),
                backup_poster_path=backup_poster_path,
                backup_media_path=backup_media_path,
                status="archived"
            )

            # E. Unmonitor in Arr
            if media_type == "movie" and self.radarr:
                self.radarr.unmonitor_movie_by_path(file_path)
            elif media_type == "episode" and self.sonarr:
                self.sonarr.unmonitor_episode_by_path(file_path)

            # F. Scan library
            server.trigger_library_scan()

            results["success"] = True
            logger.info("Successfully archived and 'Spektored' item: %s", title)
            return results

        except Exception as exc:
            logger.error("Failed to archive item %s: %s", item_id, exc)
            # Rollback poster if uploaded
            if poster_success and backup_poster_path:
                try:
                    server.upload_poster(item_id, backup_poster_path)
                except Exception as rb_exc:
                    logger.error("Rollback poster failed: %s", rb_exc)
            results["error"] = str(exc)
            return results


# ---------------------------------------------------------------------------
# FastAPI Web Server
# ---------------------------------------------------------------------------
import collections

class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.logs = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.logs.append(log_entry)
        except Exception:
            self.handleError(record)

memory_log_handler = MemoryLogHandler()
memory_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
memory_log_handler.setLevel(logging.INFO)
logging.getLogger("mediaspektor").addHandler(memory_log_handler)
logging.getLogger().addHandler(memory_log_handler)

# Initialize FastAPI App
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="MediaSpektor", description="Modern self-hosted watch state storage archiver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables for Orchestrator and config path
GLOBAL_SPEKTOR: MediaSpektor | None = None
CONFIG_PATH: str = "config.yaml"

def get_spektor() -> MediaSpektor:
    global GLOBAL_SPEKTOR
    if GLOBAL_SPEKTOR is None:
        GLOBAL_SPEKTOR = MediaSpektor(CONFIG_PATH)
    return GLOBAL_SPEKTOR

# Static files mapping
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

@app.get("/api/config")
def get_config():
    spektor = get_spektor()
    return spektor.config

class UpdateConfigReq(BaseModel):
    config: dict

@app.post("/api/config")
def update_config(req: UpdateConfigReq):
    global GLOBAL_SPEKTOR
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(req.config, f)
        GLOBAL_SPEKTOR = MediaSpektor(CONFIG_PATH)
        logger.info("Configuration updated and reloaded successfully.")
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/stats")
def get_web_stats():
    spektor = get_spektor()
    return spektor.stats()

@app.get("/api/logs")
def get_web_logs():
    return list(memory_log_handler.logs)

@app.get("/api/movies")
def get_web_movies():
    spektor = get_spektor()
    all_movies = []
    for server in spektor.servers:
        libs = server.config.get("libraries", [])
        movies = server.get_movies(libs)
        for m in movies:
            db_item = spektor.db.get_item(server.server_type, m["id"])
            m["status"] = db_item["status"] if db_item else "original"
            m["server_type"] = server.server_type
            all_movies.append(m)
    return all_movies

@app.get("/api/shows")
def get_web_shows():
    spektor = get_spektor()
    all_shows = []
    for server in spektor.servers:
        libs = server.config.get("libraries", [])
        shows = server.get_shows(libs)
        for s in shows:
            s["server_type"] = server.server_type
            all_shows.append(s)
    return all_shows

@app.get("/api/shows/{server_type}/{show_id}/seasons")
def get_web_seasons(server_type: str, show_id: str):
    spektor = get_spektor()
    for server in spektor.servers:
        if server.server_type == server_type:
            return server.get_seasons(show_id)
    raise HTTPException(status_code=404, detail="Server type not found or not active")

@app.get("/api/shows/{server_type}/{show_id}/seasons/{season_id}/episodes")
def get_web_episodes(server_type: str, show_id: str, season_id: str):
    spektor = get_spektor()
    for server in spektor.servers:
        if server.server_type == server_type:
            episodes = server.get_episodes(show_id, season_id)
            for ep in episodes:
                db_item = spektor.db.get_item(server_type, ep["id"])
                ep["status"] = db_item["status"] if db_item else "original"
            return episodes
    raise HTTPException(status_code=404, detail="Server type not found or not active")

@app.get("/api/posterproxy")
def poster_proxy(server_type: str, item_id: str):
    spektor = get_spektor()
    server = None
    for s in spektor.servers:
        if s.server_type == server_type:
            server = s
            break
    if not server:
        raise HTTPException(status_code=404, detail=f"Active server connector '{server_type}' not found.")

    try:
        if server_type == "plex":
            item = server._server.fetchItem(item_id)
            if not item.posterUrl:
                raise HTTPException(status_code=404, detail="Plex poster url missing")
            url = item.posterUrl
            if url.startswith("/"):
                url = server.config["url"].rstrip("/") + url + "?X-Plex-Token=" + server.config["token"]
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            return StreamingResponse(resp.iter_content(chunk_size=1024), media_type=resp.headers.get("Content-Type", "image/jpeg"))
        elif server_type in ("jellyfin", "emby"):
            url = urljoin(server.base_url + "/", f"Items/{item_id}/Images/Primary")
            resp = requests.get(url, headers=server.headers, timeout=30, stream=True)
            resp.raise_for_status()
            return StreamingResponse(resp.iter_content(chunk_size=1024), media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Poster proxy failed: {exc}")

class ActionReq(BaseModel):
    server_type: str
    item_id: str

def run_bg_spektor(server_type: str, item_id: str):
    spektor = get_spektor()
    spektor.archive_item(server_type, item_id)

def run_bg_restore(server_type: str, item_id: str):
    spektor = get_spektor()
    spektor.restore(server_type, item_id)

@app.post("/api/spektor")
def trigger_spektor(req: ActionReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_spektor, req.server_type, req.item_id)
    return {"success": True, "message": "Archival process queued as background task."}

@app.post("/api/restore")
def trigger_restore(req: ActionReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_restore, req.server_type, req.item_id)
    return {"success": True, "message": "Restoration process queued as background task."}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MediaSpektor — Reclaim disk space by archiving watched media.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override config to force dry-run simulation",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run a dry-run inspection and report space savings",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Run the full archival execution",
    )
    parser.add_argument(
        "--restore",
        nargs=2,
        metavar=("SERVER_TYPE", "ITEM_ID"),
        help="Restore a previously archived item",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Display archive statistics",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the web server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to run the web server on (default: 5000)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("mediaspektor").setLevel(logging.DEBUG)
    else:
        logging.getLogger("mediaspektor").setLevel(logging.INFO)

    global CONFIG_PATH
    CONFIG_PATH = args.config

    # Initialize orchestrator
    get_spektor()

    if args.scan or args.archive or args.restore or args.stats:
        ghost = get_spektor()
        if args.stats:
            stats = ghost.stats()
            print(json.dumps(stats, indent=2))
            return

        if args.restore:
            server_type, item_id = args.restore
            success = ghost.restore(server_type, item_id)
            if success:
                logger.info("Restore complete for %s/%s", server_type, item_id)
            else:
                logger.error("Restore failed")
                sys.exit(1)
            return

        dry_run = args.dry_run or (
            ghost.config.get("safety", {}).get("dry_run", False)
        )

        if args.scan:
            report = ghost.scan()
            print(json.dumps(report, indent=2))
            return

        if args.archive:
            results = ghost.archive(dry_run=dry_run)
            print(json.dumps(results, indent=2))
            if results["errors"]:
                sys.exit(1)
            return
    else:
        logger.info("Starting MediaSpektor Self-Hosted Web App...")
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main()
