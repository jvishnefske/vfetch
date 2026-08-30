"""Extraction-shaping logic, with yt-dlp's network calls stubbed out."""
import app.ytdlp_service as yt


class _FakeYDL:
    """Stand-in for YoutubeDL: returns canned info based on the opts it's given."""
    flat_result = None
    full_result = None

    def __init__(self, opts):
        self.flat = opts.get("extract_flat") == "in_playlist"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        # The flat pass returns the playlist shell; later passes return a video.
        return _FakeYDL.flat_result if self.flat else _FakeYDL.full_result


def _video(**kw):
    base = {
        "title": "Vid", "description": "", "webpage_url": kw.get("url", "u"),
        "extractor_key": "Fake", "formats": [{
            "url": "https://cdn/x.mp4", "protocol": "https", "ext": "mp4",
            "vcodec": "avc1", "acodec": "mp4a", "height": 720,
        }],
    }
    base.update(kw)
    return base


def test_playlist_surfaces_linked_videos(monkeypatch):
    _FakeYDL.flat_result = {
        "_type": "playlist", "title": "My List",
        "entries": [
            {"url": "https://site/v/1", "title": "One", "id": "1", "duration": 60},
            {"url": "https://site/v/2", "title": "Two", "id": "2", "duration": 90},
            None,  # yt-dlp sometimes yields None entries; must be skipped
        ],
    }
    _FakeYDL.full_result = _video(url="https://site/v/1", title="One")
    monkeypatch.setattr(yt, "YoutubeDL", _FakeYDL)

    out = yt._extract_sync("https://site/playlist")
    assert out["playlist"] == {"title": "My List", "count": 2}
    assert [r["url"] for r in out["related"]] == ["https://site/v/1", "https://site/v/2"]
    assert out["related"][0]["title"] == "One"
    assert out["sources"], "first entry should still resolve to a playable source"


def test_links_sync_is_flat_only(monkeypatch):
    _FakeYDL.flat_result = {
        "_type": "playlist", "title": "Chan", "playlist_count": 100,
        "entries": [
            {"url": "https://s/1", "title": "A"},
            {"url": "https://s/2", "title": "B"},
            None,
        ],
    }
    _FakeYDL.full_result = None  # _links_sync must never need a full resolve
    monkeypatch.setattr(yt, "YoutubeDL", _FakeYDL)

    out = yt._links_sync("https://s/chan")
    assert out["playlist"] == {"title": "Chan", "count": 100, "kind": "playlist"}
    assert [r["url"] for r in out["related"]] == ["https://s/1", "https://s/2"]


def test_links_sync_falls_back_to_channel(monkeypatch):
    """A standalone video has no playlist entries -> surface the uploader's
    channel as a 'more from' feed, excluding the current video."""
    video = _video(url="https://yt/watch?v=abc", title="Solo")
    video.update({"id": "abc", "_type": None, "channel_url": "https://yt/chan",
                  "uploader": "Cool Channel"})
    channel = {
        "_type": "playlist", "title": "Cool Channel - Videos",
        "entries": [
            {"url": "https://yt/watch?v=abc", "title": "Solo", "id": "abc"},  # self
            {"url": "https://yt/watch?v=d2", "title": "Two", "id": "d2"},
            {"url": "https://yt/watch?v=d3", "title": "Three", "id": "d3"},
        ],
    }

    class _ByUrl(_FakeYDL):
        def extract_info(self, url, download=False):
            return channel if "chan" in url else video

    monkeypatch.setattr(yt, "YoutubeDL", _ByUrl)

    out = yt._links_sync("https://yt/watch?v=abc")
    assert out["playlist"] == {"title": "More from Cool Channel", "count": 2,
                               "kind": "channel"}
    # current video excluded; only the other two channel entries remain
    assert [r["url"] for r in out["related"]] == \
        ["https://yt/watch?v=d2", "https://yt/watch?v=d3"]


def test_single_video_has_no_related(monkeypatch):
    _FakeYDL.flat_result = _video(url="https://site/watch", title="Solo")
    _FakeYDL.full_result = _FakeYDL.flat_result
    monkeypatch.setattr(yt, "YoutubeDL", _FakeYDL)

    out = yt._extract_sync("https://site/watch")
    assert out["related"] == []
    assert out["playlist"] is None
    assert out["sources"]
