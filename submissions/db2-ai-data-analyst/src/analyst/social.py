"""Voice of the customer: social posts stored in Db2 next to the sales data.

Posts (Reddit, X, review sites) live in RETAIL.SOCIAL_POSTS; their Gemini embeddings
live in ANALYST.SOCIAL_EMBEDDINGS as VECTOR(768, FLOAT32). The point is the *hybrid*
query: one Db2 statement ranks posts by VECTOR_DISTANCE while filtering and joining on
relational columns (date window, product category, brand, store), and another groups
the semantic matches by month - so "why did smartwatch returns spike?" gets the sales
numbers *and* what customers were saying at that time, from the same database.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .config import Settings, settings
from .db import Db2
from .embeddings import Embedder, to_vector_literal

PLATFORMS = ("REDDIT", "X", "REVIEW", "OTHER")
TOPICS = ("QUALITY", "DELIVERY", "PRICE", "SERVICE", "STORE", "SIZING", "APP", "PRAISE")


@dataclass
class Post:
    external_id: str
    platform: str
    channel: str
    author: str
    posted_at: dt.datetime
    text: str
    url: str | None = None
    engagement: int = 0
    product_id: int | None = None
    store_id: int | None = None
    brand: str | None = None
    topic: str | None = None
    sentiment: float | None = None


@dataclass
class SocialFilter:
    query: str
    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None
    subcategory: str | None = None
    brand: str | None = None
    platform: str | None = None

    def clauses(self) -> tuple[list[str], list]:
        where, params = [], []
        if self.date_from:
            where.append("sp.POST_DATE >= ?")
            params.append(_date(self.date_from))
        if self.date_to:
            where.append("sp.POST_DATE <= ?")
            params.append(_date(self.date_to))
        # Prefix match so 'Smartwatch' finds 'Smartwatches' (models often singularise).
        if self.category:
            where.append("UPPER(p.CATEGORY) LIKE UPPER(?)")
            params.append(_prefix(self.category))
        if self.subcategory:
            where.append("UPPER(p.SUBCATEGORY) LIKE UPPER(?)")
            params.append(_prefix(self.subcategory))
        if self.brand:
            where.append("UPPER(COALESCE(p.BRAND, sp.BRAND)) = UPPER(?)")
            params.append(self.brand)
        if self.platform and self.platform.upper() in PLATFORMS:
            where.append("sp.PLATFORM = ?")
            params.append(self.platform.upper())
        return where, params


def _prefix(v: str) -> str:
    v = v.strip().replace("%", "").replace("_", " ")
    return (v[:-1] if v.lower().endswith("s") else v) + "%"


def _date(v: str) -> str:
    return dt.date.fromisoformat(str(v)[:10]).isoformat()


# --------------------------------------------------------------------------- light NLP
# Used for connector imports when no LLM is configured (seed posts carry their own labels).
_POS = {"love", "great", "amazing", "excellent", "fast", "perfect", "happy", "best", "awesome", "smooth",
        "recommend", "worth", "good", "nice", "helpful", "friendly", "quick", "solid", "impressed", "fantastic"}
_NEG = {"broke", "broken", "snapped", "dead", "late", "worst", "refund", "return", "returned", "defective",
        "faulty", "bad", "terrible", "awful", "slow", "rude", "scam", "never", "disappointed", "cracked",
        "overheating", "swollen", "useless", "waste", "delayed", "missing", "wrong", "crashing", "queue", "issue"}
_TOPIC_WORDS = {
    "DELIVERY": ("deliver", "courier", "shipping", "shipped", "arrived", "package", "dispatch"),
    "PRICE": ("price", "discount", "sale", "offer", "deal", "expensive", "cheap", "₹"),
    "SIZING": ("size", "fit", "small", "tight", "loose"),
    "STORE": ("store", "outlet", "branch", "staff", "queue", "billing counter"),
    "APP": ("app", "website", "checkout", "login", "upi failed", "payment failed"),
    "SERVICE": ("support", "customer care", "service", "replacement", "warranty", "helpdesk"),
    "QUALITY": ("broke", "snapped", "defect", "battery", "screen", "quality", "stopped working", "cracked"),
}


def score_sentiment(text: str) -> float:
    words = re.findall(r"[a-z']+", text.lower())
    pos = sum(w in _POS for w in words)
    neg = sum(w in _NEG for w in words)
    if pos == neg == 0:
        return 0.0
    return round(max(-1.0, min(1.0, (pos - neg) / (pos + neg))), 3)


def guess_topic(text: str, sentiment: float | None = None) -> str:
    t = text.lower()
    best, hits = None, 0
    for topic, words in _TOPIC_WORDS.items():
        n = sum(w in t for w in words)
        if n > hits:
            best, hits = topic, n
    if best:
        return best
    return "PRAISE" if (sentiment or 0) > 0.3 else "SERVICE"


def link_entities(text: str, products: list[dict], stores: list[dict]) -> tuple[int | None, int | None, str | None]:
    """Map free text to a product / store / brand from the Db2 catalog (longest name wins)."""
    t = text.lower()
    product_id = brand = None
    for p in sorted(products, key=lambda p: -len(p["PRODUCT_NAME"])):
        if p["PRODUCT_NAME"].lower() in t:
            product_id, brand = int(p["PRODUCT_ID"]), p["BRAND"]
            break
    if brand is None:
        for b in sorted({p["BRAND"] for p in products}, key=len, reverse=True):
            if re.search(rf"\b{re.escape(b.lower())}\b", t):
                brand = b
                break
    store_id = None
    for s in stores:
        if s["CHANNEL"] == "STORE" and re.search(rf"\b{re.escape(s['CITY'].lower())}\b", t):
            store_id = int(s["STORE_ID"])
            break
    return product_id, store_id, brand


# --------------------------------------------------------------------------- store
class SocialStore:
    def __init__(self, db: Db2, embedder: Embedder, cfg: Settings = settings):
        self.db = db
        self.embedder = embedder
        self.cfg = cfg
        self.posts = f"{cfg.data_schema}.SOCIAL_POSTS"
        self.vectors = f"{cfg.app_schema}.SOCIAL_EMBEDDINGS"
        self.products = f"{cfg.data_schema}.PRODUCTS"
        self.stores = f"{cfg.data_schema}.STORES"

    def _vec(self) -> str:
        return f"VECTOR(CAST(? AS CLOB(1M)), {self.cfg.embed_dim}, FLOAT32)"

    def available(self) -> bool:
        try:
            return bool(self.db.scalar(f"SELECT COUNT(*) FROM {self.vectors}"))
        except Exception:  # noqa: BLE001 - table missing on an old install
            return False

    # -- ingestion -------------------------------------------------------------
    def ingest(self, posts: list[Post], enrich: bool = True) -> int:
        """Insert new posts (dedup on EXTERNAL_ID), label them, embed them in batches."""
        if not posts:
            return 0
        existing = {r["EXTERNAL_ID"] for r in self.db.query_dicts(f"SELECT EXTERNAL_ID FROM {self.posts}")}
        fresh = [p for p in posts if p.external_id not in existing]
        if not fresh:
            return 0
        if enrich:
            products = self.db.query_dicts(f"SELECT PRODUCT_ID, PRODUCT_NAME, BRAND FROM {self.products}")
            stores = self.db.query_dicts(f"SELECT STORE_ID, CITY, CHANNEL FROM {self.stores}")
            for p in fresh:
                if p.product_id is None and p.store_id is None and p.brand is None:
                    p.product_id, p.store_id, p.brand = link_entities(p.text, products, stores)
                if p.sentiment is None:
                    p.sentiment = score_sentiment(p.text)
                if p.topic is None:
                    p.topic = guess_topic(p.text, p.sentiment)
        start = int(self.db.scalar(f"SELECT COALESCE(MAX(POST_ID), 0) FROM {self.posts}") or 0)
        rows = []
        for i, p in enumerate(fresh, start=1):
            rows.append((
                start + i, p.external_id[:80], p.platform, p.channel[:80], p.author[:80],
                p.posted_at.replace(microsecond=0), p.posted_at.date(), p.product_id, p.store_id,
                (p.brand or None), (p.topic or "SERVICE"), float(p.sentiment or 0.0), int(p.engagement),
                p.text[:1000], (p.url or None),
            ))
        vectors = self.embedder.embed_many(
            [p.text for p in fresh], progress=lambda d, t: print(f"    embedded {d}/{t}", flush=True))
        with self.db.transaction():
            self.db.execute_many(
                f"INSERT INTO {self.posts} (POST_ID, EXTERNAL_ID, PLATFORM, CHANNEL, AUTHOR, POSTED_AT, POST_DATE, "
                f"PRODUCT_ID, STORE_ID, BRAND, TOPIC, SENTIMENT, ENGAGEMENT, POST_TEXT, URL) "
                f"VALUES ({', '.join('?' * 15)})",
                rows,
            )
            for i in range(0, len(rows), 200):
                self.db.execute_many(
                    f"INSERT INTO {self.vectors} (POST_ID, EMBEDDING) VALUES (?, {self._vec()})",
                    [(r[0], to_vector_literal(v)) for r, v in zip(rows[i:i + 200], vectors[i:i + 200])],
                )
        return len(rows)

    # -- hybrid retrieval ------------------------------------------------------
    def _base(self, f: SocialFilter) -> tuple[str, list]:
        where, params = f.clauses()
        sql = (f"FROM {self.vectors} e "
               f"JOIN {self.posts} sp ON sp.POST_ID = e.POST_ID "
               f"LEFT JOIN {self.products} p ON p.PRODUCT_ID = sp.PRODUCT_ID "
               f"LEFT JOIN {self.stores} s ON s.STORE_ID = sp.STORE_ID")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, params

    # Embedding distances are relative: a post "matches" when it is about as close to the query
    # as the best hit (within MATCH_MARGIN), and never further than MAX_DISTANCE.
    MATCH_MARGIN = 0.08
    MAX_DISTANCE = 0.42

    def cutoff(self, f: SocialFilter) -> float | None:
        """Distance threshold for ``f``: best hit + margin (one Db2 MIN(VECTOR_DISTANCE) query)."""
        vec = to_vector_literal(self.embedder.embed(f.query))
        frm, params = self._base(f)
        best = self.db.query(f"SELECT MIN(VECTOR_DISTANCE(e.EMBEDDING, {self._vec()}, COSINE)) {frm}",
                             [vec] + params)[1][0][0]
        if best is None or float(best) > self.MAX_DISTANCE:
            return None
        return min(float(best) + self.MATCH_MARGIN, self.MAX_DISTANCE)

    def search(self, f: SocialFilter, k: int = 6, max_distance: float | None = None) -> list[dict]:
        """Top-k distinct posts by cosine distance, restricted by relational filters - one Db2 statement."""
        max_distance = self.cutoff(f) if max_distance is None else max_distance
        if max_distance is None:
            return []
        vec = to_vector_literal(self.embedder.embed(f.query))
        frm, params = self._base(f)
        rows = self.db.query_dicts(
            f"""
            SELECT sp.POST_ID, sp.PLATFORM, sp.CHANNEL, sp.AUTHOR, sp.POST_DATE, sp.TOPIC, sp.SENTIMENT,
                   sp.ENGAGEMENT, sp.POST_TEXT, sp.URL, p.PRODUCT_NAME, COALESCE(p.BRAND, sp.BRAND) AS BRAND,
                   s.CITY AS STORE_CITY,
                   VECTOR_DISTANCE(e.EMBEDDING, {self._vec()}, COSINE) AS DIST
            {frm}
            ORDER BY DIST
            FETCH FIRST {int(k) * 40} ROWS ONLY
            """,
            [vec] + params,
        )
        cand = [r for r in rows if r["DIST"] is not None and float(r["DIST"]) <= max_distance]
        # Among near-equal matches prefer engaged posts; show each wording once.
        cand.sort(key=lambda r: (round(float(r["DIST"]), 2), -int(r["ENGAGEMENT"] or 0)))
        out, seen = [], set()
        for r in cand:
            key = re.sub(r"\W+", " ", r["POST_TEXT"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append(_post_dict(r))
            if len(out) == k:
                break
        return out

    def timeline(self, f: SocialFilter, max_distance: float | None = None) -> list[dict]:
        """Monthly volume and sentiment of posts semantically matching the query."""
        max_distance = self.cutoff(f) if max_distance is None else max_distance
        if max_distance is None:
            return []
        vec = to_vector_literal(self.embedder.embed(f.query))
        frm, params = self._base(f)
        cond = f"VECTOR_DISTANCE(e.EMBEDDING, {self._vec()}, COSINE) <= {float(max_distance)}"
        frm = frm + (" AND " if " WHERE " in frm else " WHERE ") + cond
        rows = self.db.query_dicts(
            f"""
            SELECT VARCHAR_FORMAT(sp.POST_DATE, 'YYYY-MM') AS MONTH, COUNT(*) AS MENTIONS,
                   DECIMAL(AVG(sp.SENTIMENT), 5, 3) AS AVG_SENTIMENT
            {frm}
            GROUP BY VARCHAR_FORMAT(sp.POST_DATE, 'YYYY-MM')
            ORDER BY MONTH
            """,
            params + [vec],
        )
        return [{"month": r["MONTH"], "mentions": int(r["MENTIONS"]),
                 "avg_sentiment": float(r["AVG_SENTIMENT"]) if r["AVG_SENTIMENT"] is not None else None}
                for r in rows]

    def evidence(self, f: SocialFilter, k: int = 6) -> "SocialEvidence":
        cut = self.cutoff(f)
        if cut is None:
            return SocialEvidence(f)
        return SocialEvidence(f, posts=self.search(f, k, cut), timeline=self.timeline(f, cut))

    def stats(self) -> dict:
        try:
            rows = self.db.query_dicts(
                f"""SELECT PLATFORM, COUNT(*) AS POSTS, DECIMAL(AVG(SENTIMENT), 5, 3) AS AVG_SENTIMENT,
                           MIN(POST_DATE) AS FIRST, MAX(POST_DATE) AS LAST
                    FROM {self.posts} GROUP BY PLATFORM ORDER BY POSTS DESC"""
            )
        except Exception:  # noqa: BLE001
            return {"total": 0, "platforms": []}
        return {
            "total": sum(int(r["POSTS"]) for r in rows),
            "platforms": [{"platform": r["PLATFORM"], "posts": int(r["POSTS"]),
                           "avg_sentiment": float(r["AVG_SENTIMENT"] or 0),
                           "first": str(r["FIRST"]), "last": str(r["LAST"])} for r in rows],
        }


def _post_dict(r: dict) -> dict:
    return {
        "id": int(r["POST_ID"]), "platform": r["PLATFORM"], "channel": r["CHANNEL"], "author": r["AUTHOR"],
        "date": str(r["POST_DATE"]), "topic": r["TOPIC"], "sentiment": float(r["SENTIMENT"]),
        "engagement": int(r["ENGAGEMENT"] or 0), "text": r["POST_TEXT"], "url": r["URL"],
        "product": r["PRODUCT_NAME"], "brand": r["BRAND"], "store_city": r["STORE_CITY"],
        "similarity": round(1 - float(r["DIST"]), 3),
    }


@dataclass
class SocialEvidence:
    filter: SocialFilter
    posts: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        f = self.filter
        return {
            "query": f.query,
            "filters": {k: v for k, v in f.__dict__.items() if k != "query" and v},
            "posts": self.posts,
            "timeline": self.timeline,
            "total_matches": sum(t["mentions"] for t in self.timeline),
        }
