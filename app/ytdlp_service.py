"""Thin wrapper around yt-dlp for resolving playable stream URLs.

yt-dlp performs metadata extraction only here (no downloading). For some
extractors (notably YouTube) yt-dlp needs a JavaScript runtime on the server to
solve signature / n-sig challenges. yt-dlp auto-detects an available runtime
(Deno is preferred, Node works too). See README for details.
"""
from __future__ import annotations

import asyncio
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from yt_dlp import YoutubeDL


def detect_js_runtime() -> Optional[str]:
    """Return the path of a JS runtime yt-dlp can use, if one is installed."""
    for runtime in ("deno", "node", "nodejs"):
        path = shutil.which(runtime)
        if path:
            return f"{runtime} ({path})"
    return None


# Substrings that mark a *transport* failure (origin unreachable, timeout, DNS,
# TLS, origin 5xx/429). Everything else — "Unsupported URL", "Unable to extract",
# "no video formats" — means the page was fetched fine but simply isn't media,
# which is an expected outcome on home/listing/search pages.
_UPSTREAM_MARKERS = (
    "unable to download webpage",
    "unable to download api page",
    "timed out",
    "timeout",
    "connection",
    "temporary failure",
    "name resolution",
    "ssl",
    "failed to resolve",
    "503",
    "502",
    "500",
    "429",
)


def status_for_error(exc: Exception) -> int:
    """Map a yt-dlp extraction failure to an HTTP status.

    Returns 502 only for genuine upstream/transport failures; an expected
    "this isn't a media page" outcome maps to 422 so it doesn't read as a
    server error in logs or trip 5xx alerting. Ambiguous failures default to
    422 (the player just falls back to links/recommendations either way)."""
    msg = str(exc).lower()
    return 502 if any(m in msg for m in _UPSTREAM_MARKERS) else 422


# Base options shared by every extraction. We never download; we only want the
# metadata yt-dlp resolves (direct URLs, formats, protocols, etc.).
_BASE_OPTS: Dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    # Surface formats from every available client; YouTube in particular hides
    # progressive/HLS formats behind specific player clients.
    "extractor_args": {"youtube": {"player_client": ["default", "web_safari"]}},
}

# Cap on how many linked (playlist/channel) videos we surface.
_RELATED_LIMIT = 60

# Cap on the "more from this uploader" feed we synthesize for a standalone video
# (yt-dlp exposes no related feed on a watch page; the channel is the next best).
_CHANNEL_RELATED_LIMIT = 12


def _protocol_kind(protocol: Optional[str]) -> str:
    """Collapse yt-dlp's protocol strings into what the browser needs to know."""
    if not protocol:
        return "http"
    if "m3u8" in protocol:
        return "hls"
    if "dash" in protocol or protocol == "http_dash_segments":
        return "dash"
    if protocol.startswith("http") or protocol == "https":
        return "http"
    return protocol


def _is_playable_in_single_tag(fmt: Dict[str, Any]) -> bool:
    """A <video> tag (or hls.js) needs one URL carrying both audio and video.

    HLS master playlists mux internally so they qualify even when yt-dlp reports
    a video-only codec on the variant. Progressive formats must carry both.
    """
    kind = _protocol_kind(fmt.get("protocol"))
    if kind == "hls":
        return True
    if kind == "http":
        return fmt.get("vcodec") not in (None, "none") and fmt.get(
            "acodec"
        ) not in (None, "none")
    return False


