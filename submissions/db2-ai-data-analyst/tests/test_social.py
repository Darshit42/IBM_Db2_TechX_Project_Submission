"""Social posts: labelling, entity linking, seed storylines, connectors (mocked HTTP), Db2 search."""
import datetime as dt
import json

import httpx
import pytest

from analyst import seed_social
from analyst.social import SocialFilter, guess_topic, link_entities, score_sentiment
from analyst.social_connectors import ConnectorError, fetch_reddit, fetch_x, load_file

PRODUCTS = [
    {"PRODUCT_ID": 1, "PRODUCT_NAME": "Kestrel Smartwatche Pro", "CATEGORY": "Electronics",
     "SUBCATEGORY": "Smartwatches", "BRAND": "Kestrel"},
    {"PRODUCT_ID": 2, "PRODUCT_NAME": "Saffron Footwear Go", "CATEGORY": "Fashion",
     "SUBCATEGORY": "Footwear", "BRAND": "Saffron"},
    {"PRODUCT_ID": 3, "PRODUCT_NAME": "Hearthly Cookware Max", "CATEGORY": "Home & Kitchen",
     "SUBCATEGORY": "Cookware", "BRAND": "Hearthly"},
]
STORES = [{"STORE_ID": 106, "CITY": "Mathura", "CHANNEL": "STORE"},
          {"STORE_ID": 101, "CITY": "Delhi", "CHANNEL": "STORE"},
          {"STORE_ID": 900, "CITY": "Online", "CHANNEL": "ONLINE"}]


def test_sentiment_and_topic():
    assert score_sentiment("Strap snapped, defective, returned it") < -0.5
    assert score_sentiment("Love it, great quality, recommend") > 0.5
    assert score_sentiment("It is a watch") == 0.0
    assert guess_topic("courier never delivered my package") == "DELIVERY"
    assert guess_topic("size runs small and tight") == "SIZING"
    assert guess_topic("nothing specific", sentiment=0.8) == "PRAISE"


def test_link_entities_prefers_product_then_brand_then_store():
    assert link_entities("my kestrel smartwatche pro died", PRODUCTS, STORES) == (1, None, "Kestrel")
    assert link_entities("Saffron shoes run small", PRODUCTS, STORES) == (None, None, "Saffron")
    assert link_entities("long queue at the Mathura store", PRODUCTS, STORES) == (None, 106, None)


def test_filter_clauses_are_parameterised():
    where, params = SocialFilter("x", date_from="2026-03-01", date_to="2026-07-31T00:00",
                                 subcategory="Smartwatches", platform="reddit").clauses()
    assert where == ["sp.POST_DATE >= ?", "sp.POST_DATE <= ?", "UPPER(p.SUBCATEGORY) LIKE UPPER(?)", "sp.PLATFORM = ?"]
    assert params == ["2026-03-01", "2026-07-31", "Smartwatche%", "REDDIT"]
    with pytest.raises(ValueError):
        SocialFilter("x", date_from="last spring").clauses()


def test_seed_posts_follow_the_sales_storylines():
    posts = seed_social.generate(PRODUCTS, STORES)
    assert posts == seed_social.generate(PRODUCTS, STORES)  # deterministic
    assert len({p.external_id for p in posts}) == len(posts) > 1000
    kestrel_neg = [p for p in posts if p.product_id == 1 and p.topic == "QUALITY" and p.sentiment < 0]
    in_wave = [p for p in kestrel_neg if dt.date(2026, 3, 1) <= p.posted_at.date() <= dt.date(2026, 7, 31)]
    assert len(in_wave) > 0.9 * len(kestrel_neg)
    mathura = [p for p in posts if p.store_id == 106]
    assert min(p.posted_at.date() for p in mathura) >= dt.date(2025, 6, 15)
    assert all(-1 <= p.sentiment <= 1 for p in posts)
    assert {p.platform for p in posts} == {"REDDIT", "X", "REVIEW"}


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reddit_connector_uses_oauth_and_parses_listing():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.host + req.url.path)
        if req.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.url.params["restrict_sr"] == "1"
        return httpx.Response(200, json={"data": {"children": [{"data": {
            "name": "t3_abc", "subreddit": "smartwatch", "author": "ravi", "created_utc": 1767225600,
            "title": "Kestrel strap snapped", "selftext": "after two weeks", "permalink": "/r/smartwatch/abc",
            "score": 41, "num_comments": 9}}]}})

    posts = fetch_reddit("Kestrel", subreddit="r/smartwatch", client_id="id", client_secret="s",
                         client=_mock(handler))
    assert calls == ["www.reddit.com/api/v1/access_token", "oauth.reddit.com/r/smartwatch/search"]
    p = posts[0]
    assert (p.external_id, p.platform, p.channel, p.engagement) == ("reddit-t3_abc", "REDDIT", "r/smartwatch", 50)
    assert p.text == "Kestrel strap snapped after two weeks"


