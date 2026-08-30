"""Recommender invariants: same-domain only, 3/4-1/4 split, FTS ranking,
and the p = total_rows/1000 eviction."""
import tempfile

import app.recommender as rmod
from app.recommender import Recommender


def _fresh() -> Recommender:
    return Recommender(tempfile.mktemp(suffix="-rec-test.db"))


def test_domain_extraction():
    r = _fresh()
    assert r._domain("https://www.youtube.com/watch?v=1") == "youtube.com"
    assert r._domain("https://m.youtube.com/x") == "youtube.com"
    assert r._domain("https://old.reddit.com/r/x") == "reddit.com"


def test_same_domain_only_and_excludes_current():
    r = _fresh()
    for i in range(1, 5):
        r.upsert(f"https://youtube.com/w?v={i}", f"Pasta {i}", "cooking pasta recipe")
    for i in range(3):
        r.upsert(f"https://reddit.com/p/{i}", "Pasta thread", "cooking pasta recipe")
    out = r.recommend("https://youtube.com/w?v=1", "Homemade Pasta", "pasta", n=8)
    urls = [x["url"] for x in out["recommendations"]]
    assert urls, "expected same-domain recommendations"
    assert all("reddit.com" not in u for u in urls)  # never cross domains
    assert "https://youtube.com/w?v=1" not in urls    # current page excluded
    assert out["based_on"]["domain"] == "youtube.com"


def test_split_targets_three_quarters_sql():
    out = _fresh().recommend("https://e.com/x", "t", "d", n=8)
    assert out["stats"]["target_sql"] == 6      # ceil(3*8/4)
    assert out["stats"]["target_random"] == 2   # floor(8/4)


def test_fts_ranks_relevant_first():
    r = _fresh()
    r.upsert("https://e.com/1", "Pasta Carbonara", "eggs cheese pasta")
    r.upsert("https://e.com/2", "Rocket Launch", "space orbital mechanics")
    out = r.recommend("https://e.com/seed", "Pasta recipe", "pasta", n=8)
    fts = [x for x in out["recommendations"] if x["source"] == "fts"]
    assert fts and "Pasta" in (fts[0]["title"] or "")


def test_thumbnail_round_trips():
    r = _fresh()
    r.upsert("https://e.com/1", "Pasta", "pasta", thumbnail="https://img/1.jpg")
    r.upsert("https://e.com/2", "Pasta two", "pasta")  # no thumbnail -> None
    recs = {x["url"]: x for x in
            r.recommend("https://e.com/seed", "pasta", "pasta", n=8)["recommendations"]}
    assert recs["https://e.com/1"]["thumbnail"] == "https://img/1.jpg"
    assert recs["https://e.com/2"]["thumbnail"] is None


def test_eviction_probability(monkeypatch):
    r = _fresh()
    for i in range(10):
        r.upsert(f"https://e.com/{i}", "t", "body text")

    # p = 10/1000 = 0.01; force the draw below p -> exactly one row deleted
    monkeypatch.setattr(rmod.random, "random", lambda: 0.0)
    before = r.stats()["total_rows"]
    r.recommend("https://e.com/seed", "t", "body", n=4)
    assert r.stats()["total_rows"] == before - 1

    # draw above p -> zero deleted ("one or zero")
    monkeypatch.setattr(rmod.random, "random", lambda: 0.999)
    before = r.stats()["total_rows"]
    r.recommend("https://e.com/seed", "t", "body", n=4)
    assert r.stats()["total_rows"] == before