def _format_to_source(fmt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = fmt.get("url")
    if not url:
        return None
    kind = _protocol_kind(fmt.get("protocol"))
    height = fmt.get("height")
    label = (
        f"{height}p" if height else (fmt.get("format_note") or fmt.get("ext") or "media")
    )
    return {
        "format_id": fmt.get("format_id"),
        "url": url,
        "protocol": kind,
        "ext": fmt.get("ext"),
        "height": height,
        "width": fmt.get("width"),
        "fps": fmt.get("fps"),
        "vcodec": fmt.get("vcodec"),
        "acodec": fmt.get("acodec"),
        "tbr": fmt.get("tbr"),
        "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
        "label": f"{label} · {kind.upper()} · {fmt.get('ext') or '?'}",
    }


def _entry_to_related(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A lightweight reference to a linked video (no formats resolved)."""
    link = entry.get("url") or entry.get("webpage_url")
    if not link:
        return None
    thumb = entry.get("thumbnail")
    if not thumb and entry.get("thumbnails"):
        thumb = entry["thumbnails"][-1].get("url")
    return {
        "url": link,
        "title": entry.get("title"),
        "id": entry.get("id"),
        "duration": entry.get("duration"),
        "thumbnail": thumb,
    }


def _flat_opts() -> Dict[str, Any]:
    # Flatten playlist entries instead of resolving each, and stop early so we
    # never page through a huge channel.
    return {**_BASE_OPTS, "extract_flat": "in_playlist",
            "playlistend": _RELATED_LIMIT}


def _channel_related_sync(
    channel_url: str, exclude_id: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Flat-list a channel/uploader's recent videos (the current one excluded)."""
    opts = {**_BASE_OPTS, "extract_flat": "in_playlist", "playlistend": limit + 1}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    if not info or not info.get("entries"):
        return []
    related: List[Dict[str, Any]] = []
    for entry in info["entries"]:
        if not entry or (exclude_id and entry.get("id") == exclude_id):
            continue
        if (r := _entry_to_related(entry)):
            related.append(r)
        if len(related) >= limit:
            break
    return related


# A standalone page load fires /api/resolve and /api/links concurrently, and
# both start with the *same* flat extraction. This short-TTL cache (with a
# per-URL lock so the second caller waits for the first instead of racing it)
# collapses that into a single yt-dlp call — and remembers failures too, so a
# non-media page is probed once, not twice, per visit.
_CACHE_TTL = 30.0
_cache: Dict[str, tuple] = {}            # url -> (expires_at, ("ok", info) | ("err", exc))
_cache_lock = threading.Lock()
_url_locks: Dict[str, threading.Lock] = {}


def _lock_for(url: str) -> threading.Lock:
    with _cache_lock:
        lk = _url_locks.get(url)
        if lk is None:
            lk = _url_locks[url] = threading.Lock()
        return lk


def _cache_get(url: str):
    with _cache_lock:
        ent = _cache.get(url)
        if ent and ent[0] > time.monotonic():
            return ent[1]
        if ent:
            _cache.pop(url, None)
        return None


def _flat_extract(url: str) -> Dict[str, Any]:
    """Flat-extract `url` once, sharing the result across resolve+links and
    repeat visits within the TTL. Raises the original error on a cached miss."""
    hit = _cache_get(url)
    if hit is None:
        with _lock_for(url):
            hit = _cache_get(url)  # re-check now that we hold the lock
            if hit is None:
                try:
                    with YoutubeDL(_flat_opts()) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if info is None:
                        raise ValueError("yt-dlp returned no information for this URL")
                    # Materialise any lazy entries list so it survives caching.
                    if info.get("entries") is not None:
                        info["entries"] = list(info["entries"])
                    hit = ("ok", info)
                except Exception as exc:  # cache the failure too (dedupe probes)
                    hit = ("err", exc)
                with _cache_lock:
                    _cache[url] = (time.monotonic() + _CACHE_TTL, hit)
    kind, payload = hit
    if kind == "err":
        raise payload
    return payload


def _links_sync(url: str) -> Dict[str, Any]:
    """Fast path: just the videos linked from the URL (flat, no full resolve)."""
    info = _flat_extract(url)
    related: List[Dict[str, Any]] = []
    playlist: Optional[Dict[str, Any]] = None
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        related = [r for e in entries[:_RELATED_LIMIT]
                   if (r := _entry_to_related(e))]
        playlist = {"title": info.get("title"),
                    "count": info.get("playlist_count") or len(entries),
                    "kind": "playlist"}
    else:
        # Standalone video: yt-dlp surfaces no related feed on a watch page, so
        # approximate one with "more from this uploader" from the channel.
        channel = info.get("channel_url") or info.get("uploader_url")
        if channel:
            try:
                related = _channel_related_sync(
                    channel, info.get("id"), _CHANNEL_RELATED_LIMIT
                )
            except Exception:
                related = []  # channel listing is best-effort; never block links
            if related:
                uploader = info.get("uploader") or info.get("channel")
                playlist = {
                    "title": f"More from {uploader}" if uploader
                    else "More from this channel",
                    "count": len(related),
                    "kind": "channel",
                }
    return {
        "webpage_url": info.get("webpage_url") or url,
        "playlist": playlist,
        "related": related,
    }


async def links(url: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_links_sync, url)


def _extract_sync(url: str) -> Dict[str, Any]:
    # Pass 1 (cheap): flatten playlist entries instead of resolving each. A
    # standalone video URL is still fully extracted here ('in_playlist' only
    # flattens items *inside* a playlist), so single videos cost just one call.
    # Shared with /api/links via the flat-extract cache.
    info = _flat_extract(url)

    related: List[Dict[str, Any]] = []
    playlist: Optional[Dict[str, Any]] = None

    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("playlist contained no playable entries")
        # Every entry is a video linked from this URL.
        related = [r for e in entries[:_RELATED_LIMIT] if (r := _entry_to_related(e))]
        playlist = {"title": info.get("title"), "count": len(entries)}
        # Resolve the first entry fully so there's something to play (pass 2).
        first_url = entries[0].get("url") or entries[0].get("webpage_url")
        with YoutubeDL({**_BASE_OPTS, "noplaylist": True}) as ydl:
            info = ydl.extract_info(first_url, download=False)
        if info is None:
            raise ValueError("could not resolve the first playlist entry")

    raw_formats: List[Dict[str, Any]] = info.get("formats") or []

    # Defensive: if the flat pass didn't yield formats for a single video,
    # re-extract it fully.
    if not raw_formats and not info.get("url"):
        with YoutubeDL({**_BASE_OPTS, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False) or info
        raw_formats = info.get("formats") or []

    # If the extractor gave us a top-level url (single-format result) and no
    # formats list, synthesize one entry from the info dict itself.
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    sources: List[Dict[str, Any]] = []
    for fmt in raw_formats:
        if not _is_playable_in_single_tag(fmt):
            continue
        src = _format_to_source(fmt)
        if src:
            sources.append(src)

    # Order: highest resolution first, HLS slightly preferred at equal height.
    sources.sort(
        key=lambda s: (s.get("height") or 0, 1 if s["protocol"] == "hls" else 0),
        reverse=True,
    )

    # Deduplicate by (protocol, height, ext) so the picker isn't noisy.
    seen = set()
    deduped = []
    for s in sources:
        key = (s["protocol"], s.get("height"), s.get("ext"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "sources": deduped,
        # Videos linked from the URL (playlist/channel entries). Empty for a
        # standalone video page — yt-dlp exposes no "related/recommended" feed
        # there; the FTS5 recommender approximates that instead.
        "related": related,
        "playlist": playlist,
    }


async def resolve(url: str) -> Dict[str, Any]:
    """Async wrapper: run the blocking yt-dlp extraction in a worker thread."""
    return await asyncio.to_thread(_extract_sync, url)