def test_x_connector_parses_recent_search():
    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Bearer xyz"
        return httpx.Response(200, json={
            "data": [{"id": "99", "author_id": "7", "text": "Bazaar delivery late again",
                      "created_at": "2025-10-28T10:00:00.000Z",
                      "public_metrics": {"like_count": 3, "reply_count": 1, "retweet_count": 2}}],
            "includes": {"users": [{"id": "7", "username": "metro_mom"}]}})

    p = fetch_x("Bazaar", bearer="xyz", client=_mock(handler))[0]
    assert (p.channel, p.engagement, p.url) == ("@metro_mom", 6, "https://x.com/metro_mom/status/99")
    assert p.posted_at == dt.datetime(2025, 10, 28, 10, 0)


def test_connectors_require_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    with pytest.raises(ConnectorError):
        fetch_reddit("x", client_id="", client_secret="")
    with pytest.raises(ConnectorError):
        fetch_x("x", bearer="")


def test_file_import_csv_and_json(tmp_path):
    csv_file = tmp_path / "export.csv"
    csv_file.write_text("platform,channel,author,posted_at,text,engagement\n"
                        "reddit,r/india,u/a,2026-04-02T09:00:00,Kestrel watch broke,12\n"
                        "tiktok,@b,b,2026-04-03T09:00:00,love the sale,3\n"
                        "x,@c,c,2026-04-04T09:00:00,,1\n", encoding="utf-8")
    posts = load_file(csv_file)
    assert [p.platform for p in posts] == ["REDDIT", "OTHER"]  # unknown platform -> OTHER, empty text dropped
    json_file = tmp_path / "x.json"
    json_file.write_text(json.dumps([{"id": "x-1", "platform": "X", "channel": "@d", "author": "d",
                                      "posted_at": "2026-05-01T08:00:00Z", "text": "late delivery"}]))
    assert load_file(json_file)[0].external_id == "x-1"


# --------------------------------------------------------------------------- live Db2
@pytest.fixture(scope="module")
def store():
    try:
        from analyst.config import settings
        from analyst.db import Db2
        from analyst.embeddings import Embedder
        from analyst.social import SocialStore

        db = Db2()
        db.server_info()
        s = SocialStore(db, Embedder(settings), settings)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Db2 not reachable: {e}")
    if not s.available():
        pytest.skip("social posts not loaded (python src/setup_db.py)")
    return s


def test_hybrid_search_finds_the_smartwatch_complaints(store):
    f = SocialFilter("smartwatch strap broke, returning it", subcategory="Smartwatches")
    posts = store.search(f, k=5)
    assert posts and all(p["product"] and "Kestrel" in p["product"] for p in posts)
    timeline = {t["month"]: t["mentions"] for t in store.timeline(f)}
    peak = max(timeline, key=timeline.get)
    assert "2026-03" <= peak <= "2026-07"


def test_stats_cover_all_platforms(store):
    stats = store.stats()
    assert stats["total"] > 1000
    assert {p["platform"] for p in stats["platforms"]} == {"REDDIT", "X", "REVIEW"}
