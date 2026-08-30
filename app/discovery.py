"""Link discovery: scrape a page with BeautifulSoup and return candidate URLs.

The orchestration (cache check + yt-dlp resolution of uncached links) lives in
the API layer; this module only fetches HTML and extracts links.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx2 as httpx
from bs4 import BeautifulSoup

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_BYTES = 4_000_000   # don't parse absurdly large pages
_FALLBACK_LIMIT = 25     # cap non-navigable (no-href) anchors we list anyway
_SKIP_PREFIXES = ("#", "javascript:", "mailto:", "tel:", "data:")
# Non-standard places sites stash the real URL when there's no plain href.
_HREF_FALLBACK_ATTRS = ("data-href", "data-url", "data-permalink",
                        "data-link", "data-href-url")

# Anchors inside these are page chrome (nav/sidebar/footer), not main content —
# we rank them last so main-content links surface first.
_CHROME_TAGS = {"nav", "header", "footer", "aside"}
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}
_CHROME_HINT = ("sidebar", "side-bar", "navbar", "site-nav", "main-nav",
                "top-nav", "masthead", "footer", "breadcrumb", "toolbar",
                "menu", "banner", "widget")


def _is_chrome(a) -> bool:
    """Heuristic: is this anchor inside nav/header/footer/sidebar chrome?"""
    for p in a.parents:
        if getattr(p, "name", None) in _CHROME_TAGS:
            return True
        getter = getattr(p, "get", None)
        if getter is None:
            continue
        if (getter("role") or "").lower() in _CHROME_ROLES:
            return True
        hay = (str(getter("id") or "") + " "
               + " ".join(getter("class") or [])).lower()
        if any(h in hay for h in _CHROME_HINT):
            return True
    return False


async def fetch_html(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(
            url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"}
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower():
            raise ValueError(f"not an HTML page (content-type: {ctype or '?'})")
        return resp.text[:_MAX_BYTES]


def _img_src(img, base_url: str) -> Optional[str]:
    """Best http(s) image URL from an <img> (handles common lazy-load attrs).

    Lazy-loaded thumbnails put the REAL image in a data-* attribute and a
    throwaway placeholder (a blank pixel, a spinner) in `src` itself, so the
    data-* attributes are checked first — falling back to `src` only when none
    of them are present. Recommendation thumbnails don't have this problem
    (yt-dlp parses the page's own metadata/API, never a placeholder <img>), but
    this DOM scrape has to see through the lazy-load markup to find the same
    real thumbnail.
    """
    src = (img.get("data-src") or img.get("data-lazy-src") or img.get("data-thumb")
           or img.get("data-original") or img.get("src"))
    if not src and img.get("srcset"):
        src = img["srcset"].split(",")[0].strip().split(" ")[0]
    if not src:
        return None
    src = src.strip()
    if src.lower().startswith("data:"):  # skip inline base64 blobs
        return None
    absolute = urljoin(base_url, src)
    return absolute if urlparse(absolute).scheme in ("http", "https") else None


def _resolve_href(a, base_url: str) -> Optional[str]:
    """Absolute http(s) URL for an anchor: its href, else a data-* fallback."""
    cand = (a.get("href") or "").strip()
    if not cand or cand.lower().startswith(_SKIP_PREFIXES):
        cand = None
        for attr in _HREF_FALLBACK_ATTRS:
            v = (a.get(attr) or "").strip()
            if v and not v.lower().startswith(_SKIP_PREFIXES):
                cand = v
                break
    if not cand:
        return None
    absolute = urljoin(base_url, cand).split("#", 1)[0]
    return absolute if urlparse(absolute).scheme in ("http", "https") else None


def extract_links(html: str, base_url: str) -> List[Dict[str, Optional[str]]]:
    """Links from a page, each as {url, text, image}.

    - Navigable links come from <a href>, falling back to data-* attributes for
      anchors that don't use a plain href.
    - Anchors wrapping an <img> contribute that image's URL + alt text, so
      thumbnail-grid links render richly even with no visible text.
    - As a last resort, <a> tags with no usable URL at all are still listed
      (url=""), capped at _FALLBACK_LIMIT, so nothing is silently dropped.

    Every navigable link on the page is returned (deduped); the caller is
    responsible for filtering (e.g. same-domain), sorting and limiting — so the
    number cap is applied to ranked links, not to whatever happened to appear
    first in document order.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: List[Dict[str, Optional[str]]] = []
    fallback: List[Dict[str, Optional[str]]] = []
    seen = set()
    for a in soup.find_all("a"):
        img = a.find("img")
        image = _img_src(img, base_url) if img is not None else None
        text = (a.get_text(" ", strip=True)
                or (img.get("alt").strip() if img and img.get("alt") else "")
                or (a.get("title") or "").strip()
                or (a.get("aria-label") or "").strip())
        url = _resolve_href(a, base_url)
        if url:
            if url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "text": text or None, "image": image,
                          "chrome": _is_chrome(a)})
        elif (not a.has_attr("href") and (image or text)
              and len(fallback) < _FALLBACK_LIMIT):
            # Non-conforming anchor (no href) — list it, but not navigable.
            fallback.append({"url": "", "text": text or None, "image": image,
                             "chrome": _is_chrome(a)})
    return links + fallback
