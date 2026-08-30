"""Pydantic models for request bodies and typed API responses.

These give us validated I/O and a fully populated OpenAPI schema at /docs.

A deliberate choice: URLs that we pass through *byte-for-byte* (the /api/proxy
target, and the page URL handed to yt-dlp) stay plain `str`, validated with a
cheap scheme check. Coercing them through pydantic's `HttpUrl` would normalise
them (trailing slashes, re-encoding) and can break signed CDN query strings.
`HttpUrl` is used only where we *store* a URL (CacheItem), not where we relay it.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


# --- requests ---------------------------------------------------------------

class CacheItem(BaseModel):
    url: HttpUrl = Field(..., description="Page URL (validated + normalised)")
    title: str = ""
    description: str = ""
    thumbnail: str = ""


# --- /api/resolve -----------------------------------------------------------

class Source(BaseModel):
    format_id: Optional[str] = None
    url: str
    protocol: str = Field(..., description="Playback protocol: http | hls | dash")
    ext: Optional[str] = None
    height: Optional[int] = None
    width: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    tbr: Optional[float] = None
    filesize: Optional[int] = None
    label: str
    play_url: Optional[str] = Field(None, description="Same-origin proxied URL")


class RelatedVideo(BaseModel):
    """A video linked from the URL (a playlist/channel entry)."""
    url: str
    title: Optional[str] = None
    id: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None


class PlaylistInfo(BaseModel):
    title: Optional[str] = None
    count: int
    # "playlist": entries of the playlist/channel the URL points at.
    # "channel": a synthesized "more from this uploader" feed for a lone video.
    kind: Literal["playlist", "channel"] = "playlist"


class LinksResponse(BaseModel):
    """Fast, flat list of videos linked from a URL (no player resolution)."""
    webpage_url: str
    playlist: Optional[PlaylistInfo] = None
    related: List[RelatedVideo] = []


class ResolveResponse(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    webpage_url: str
    extractor: Optional[str] = None
    sources: List[Source]
    related: List[RelatedVideo] = []
    playlist: Optional[PlaylistInfo] = None


# --- /api/recommend ---------------------------------------------------------

class Recommendation(BaseModel):
    url: str
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    source: Literal["fts", "random"]
    score: Optional[float] = Field(None, description="bm25() rank (FTS only)")


class BasedOn(BaseModel):
    url: str
    title: str = ""
    description: str = ""
    domain: str


class RecommendStats(BaseModel):
    total_rows: int
    evicted: bool
    fts_used: bool
    target_sql: int
    target_random: int


class RecommendResponse(BaseModel):
    based_on: BasedOn
    count: int
    recommendations: List[Recommendation]
    stats: RecommendStats


# --- misc -------------------------------------------------------------------

class StatsResponse(BaseModel):
    total_rows: int
    non_video_rows: int = 0


class DiscoveredVideo(BaseModel):
    url: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None


class DiscoveredLink(BaseModel):
    """One scraped link. yt-dlp is NOT called during discovery — it runs only
    when the link is clicked. `text` is the anchor label; `image` is an image
    scraped from inside the anchor; title/description/thumbnail are filled in
    only when the link is already cached (resolved on a prior click)."""
    url: str                          # "" for non-navigable fallback anchors
    text: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail: Optional[str] = None   # yt-dlp's thumbnail, if already cached
    image: Optional[str] = None       # image scraped from inside the anchor
    cached: bool = False              # already resolved -> clicking is instant
    rich: bool = False                # wraps an image / non-text content
    chrome: bool = False              # nav/sidebar/footer link (ranked last)


class DiscoverResponse(BaseModel):
    page: str
    links_found: int
    links: List[DiscoveredLink]  # rich (image) links sorted to the front
    cached_count: int            # how many are already cached videos
    stats: StatsResponse


class HealthResponse(BaseModel):
    status: str
    js_runtime: Optional[str] = None
    js_runtime_available: bool


# --- /api/trace (remote client tracing over /ws/trace/{client_id}) ----------

class TraceClientsResponse(BaseModel):
    clients: List[str]  # client ids with a live tracing socket open right now


class TraceCommandResponse(BaseModel):
    client_id: str
    command: Literal["start", "stop"]
