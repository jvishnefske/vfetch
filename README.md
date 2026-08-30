# xfetch

A FastAPI backend that uses **yt-dlp** to resolve playable stream URLs and plays
them in the browser with a plain HTML5 `<video>` tag — falling back to
**hls.js** for HLS/`m3u8` adaptive streams.

## How it works

```
browser ──/api/resolve──▶ FastAPI ──▶ yt-dlp (+ JS runtime) ──▶ stream metadata
   │                                                                  │
   └──◀ list of formats (progressive + HLS), each with a play_url ◀───┘
   │
   └──/api/proxy──▶ FastAPI streams the media back same-origin (Range-aware,
                    rewrites m3u8 manifests so segments/keys also route here)
```

- **Progressive** (`http`) formats are set directly as `<video src>`.
- **HLS** (`m3u8`) formats are loaded through **hls.js** (or native HLS on Safari).
- All playback flows through `/api/proxy` so the browser never trips over
  cross-origin (CORS) or hotlink restrictions, and HLS manifests are rewritten
  so child playlists, segments, and `EXT-X-KEY`/`EXT-X-MAP` URIs stay same-origin.

## Server-side JavaScript runtime

Some extractors (YouTube especially) require a **JS runtime on the server** to
solve signature / n-sig challenges. yt-dlp auto-detects one — **Deno** is
preferred, **Node** works too. Without it, those sites may fail to resolve.

```bash
# Deno (recommended by yt-dlp)
curl -fsSL https://deno.land/install.sh | sh
# …or Node (already common on dev machines)
```

Check what was detected at `GET /api/health` or in the footer of the UI.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

## Develop & test

Dependencies and metadata live in `pyproject.toml`; the `requirements*.txt` files
are thin pointers (`-e .` / `-e .[dev]`) so there's a single source of truth.

```bash
pip install -e ".[dev]"   # editable install + pytest
pytest                    # config in pyproject ([tool.pytest.ini_options])
```

`tests/` covers the API (incl. the `GET /` TemplateResponse regression) and the
recommender invariants (same-domain only, 3/4-1/4 split, FTS ranking, eviction).
The deploy hook on rainier runs the suite on every push and **refuses to restart
the service if tests fail** — a red push leaves the previous working code running.

## Recommendations (SQLite FTS5)

Resolved pages are cached (url, title, description, domain) in a SQLite **FTS5**
table. The recommender returns items **from the same domain only** — it will
never suggest `reddit.com` for a `youtube.com` page — split:

- **~3/4** from an FTS5 similarity query over title+description, ranked by `bm25()`
- **~1/4** chosen at random (also tops up any FTS shortfall to reach `n`)

On every `/api/recommend` call the cache self-trims: with probability
`p = total_rows / 1000` it deletes **one** random row (else none), so the cache
drifts toward a soft ceiling near ~1000 rows.

```
GET  /api/resolve?url=…        also caches the page automatically
POST /api/cache  {url,title,description}    add/refresh a page
GET  /api/recommend?url=…&title=…&description=…&n=8
GET  /api/stats                {"total_rows": N}
```

`title`/`description` on `/api/recommend` are optional but sharpen matching; pass
the current page's metadata (the player UI does this and shows a "Related" list).
DB location is `$XFETCH_DB` (defaults to `./xfetch.db`).

## Remote client tracing

Every open tab connects an [RxJS](https://rxjs.dev) `webSocket()` to
`/ws/trace/{client_id}` on load and stays subscribed for the life of the page.
The server can flip that tab into tracing mode at any time — no reload needed —
and the browser streams its console output and app status events back over the
same socket into this process's normal log stream.

```
GET  /api/trace/clients        -> {"clients": [...]}  ids with a live socket right now
POST /api/trace/{id}/start     -> pushes {"type":"control","command":"start"} down the socket
POST /api/trace/{id}/stop      -> same, with "stop"
```

`{id}` is a UUID the page generates once and keeps in `localStorage`
(`xfetch_trace_id`); it's shown truncated in the header's "trace" chip so it
can be read off and handed to whoever is debugging. A tab can also self-start
by loading it with `?trace=1`, without waiting on a server push. Traced
console/status events are batched (250ms) client-side before being sent, so a
burst of activity is one WS frame, not one per line.

## Deploy

### Nix

The flake exposes a package, a runnable app, a dev shell, and a NixOS module.
**Deno is bundled** into the package, so yt-dlp always has a JS runtime.

```bash
nix run .                       # run it (honours $XFETCH_HOST / $XFETCH_PORT)
nix build .                     # ./result/bin/xfetch
nix develop                     # dev shell with all deps + deno
```

NixOS — add the module and enable the service:

```nix
{
  inputs.xfetch.url = "github:you/xfetch";   # or "path:/opt/xfetch"

  outputs = { self, nixpkgs, xfetch }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        xfetch.nixosModules.default
        { services.xfetch = { enable = true; port = 8000; openFirewall = false; }; }
      ];
    };
  };
}
```

The module runs a hardened `systemd` service under a `DynamicUser` with caches
under `/var/cache/xfetch`. (If this dir isn't a git repo yet, `git init` so the
flake can see the files.)

### systemd (pip / venv, no Nix)

`deploy/xfetch.service` is a hardened unit for a plain venv install; the header
comment has the full step-by-step. Configure via `deploy/xfetch.env.example`
copied to `/etc/xfetch.env`. Remember to install Deno or Node so yt-dlp can
solve signature challenges.

```bash
sudo cp deploy/xfetch.service /etc/systemd/system/
sudo systemctl enable --now xfetch
```

## Notes

- yt-dlp is pinned loosely; keep it fresh (`pip install -U yt-dlp`) since
  extractors break often.
- The proxy is intentionally open to any http(s) URL for local use. Before
  exposing this publicly, add an allow-list / auth, since it's an open relay.
- DASH-only streams (`http_dash_segments`) and audio-only formats are filtered
  out — a single `<video>` tag can't mux separate video/audio tracks. Add
  dash.js and a similar rewrite if you need DASH.
