"""Endpoint tests via FastAPI TestClient — no network, no live server.

The headline test is `test_index_renders_200`: it locks in the regression where
the index route 500'd because of Starlette's request-first TemplateResponse
signature change. Network-dependent yt-dlp extraction is monkeypatched.
"""
from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)


def test_index_renders_200():
    """REGRESSION: GET / must render HTML, not 500 on TemplateResponse."""
    r = client.get("/")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text
    assert "yt-dlp response" in r.text  # the metadata region is present


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "js_runtime_available" in r.json()


def test_resolve_rejects_non_http():
    # ftp:// has an explicit (non-http) scheme -> still rejected, not coerced
    assert client.get("/api/resolve", params={"url": "ftp://x"}).status_code == 400


def test_resolve_defaults_missing_scheme_to_https(monkeypatch):
    captured = {}

    async def fake_resolve(url):
        captured["url"] = url
        return {"title": "T", "webpage_url": url,
                "sources": [{"url": "u", "protocol": "http", "label": "x"}]}

    monkeypatch.setattr(main.ytdlp_service, "resolve", fake_resolve)
    r = client.get("/api/resolve", params={"url": "example.com/watch?v=1"})
    assert r.status_code == 200
    assert captured["url"] == "https://example.com/watch?v=1"


def test_proxy_rejects_non_http():
    assert client.get("/api/proxy", params={"url": "ftp://x"}).status_code == 400


def test_cache_rejects_bad_url_via_httpurl():
    # pydantic HttpUrl validation -> 422 (not our manual 400)
    assert client.post("/api/cache", json={"url": "ftp://x"}).status_code == 422


def test_links_lists_and_caches(monkeypatch):
    async def fake_links(url):
        assert url == "https://lnk.test/chan"  # scheme defaulted
        return {"webpage_url": url, "playlist": {"title": "Chan", "count": 42},
                "related": [
                    {"url": "https://lnk.test/v/1", "title": "One",
                     "thumbnail": "https://t/1.jpg"},
                    {"url": "https://lnk.test/v/2", "title": "Two"},
                ]}

    monkeypatch.setattr(main.ytdlp_service, "links", fake_links)
    out = client.get("/api/links", params={"url": "lnk.test/chan"}).json()
    assert out["playlist"]["count"] == 42
    assert [r["url"] for r in out["related"]] == \
        ["https://lnk.test/v/1", "https://lnk.test/v/2"]
    # entries were cached -> discoverable by the same-domain recommender
    rec = client.get("/api/recommend", params={
        "url": "https://lnk.test/other", "title": "One", "n": 4}).json()
    assert any(x["url"] == "https://lnk.test/v/1" for x in rec["recommendations"])


def test_recommend_best_effort_without_title():
    # No title (yt-dlp didn't recognise the page) -> still returns same-domain
    # neighbours from the cache (random fill), never an error.
    main.rec.upsert("https://besteffort.test/v/1", "Clip", "", "")
    out = client.get("/api/recommend",
                     params={"url": "https://besteffort.test/", "n": 4}).json()
    assert any(r["url"] == "https://besteffort.test/v/1"
               for r in out["recommendations"])


def test_stats_shape():
    body = client.get("/api/stats").json()
    assert isinstance(body["total_rows"], int)


def test_resolve_caches_and_proxies(monkeypatch):
    async def fake_resolve(url):
        return {
            "title": "Test Clip",
            "description": "a clip about widgets",
            "uploader": "chan",
            "duration": 12.0,
            "thumbnail": None,
            "webpage_url": url,
            "extractor": "Fake",
            "sources": [{
                "format_id": "1", "url": "https://cdn.example.com/v.mp4",
                "protocol": "http", "ext": "mp4", "height": 720, "width": 1280,
                "fps": 30.0, "vcodec": "avc1", "acodec": "mp4a", "tbr": 1000.0,
                "filesize": None, "label": "720p · HTTP · mp4",
            }],
        }

    monkeypatch.setattr(main.ytdlp_service, "resolve", fake_resolve)
    r = client.get("/api/resolve", params={"url": "https://example.com/watch?v=1"})
    assert r.status_code == 200
    data = r.json()
    # play_url is the same-origin proxied URL
    assert data["sources"][0]["play_url"].startswith("/api/proxy?url=")
    assert data["related"] == []  # single video -> no linked videos
    # the page got cached and is now discoverable for same-domain recs
    rec = client.get("/api/recommend", params={
        "url": "https://example.com/other", "title": "widgets", "n": 4,
    }).json()
    assert any("example.com/watch" in x["url"] for x in rec["recommendations"])


def test_trace_start_requires_live_socket():
    assert client.post("/api/trace/nobody-connected/start").status_code == 404


def test_trace_clients_lists_connected_and_start_pushes_control():
    with client.websocket_connect("/ws/trace/abc123") as ws:
        assert "abc123" in client.get("/api/trace/clients").json()["clients"]

        r = client.post("/api/trace/abc123/start")
        assert r.status_code == 200
        assert r.json() == {"client_id": "abc123", "command": "start"}
        assert ws.receive_json() == {"type": "control", "command": "start"}

        client.post("/api/trace/abc123/stop")
        assert ws.receive_json() == {"type": "control", "command": "stop"}

    # socket closed -> no longer a valid target
    assert "abc123" not in client.get("/api/trace/clients").json()["clients"]
    assert client.post("/api/trace/abc123/start").status_code == 404


def test_trace_socket_accepts_batched_events_without_dropping_connection():
    # A batch of client-reported events (console + app "status" events) must
    # not break the socket -- a subsequent control push still gets through.
    with client.websocket_connect("/ws/trace/log-me") as ws:
        ws.send_json({"type": "batch", "events": [
            {"type": "console", "level": "error", "message": "boom"},
            {"type": "event", "name": "status", "state": "ok", "msg": "done"},
        ]})
        client.post("/api/trace/log-me/start")
        assert ws.receive_json() == {"type": "control", "command": "start"}
