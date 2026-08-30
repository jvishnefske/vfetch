"""SQLite FTS5 recommender over cached page metadata (url, title, description).

Design
------
* Pages are cached in a single FTS5 virtual table; title/description are indexed
  (porter stemming) so similar pages can be found with a MATCH query, while url
  and created_at are stored UNINDEXED.
* recommend() returns N items: 3/4 from an FTS5 similarity query ranked by
  bm25(), 1/4 chosen at random. If the FTS query is short on hits, the shortfall
  is topped up with extra random rows so the caller still gets N.
* On every recommend() call the cache self-trims: with probability
  p = total_rows / 1000 it deletes exactly one random row (so "one or zero" per
  call). The cache therefore drifts toward a soft ceiling near ~1000 rows.

All public methods are thread-safe and intended to be called from a worker
thread (e.g. asyncio.to_thread) so the event loop never blocks on SQLite.
"""
from __future__ import annotations

import random
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# FTS5 MATCH input must be sanitised — arbitrary title/description text contains
# characters that are operators in the FTS query grammar. We keep alphanumeric
# tokens only and feed them as quoted phrases, OR-joined.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_MAX_TOKENS = 32  # cap query width; long descriptions otherwise blow up MATCH


class Recommender:
    def __init__(self, path: str):
        self.path = str(path)
        # check_same_thread=False + an explicit lock: one connection shared
        # across worker threads, serialised by us.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            # Migration: FTS5 can't ALTER ADD COLUMN, but the cache is fully
            # regenerable, so if an older schema lacks `thumbnail` just drop and
            # recreate (rows repopulate as pages are resolved).
            cols = [c[1] for c in self.conn.execute(
                "PRAGMA table_info(pages)").fetchall()]
            if cols and "thumbnail" not in cols:
                self.conn.execute("DROP TABLE pages")
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
                    title,
                    description,
                    url UNINDEXED,
                    domain UNINDEXED,
                    thumbnail UNINDEXED,
                    created_at UNINDEXED,
                    tokenize = 'porter unicode61'
                )
                """
            )
            # Negative cache: links yt-dlp confirmed are NOT videos, so link
            # discovery can skip re-fetching them.
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS non_videos "
                "(url TEXT PRIMARY KEY, checked_at REAL)"
            )
            self.conn.commit()

    # --- helpers (assume self.lock is held) ---------------------------------

    def _count(self) -> int:
        return self.conn.execute("SELECT count(*) AS c FROM pages").fetchone()["c"]

    def _maybe_evict(self) -> bool:
        """Delete one random row with probability total_rows/1000, else none."""
        total = self._count()
        if total == 0:
            return False
        if random.random() < total / 1000.0:
            self.conn.execute(
                "DELETE FROM pages WHERE rowid IN "
                "(SELECT rowid FROM pages ORDER BY random() LIMIT 1)"
            )
            return True
        return False

    @staticmethod
    def _domain(url: str) -> str:
        """Registrable-domain approximation: host's last two labels, lowercased.

        www.youtube.com / m.youtube.com -> youtube.com, so recommendations stay
        within the same site and we never cross e.g. reddit.com <-> youtube.com.
        (Naive for multi-part TLDs like .co.uk, but good enough for grouping.)
        """
        host = (urlparse(url).hostname or "").lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    @staticmethod
    def _match_query(text: str) -> Optional[str]:
        seen: List[str] = []
        for tok in _TOKEN_RE.findall(text.lower()):
            if len(tok) >= 2 and tok not in seen:
                seen.append(tok)
            if len(seen) >= _MAX_TOKENS:
                break
        if not seen:
            return None
        return " OR ".join(f'"{tok}"' for tok in seen)

    # --- public API ----------------------------------------------------------

    def upsert(self, url: str, title: str = "", description: str = "",
               thumbnail: str = "") -> None:
        """Insert/replace a cached page keyed by url (FTS5 has no UNIQUE)."""
        with self.lock:
            self.conn.execute("DELETE FROM pages WHERE url = ?", (url,))
            self.conn.execute(
                "INSERT INTO pages(title, description, url, domain, thumbnail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title or "", description or "", url, self._domain(url),
                 thumbnail or "", str(time.time())),
            )
            self.conn.commit()

    def stats(self) -> Dict[str, Any]:
        with self.lock:
            non_video = self.conn.execute(
                "SELECT count(*) AS c FROM non_videos").fetchone()["c"]
            return {"total_rows": self._count(), "non_video_rows": non_video}

    def _members(self, table: str, urls: List[str]) -> set:
        """URLs from `urls` present in `table` (chunked under SQLite's var cap)."""
        unique = list(dict.fromkeys(urls))
        found: set = set()
        if not unique:
            return found
        with self.lock:
            for i in range(0, len(unique), 900):
                chunk = unique[i:i + 900]
                ph = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    f"SELECT url FROM {table} WHERE url IN ({ph})", chunk
                ).fetchall()
                found.update(r["url"] for r in rows)
        return found

    def cached(self, urls: List[str]) -> set:
        """Which of `urls` are already cached as videos."""
        return self._members("pages", urls)

    def known_non_video(self, urls: List[str]) -> set:
        """Which of `urls` are known (from a prior check) to not be videos."""
        return self._members("non_videos", urls)

    def meta(self, urls: List[str]) -> Dict[str, Dict[str, Any]]:
        """Cached {title, thumbnail} for whichever of `urls` are cached videos."""
        unique = list(dict.fromkeys(urls))
        out: Dict[str, Dict[str, Any]] = {}
        if not unique:
            return out
        with self.lock:
            for i in range(0, len(unique), 900):
                chunk = unique[i:i + 900]
                ph = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    "SELECT url, title, description, thumbnail FROM pages "
                    f"WHERE url IN ({ph})", chunk,
                ).fetchall()
                for r in rows:
                    out[r["url"]] = {"title": r["title"] or None,
                                     "description": r["description"] or None,
                                     "thumbnail": r["thumbnail"] or None}
        return out

    def mark_non_video(self, url: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO non_videos(url, checked_at) VALUES (?, ?)",
                (url, time.time()),
            )
            self.conn.commit()

    def recommend(
        self,
        url: str,
        title: str = "",
        description: str = "",
        n: int = 8,
    ) -> Dict[str, Any]:
        n = max(1, min(int(n), 50))
        target_sql = (3 * n + 3) // 4   # ceil(3n/4)
        target_random = n - target_sql  # floor(n/4)
        match_q = self._match_query(" ".join(filter(None, [title, description])))
        domain = self._domain(url)  # recommendations are confined to this domain

        with self.lock:
            evicted = self._maybe_evict()
            if evicted:
                self.conn.commit()

            recs: List[Dict[str, Any]] = []
            seen = {url}

            # 3/4: FTS5 similarity, best bm25() first (smaller == more relevant).
            # Constrained to the same domain so we never cross sites.
            if match_q:
                rows = self.conn.execute(
                    "SELECT url, title, description, thumbnail, bm25(pages) AS score "
                    "FROM pages WHERE pages MATCH ? AND url <> ? AND domain = ? "
                    "ORDER BY bm25(pages) LIMIT ?",
                    (match_q, url, domain, target_sql),
                ).fetchall()
                for r in rows:
                    recs.append(
                        {
                            "url": r["url"],
                            "title": r["title"],
                            "description": r["description"],
                            "thumbnail": r["thumbnail"] or None,
                            "source": "fts",
                            "score": r["score"],
                        }
                    )
                    seen.add(r["url"])

            # 1/4 random — plus topping up any FTS shortfall so we still hit N.
            need = n - len(recs)
            if need > 0:
                placeholders = ",".join("?" * len(seen))
                rows = self.conn.execute(
                    f"SELECT url, title, description, thumbnail FROM pages "
                    f"WHERE url NOT IN ({placeholders}) AND domain = ? "
                    f"ORDER BY random() LIMIT ?",
                    (*seen, domain, need),
                ).fetchall()
                for r in rows:
                    recs.append(
                        {
                            "url": r["url"],
                            "title": r["title"],
                            "description": r["description"],
                            "thumbnail": r["thumbnail"] or None,
                            "source": "random",
                            "score": None,
                        }
                    )

            total = self._count()

        return {
            "based_on": {"url": url, "title": title, "description": description,
                         "domain": domain},
            "count": len(recs),
            "recommendations": recs,
            "stats": {
                "total_rows": total,
                "evicted": evicted,
                "fts_used": match_q is not None,
                "target_sql": target_sql,
                "target_random": target_random,
            },
        }
