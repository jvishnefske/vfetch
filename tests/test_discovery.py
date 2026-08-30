"""bs4 link extraction + the /api/discover flow (yt-dlp is NOT called)."""
from app import discovery, main
from tests.test_app import client


def test_extract_links_absolutizes_dedupes_and_falls_back():
    html = """
      <a href="/v/1">one</a>
      <a href="https://other.com/v/2">two</a>
      <a href="/v/1#frag">dup</a>
      <a href="mailto:a@b.com">mail</a>
      <a href="javascript:void(0)">js</a>
      <a href="page.html">rel</a>
      <a href="/v/4"><img src="/thumbs/4.jpg" alt="Four"></a>
      <a data-href="/v/5">data-href fallback</a>
      <a><img src="/t/x.jpg" alt="No href"></a>
    """
    links = discovery.extract_links(html, "https://site.com/dir/index")
    assert links == [
        {"url": "https://site.com/v/1", "text": "one", "image": None, "chrome": False},
        {"url": "https://other.com/v/2", "text": "two", "image": None, "chrome": False},
        {"url": "https://site.com/dir/page.html", "text": "rel", "image": None,
         "chrome": False},
        # image-wrapping anchor: alt text + scraped image, even with no link text
        {"url": "https://site.com/v/4", "text": "Four",
         "image": "https://site.com/thumbs/4.jpg", "chrome": False},
        # url recovered from a data-* attribute (non-conforming anchor)
        {"url": "https://site.com/v/5", "text": "data-href fallback", "image": None,
         "chrome": False},
        # anchor with NO href at all -> listed as a non-navigable fallback (url="")
        {"url": "", "text": "No href", "image": "https://site.com/t/x.jpg",
         "chrome": False},
    ]


def test_extract_links_prefers_lazy_load_src_over_placeholder():
    # Real-world pattern: `src` is a blank-pixel/spinner placeholder, the real
    # thumbnail only shows up once JS lazy-loads it into a data-* attribute.
    # Scraping (unlike yt-dlp, which reads the site's own metadata/API) has to
    # know to prefer that data-* attribute over the placeholder `src`.
    html = '<a href="/v/1"><img src="/img/blank.gif" data-src="/thumbs/real.jpg"></a>'
    links = discovery.extract_links(html, "https://site.com/")
    assert links[0]["image"] == "https://site.com/thumbs/real.jpg"


def test_discover_same_domain_filter(monkeypatch):
    html = ('<a href="https://site.test/a">a</a>'
            '<a href="https://other.test/b">b</a>'
            '<a href="/c">c</a>'
            '<a href="https://www.site.test/d">d</a>')   # subdomain -> same domain

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(main.discovery, "fetch_html", fake_fetch)
    out = client.get("/api/discover", params={
        "url": "https://site.test/page", "same_domain": "true"}).json()
    urls = {l["url"] for l in out["links"]}
    assert "https://site.test/a" in urls
    assert "https://site.test/c" in urls
    assert "https://www.site.test/d" in urls       # www. collapses to site.test
    assert "https://other.test/b" not in urls       # cross-domain dropped


def test_chrome_links_flagged_and_ranked_last(monkeypatch):
    html = (
        '<nav><a href="https://s.test/home">Home</a></nav>'
        '<main><a href="https://s.test/article">Read this</a></main>'
        '<div class="sidebar"><a href="https://s.test/side">Sidebar</a></div>'
        '<footer><a href="https://s.test/about">About</a></footer>'
    )

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(main.discovery, "fetch_html", fake_fetch)
    out = client.get("/api/discover", params={"url": "https://s.test/page"}).json()
    by = {l["url"]: l for l in out["links"]}

    assert by["https://s.test/home"]["chrome"] is True      # <nav>
    assert by["https://s.test/side"]["chrome"] is True      # class="sidebar"
    assert by["https://s.test/about"]["chrome"] is True     # <footer>
    assert by["https://s.test/article"]["chrome"] is False  # <main>
    # main-content link ranks ahead of all the chrome links
    assert out["links"][0]["url"] == "https://s.test/article"


def test_discover_scrapes_without_calling_ytdlp(monkeypatch):
    html = (
        '<a href="https://disco.test/text">just text</a>'
        '<a href="https://disco.test/grid"><img src="/t/g.jpg" alt="Grid"></a>'
        '<a href="https://disco.test/cached">cached one</a>'
    )

    async def fake_fetch(url):
        return html

    async def boom(url):  # discovery must never resolve with yt-dlp
        raise AssertionError("yt-dlp must not run during discover")

    monkeypatch.setattr(main.discovery, "fetch_html", fake_fetch)
    monkeypatch.setattr(main.ytdlp_service, "resolve", boom)
    # a link that was resolved on a prior click is in the cache already
    main.rec.upsert("https://disco.test/cached", "Cached Title", "desc here",
                    "https://t/c.jpg")

    out = client.get("/api/discover", params={"url": "https://disco.test/page"}).json()
    by = {l["url"]: l for l in out["links"]}

    assert out["links_found"] == 3
    assert out["cached_count"] == 1
    # rich (image-wrapping) link is sorted to the front
    assert out["links"][0]["url"] == "https://disco.test/grid"
    assert by["https://disco.test/grid"]["rich"] is True
    assert by["https://disco.test/grid"]["image"] == "https://disco.test/t/g.jpg"
    assert by["https://disco.test/grid"]["text"] == "Grid"
    # cached link is annotated from the cache (clicking would be instant)
    assert by["https://disco.test/cached"]["cached"] is True
    assert by["https://disco.test/cached"]["title"] == "Cached Title"
    assert by["https://disco.test/cached"]["thumbnail"] == "https://t/c.jpg"
    # plain text link: not rich, not cached, no thumbnail
    assert by["https://disco.test/text"]["rich"] is False
    assert by["https://disco.test/text"]["cached"] is False
    assert by["https://disco.test/text"]["thumbnail"] is None
