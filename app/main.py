"""FastAPI backend that resolves stream URLs with yt-dlp and plays them in the
browser via a plain <video> tag (progressive) or hls.js (HLS / m3u8).

Routes
------
GET /                     -> the player UI
GET /api/health           -> runtime / dependency status
GET /api/resolve?url=...  -> JSON: title + list of playable sources
GET /api/proxy?url=...    -> streams remote media through this server so the
                             browser never hits cross-origin/CORS or hotlink
                             restrictions. HLS manifests are rewritten so their
                             child playlists, segments and keys also route here.

WS  /ws/trace/{client_id}    the browser opens this on load and stays subscribed.
POST /api/trace/{id}/start   pushes a control message down that socket telling
POST /api/trace/{id}/stop    the browser to start/stop streaming console + app
                             trace events back over the same connection.
GET /api/trace/clients       -> ids with a live tracing socket right now
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx2 as httpx
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import discovery, models, recommender, ytdlp_service

logger = logging.getLogger("xfetch")
if not logger.handlers:  # uvicorn doesn't configure the root logger; do our own
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     [xfetch] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="xfetch", description="yt-dlp powered web video player")

# Recommender cache (SQLite FTS5). Override location with $XFETCH_DB.
DB_PATH = os.environ.get("XFETCH_DB", str(BASE_DIR.parent / "xfetch.db"))
rec = recommender.Recommender(DB_PATH)

# client_id -> live websocket. Single-process, in-memory: enough to let an
# operator push a start/stop trace command at a specific open browser tab.
_trace_sockets: dict[str, WebSocket] = {}

# Headers some CDNs require; forwarded on proxied requests.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Manifests and key files are small; everything else is streamed.
_MANIFEST_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)
_MANIFEST_EXTS = (".m3u8", ".m3u")


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _normalize_url(url: str) -> str:
    """Default a missing scheme to https:// so users can omit it. A URL that
    already carries a (non-http) scheme is left as-is for the validators."""
    url = url.strip()
    if url and not _SCHEME_RE.match(url):
        url = "https://" + url
    return url


def _proxy_url(target: str) -> str:
    return "/api/proxy?url=" + quote(target, safe="")


def _looks_like_manifest(url: str, content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _MANIFEST_CONTENT_TYPES:
        return True
    path = urlparse(url).path.lower()
    return path.endswith(_MANIFEST_EXTS)


def _rewrite_m3u8(body: str, base_url: str) -> str:
    """Rewrite every URI in an m3u8 playlist to go back through our proxy so
    nested playlists, media segments and EXT-X-KEY/MAP all stay same-origin."""
    out_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            # Tag lines may embed a URI="..." attribute (KEY, MAP, MEDIA, ...).
            if 'URI="' in stripped:
                pre, rest = stripped.split('URI="', 1)
                uri, post = rest.split('"', 1)
                absolute = urljoin(base_url, uri)
                line = f'{pre}URI="{_proxy_url(absolute)}"{post}'
            out_lines.append(line)
            continue
        # A bare URI line (segment or variant playlist).
        absolute = urljoin(base_url, stripped)
        out_lines.append(_proxy_url(absolute))
    return "\n".join(out_lines)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # Starlette's current API is request-first: (request, name, context).
    return templates.TemplateResponse(
        request,
        "index.html",
        {"js_runtime": ytdlp_service.detect_js_runtime()},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    # Browsers request this unprompted; answer 204 so it doesn't log as a 404.
    return Response(status_code=204)


@app.get("/api/health", response_model=models.HealthResponse)
async def health() -> dict:
    runtime = ytdlp_service.detect_js_runtime()
    return {
        "status": "ok",
        "js_runtime": runtime,
        "js_runtime_available": runtime is not None,
    }


@app.get("/api/resolve", response_model=models.ResolveResponse)
async def resolve(url: str = Query(..., description="Page URL to resolve")) -> dict:
    url = _normalize_url(url)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        data = await ytdlp_service.resolve(url)
    except Exception as exc:  # yt-dlp raises a wide variety of error types
        status = ytdlp_service.status_for_error(exc)
        logger.info("resolve %s -> %d: %s", url, status, exc)
        raise HTTPException(status_code=status, detail=f"yt-dlp failed: {exc}") from exc
    if not data.get("sources"):
        raise HTTPException(
            status_code=422,
            detail="no formats playable in a single <video>/hls.js tag were found",
        )
    # Cache this page's metadata so it can feed / appear in recommendations.
    await asyncio.to_thread(
        rec.upsert,
        data.get("webpage_url") or url,
        data.get("title") or "",
        data.get("description") or "",
        data.get("thumbnail") or "",
    )
    # Cache linked (playlist/channel) videos too — they give the same-domain
    # recommender real neighbours to suggest (with their thumbnails).
    for item in data.get("related", []):
        if item.get("url"):
            await asyncio.to_thread(
                rec.upsert, item["url"], item.get("title") or "", "",
                item.get("thumbnail") or "",
            )
    # Hand the client proxied URLs so playback is always same-origin.
    for src in data["sources"]:
        src["play_url"] = _proxy_url(src["url"])
    return data


@app.post("/api/cache", response_model=models.StatsResponse)
async def cache_page(item: models.CacheItem) -> dict:
    """Manually add/refresh a page in the recommendation cache.

    `url` is validated (and normalised) by pydantic's HttpUrl, so no manual
    scheme check is needed here.
    """
    await asyncio.to_thread(
        rec.upsert, str(item.url), item.title, item.description, item.thumbnail
    )
    return await asyncio.to_thread(rec.stats)


@app.get("/api/recommend", response_model=models.RecommendResponse)
async def recommend(
    url: str = Query(..., description="The current page URL"),
    title: str = Query("", description="Current page title (improves matching)"),
    description: str = Query("", description="Current page description"),
    n: int = Query(8, ge=1, le=50, description="How many recommendations"),
) -> dict:
    """Recommend pages from the same domain: ~3/4 via FTS5 similarity, ~1/4
    random. Also probabilistically evicts one cached row (p = rows/1000)."""
    url = _normalize_url(url)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    return await asyncio.to_thread(rec.recommend, url, title, description, n)


@app.get("/api/links", response_model=models.LinksResponse)
async def links(url: str = Query(..., description="Page URL to list linked videos")) -> dict:
    """Fast, flat list of videos linked from the URL (playlist/channel entries),
    with no player resolution — so the UI can show links without waiting on the
    slower /api/resolve. Linked entries are cached for the recommender."""
    url = _normalize_url(url)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        data = await ytdlp_service.links(url)
    except Exception as exc:
        status = ytdlp_service.status_for_error(exc)
        logger.info("links %s -> %d: %s", url, status, exc)
        raise HTTPException(status_code=status, detail=f"yt-dlp failed: {exc}") from exc
    for item in data.get("related", []):
        if item.get("url"):
            await asyncio.to_thread(
                rec.upsert, item["url"], item.get("title") or "", "",
                item.get("thumbnail") or "",
            )
    return data


@app.get("/api/stats", response_model=models.StatsResponse)
async def stats() -> dict:
    return await asyncio.to_thread(rec.stats)


@app.get("/api/discover", response_model=models.DiscoverResponse)
async def discover(
    url: str = Query(..., description="Page URL to scrape for links"),
    same_domain: bool = Query(False, description="Only keep same-domain links"),
    limit: int = Query(200, ge=1, le=1000, description="Max links to return"),
) -> dict:
    """Scrape a page (BeautifulSoup) for links and return them WITHOUT calling
    yt-dlp — resolution happens only when a link is clicked. Already-cached links
    are annotated with their stored title/description/thumbnail; image-wrapping
    (rich) links are sorted to the front. Links are parsed, filtered and ranked
    over the whole page first; the `limit` is applied only to the ranked result,
    so good main-content links are never dropped by a document-order cap."""
    url = _normalize_url(url)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        html = await discovery.fetch_html(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"fetch failed: {exc}") from exc

    scraped = discovery.extract_links(html, url)
    if same_domain:
        d = rec._domain(url)
        scraped = [s for s in scraped if s["url"] and rec._domain(s["url"]) == d]

    urls = [s["url"] for s in scraped if s["url"]]
    cached_meta = await asyncio.to_thread(rec.meta, urls)

    out_links = []
    for s in scraped:
        meta = cached_meta.get(s["url"]) if s["url"] else None
        out_links.append({
            "url": s["url"],
            "text": s.get("text"),
            "title": (meta or {}).get("title"),
            "description": (meta or {}).get("description"),
            "thumbnail": (meta or {}).get("thumbnail"),
            "image": s.get("image"),
            "cached": bool(meta),
            "rich": bool(s.get("image")),
            "chrome": bool(s.get("chrome")),
        })
    # Main-content links first (chrome/nav/sidebar last); within each, rich
    # (image) links lead. Stable, so document order breaks further ties.
    out_links.sort(key=lambda l: (l["chrome"], not l["rich"]))

    chrome_n = sum(1 for l in out_links if l["chrome"])
    rich_n = sum(1 for l in out_links if l["rich"])
    no_label = sum(1 for l in out_links if not (l["title"] or l["text"]))
    cached_n = sum(1 for l in out_links if l["cached"])
    total = len(out_links)
    logger.info(
        "discover %s: %d links (same_domain=%s) rich=%d chrome=%d cached=%d "
        "no_label=%d -> returning %d", url, total, same_domain, rich_n,
        chrome_n, cached_n, no_label, min(total, limit),
    )
    logger.info("discover top labels: %s",
                [(l["title"] or l["text"] or "∅")[:32] for l in out_links[:8]])

    # Limit only after ranking, so the cap keeps the best links, not the first.
    out_links = out_links[:limit]

    return {
        "page": url,
        "links_found": total,
        "links": out_links,
        "cached_count": cached_n,
        "stats": await asyncio.to_thread(rec.stats),
    }


@app.get("/api/proxy")
async def proxy(request: Request, url: str = Query(...)) -> Response:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")

    fwd_headers = {
        "User-Agent": request.headers.get("user-agent", _DEFAULT_UA),
        "Accept": "*/*",
    }
    # Forward the browser's Range header so <video> seeking / partial fetches work.
    if "range" in request.headers:
        fwd_headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0))
    try:
        upstream = await client.send(
            client.build_request("GET", url, headers=fwd_headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    content_type = upstream.headers.get("content-type", "")

    # Manifests are tiny and must be rewritten — buffer, rewrite, return whole.
    if _looks_like_manifest(url, content_type):
        try:
            body_bytes = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        rewritten = _rewrite_m3u8(body_bytes.decode("utf-8", "replace"), url)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    # Everything else (segments, progressive media): stream straight through.
    passthrough = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        if h in upstream.headers:
            passthrough[h] = upstream.headers[h]
    passthrough["Cache-Control"] = "no-store"

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        media_type=content_type or "application/octet-stream",
        headers=passthrough,
    )


# --- remote client tracing ---------------------------------------------------
# The browser (see index.html's `traceSocket`, an RxJS WebSocketSubject) opens
# /ws/trace/{client_id} on load and stays subscribed for the life of the tab.
# It is idle until told otherwise: /api/trace/{id}/start pushes a control
# message down that same socket, the page starts mirroring console output and
# its own status-line events back over it, and this process logs each one.

@app.websocket("/ws/trace/{client_id}")
async def trace_socket(websocket: WebSocket, client_id: str) -> None:
    await websocket.accept()
    _trace_sockets[client_id] = websocket
    logger.info("trace: %s connected (%d live)", client_id, len(_trace_sockets))
    try:
        while True:
            msg = await websocket.receive_json()
            _log_trace_message(client_id, msg)
    except WebSocketDisconnect:
        pass
    finally:
        if _trace_sockets.get(client_id) is websocket:
            del _trace_sockets[client_id]
        logger.info("trace: %s disconnected (%d live)", client_id, len(_trace_sockets))


def _log_trace_message(client_id: str, msg: dict) -> None:
    if msg.get("type") == "batch":
        for evt in msg.get("events", []):
            _log_trace_event(client_id, evt)
    else:
        _log_trace_event(client_id, msg)


def _log_trace_event(client_id: str, evt: dict) -> None:
    if evt.get("type") == "console":
        logger.info("trace[%s] console.%s: %s", client_id,
                     evt.get("level", "log"), evt.get("message", ""))
    else:
        extra = {k: v for k, v in evt.items() if k not in ("type", "name", "ts")}
        logger.info("trace[%s] %s: %s", client_id, evt.get("name") or evt.get("type", "event"), extra)


@app.get("/api/trace/clients", response_model=models.TraceClientsResponse)
async def trace_clients() -> dict:
    """Client ids with a live tracing socket open right now — valid targets
    for /api/trace/{id}/start."""
    return {"clients": sorted(_trace_sockets)}


@app.post("/api/trace/{client_id}/start", response_model=models.TraceCommandResponse)
async def trace_start(client_id: str) -> dict:
    return await _send_trace_command(client_id, "start")


@app.post("/api/trace/{client_id}/stop", response_model=models.TraceCommandResponse)
async def trace_stop(client_id: str) -> dict:
    return await _send_trace_command(client_id, "stop")


async def _send_trace_command(client_id: str, command: str) -> dict:
    socket = _trace_sockets.get(client_id)
    if socket is None:
        raise HTTPException(
            status_code=404, detail=f"no live trace socket for client {client_id!r}"
        )
    await socket.send_json({"type": "control", "command": command})
    logger.info("trace: sent %s to %s", command, client_id)
    return {"client_id": client_id, "command": command}
