"""Deterministic synthetic social posts that line up with the sales storylines.

The retailer and its brands are fictional, so real Reddit / X posts never mention them;
these posts stand in for what the connectors (social_connectors.py) would collect for a
real business. Each storyline in seed_data.py has a matching conversation:

  * Kestrel smartwatch defect wave, Mar-Jul 2026  -> strap / battery complaints spike
  * festive season (Oct-Nov)                       -> delivery delays + discount chatter
  * Mathura store opens 2025-06-15                 -> opening buzz, then queue complaints
  * online channel growth                          -> app / UPI checkout posts rise
  * Fashion has the higher baseline return rate    -> sizing complaints
plus a steady background of praise and service posts across all categories.
"""
from __future__ import annotations

import datetime as dt
import math
import random

from .seed_data import END, START
from .social import Post

SEED = 4242

SUBREDDITS = {
    "Electronics": ["r/IndianGaming", "r/gadgets_india", "r/smartwatch", "r/india"],
    "Fashion": ["r/IndianFashionAddicts", "r/india"],
    "Home & Kitchen": ["r/IndianHomeDecor", "r/india"],
    "Grocery": ["r/IndianFood", "r/india"],
    "Beauty": ["r/IndianSkincareAddicts", "r/india"],
    "Sports": ["r/fitnessindia", "r/india"],
    "Books & Toys": ["r/IndianBooks", "r/india"],
    None: ["r/india", "r/bangalore", "r/mumbai", "r/delhi", "r/pune"],
}
REVIEW_SITES = ["review:bazaar.in", "review:mouthshut", "review:gadgets360"]
HANDLES = ["tech_tanvi", "deal_hunter_in", "rohan_reviews", "kavya.k", "metro_mom", "gadgetguru_91",
           "fitwithnikhil", "the_desi_shopper", "unboxing_arjun", "simran_says", "budget_bhai", "priya_codes"]

# (topic, sentiment, templates). {p} product, {b} brand, {c} city, {s} subcategory singular.
KESTREL = ("QUALITY", -0.75, [
    "My {p} strap snapped clean off after two weeks. Third Kestrel watch in our office with the same issue.",
    "Returned my {p} today, the battery swelled and the back cover popped open. Not safe.",
    "Anyone else's Kestrel watch dying after the latest update? Screen stays black, refund requested.",
    "{p}: strap broke while jogging, watch fell and cracked. Bazaar accepted the return at least.",
    "PSA: check your Kestrel smartwatch strap pins. Mine came loose and the watch is gone. Returning it.",
    "Kestrel support says the strap defect is 'known' and to send it back. Why sell it then?",
    "Got a replacement {p} and it overheats on the charger too. Returning for good.",
    "Bought the {p} on sale, battery drains in 5 hours and it gets hot. Defective batch?",
])
KESTREL_RECOVERY = ("QUALITY", 0.45, [
    "Replacement {p} from the new batch has been fine for a month. Strap feels sturdier.",
    "Kestrel fixed the strap pins in the new batch, mine is holding up well now.",
])
FESTIVE_DELIVERY = ("DELIVERY", -0.6, [
    "Diwali order from Bazaar Online still not delivered, 9 days late. The {s} was a gift.",
    "Courier says 'out for delivery' for the 4th day straight. Festive rush is no excuse.",
    "Ordered during the festive sale, package arrived with the wrong {s}. Waiting on exchange.",
    "Bazaar delivery in {c} is crawling this week, everyone ordering for Diwali I guess.",
])
FESTIVE_PRICE = ("PRICE", 0.7, [
    "Bazaar festive sale is actually good this year, got the {p} at 30% off.",
    "Great Diwali deal on {b} stuff at Bazaar, prices lower than the other apps.",
    "Grabbed a {s} in the Bazaar sale for a steal. Worth it.",
])
MATHURA_OPEN = ("STORE", 0.8, [
    "New Bazaar store in Mathura is huge and the staff are super helpful. Finally a proper store here!",
    "Visited the Bazaar Mathura opening weekend, great offers and very clean store.",
    "Bazaar Mathura is the best thing to open in the city this year, family loved it.",
])
MATHURA_QUEUE = ("STORE", -0.45, [
    "Bazaar Mathura billing queue was 40 minutes on Sunday. Need more counters.",
    "Mathura Bazaar store is great but the weekend queue is painful.",
])
APP_POS = ("APP", 0.6, [
    "Bazaar app checkout with UPI takes seconds now, ordering online more than going to the store.",
    "Switched to ordering on the Bazaar website, delivery to {c} is next day and returns are easy.",
])
APP_NEG = ("APP", -0.55, [
    "Bazaar app UPI payment failed twice and money got debited. Refund took 5 days.",
    "Bazaar website keeps crashing at checkout during sale hours.",
])
SIZING = ("SIZING", -0.5, [
    "{b} sizes run really small, had to return the {s} twice.",
    "Ordered my usual size in {p}, way too tight. Return pickup was smooth at least.",
    "Size chart for {b} is useless, everything fits a size smaller.",
])
PRAISE = ("PRAISE", 0.75, [
    "Really happy with the {p}, great quality for the price.",
    "{b} never disappoints, the {s} I got from Bazaar is excellent.",
    "Been using the {p} for a month, totally worth it. Recommend.",
    "Bazaar {c} store staff helped me pick the right {s}. Smooth experience.",
])
SERVICE = ("SERVICE", -0.2, [
    "Bazaar customer care took 3 days to reply about my {s} warranty claim.",
    "Support resolved my issue with the {p} quickly, replacement arrived in 2 days.",
    "Return process for my {s} was fine but the refund was slow.",
])


def _singular(sub: str) -> str:
    return sub[:-1] if sub.endswith("s") else sub


def _fill(tpl: str, product: dict | None, city: str) -> str:
    p = product or {}
    return tpl.format(p=p.get("PRODUCT_NAME", "order"), b=p.get("BRAND", "Bazaar"),
                      s=_singular(p.get("SUBCATEGORY", "item")).lower(), c=city)


def _ts(rnd: random.Random, d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(rnd.randint(7, 23), rnd.randint(0, 59), rnd.randint(0, 59)))


def _rand_date(rnd: random.Random, lo: dt.date, hi: dt.date) -> dt.date:
    return lo + dt.timedelta(days=rnd.randint(0, max((hi - lo).days, 0)))


def generate(products: list[dict], stores: list[dict], seed: int = SEED) -> list[Post]:
    """products: PRODUCT_ID, PRODUCT_NAME, CATEGORY, SUBCATEGORY, BRAND; stores: STORE_ID, CITY, CHANNEL."""
    rnd = random.Random(seed)
    posts: list[Post] = []
    cities = [s["CITY"] for s in stores if s["CHANNEL"] == "STORE"]
    store_by_city = {s["CITY"]: int(s["STORE_ID"]) for s in stores if s["CHANNEL"] == "STORE"}
    by_cat: dict[str, list[dict]] = {}
    for p in products:
        by_cat.setdefault(p["CATEGORY"], []).append(p)
    kestrel = [p for p in products if p["SUBCATEGORY"] == "Smartwatches" and p["BRAND"] == "Kestrel"] \
        or [p for p in products if p["SUBCATEGORY"] == "Smartwatches"]
    fashion = by_cat.get("Fashion", [])

    def add(story, d: dt.date, product=None, store_city=None, platform=None, boost=1.0):
        topic, base, templates = story
        platform = platform or rnd.choices(["REDDIT", "X", "REVIEW"], [0.45, 0.35, 0.20])[0]
        city = store_city or rnd.choice(cities)
        text = _fill(rnd.choice(templates), product, city)
        if platform == "REDDIT":
            channel = rnd.choice(SUBREDDITS.get(product["CATEGORY"] if product else None, SUBREDDITS[None]))
            author = f"u/{rnd.choice(HANDLES)}{rnd.randint(1, 999)}"
        elif platform == "X":
            channel = author = f"@{rnd.choice(HANDLES)}"
        else:
            channel, author = rnd.choice(REVIEW_SITES), f"verified buyer #{rnd.randint(1000, 99999)}"
        n = len(posts) + 1
        sentiment = max(-1.0, min(1.0, base + rnd.uniform(-0.2, 0.2)))
        posts.append(Post(
            external_id=f"seed-{n:05d}", platform=platform, channel=channel, author=author,
            posted_at=_ts(rnd, d), text=text, url=None,
            engagement=int(math.exp(rnd.uniform(0.5, 4.5)) * boost),
            product_id=int(product["PRODUCT_ID"]) if product else None,
            store_id=store_by_city.get(store_city) if store_city else None,
            brand=product["BRAND"] if product else None, topic=topic, sentiment=round(sentiment, 3),
        ))

    # 1) Kestrel defect wave: ramps up in March, peaks Apr-May, fades by July.
    wave = {3: 25, 4: 70, 5: 85, 6: 55, 7: 30}
    for month, n in wave.items():
        lo = dt.date(2026, month, 1)
        hi = (lo.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        for _ in range(n):
            add(KESTREL, _rand_date(rnd, lo, hi), rnd.choice(kestrel), boost=2.5)
    for _ in range(12):  # occasional complaints outside the wave (baseline noise)
        add(KESTREL, _rand_date(rnd, START, dt.date(2026, 2, 28)), rnd.choice(kestrel))
    for _ in range(18):
        add(KESTREL_RECOVERY, _rand_date(rnd, dt.date(2026, 8, 1), END), rnd.choice(kestrel))

    # 2) Festive seasons: delivery delays and sale chatter.
    for year in (2024, 2025):
        lo = max(dt.date(year, 10, 1), START)
        hi = dt.date(year, 11, 30)
        for _ in range(55 if year == 2025 else 35):
            cat = rnd.choice(list(by_cat))
            add(FESTIVE_DELIVERY, _rand_date(rnd, lo, hi), rnd.choice(by_cat[cat]))
        for _ in range(45 if year == 2025 else 30):
            cat = rnd.choice(list(by_cat))
            add(FESTIVE_PRICE, _rand_date(rnd, lo, hi), rnd.choice(by_cat[cat]))
    for _ in range(30):  # 2026 festive season up to the end of the data
        cat = rnd.choice(list(by_cat))
        add(FESTIVE_DELIVERY if rnd.random() < 0.5 else FESTIVE_PRICE,
            _rand_date(rnd, dt.date(2026, 10, 1), END), rnd.choice(by_cat[cat]))

    # 3) Mathura store opening.
    for _ in range(40):
        add(MATHURA_OPEN, _rand_date(rnd, dt.date(2025, 6, 15), dt.date(2025, 7, 31)), store_city="Mathura")
    for _ in range(20):
        add(MATHURA_QUEUE, _rand_date(rnd, dt.date(2025, 7, 1), END), store_city="Mathura")

    # 4) Online growth: app posts become more frequent over time.
    d = START
    while d <= END:
        progress = (d - START).days / (END - START).days
        if rnd.random() < 0.10 + 0.35 * progress:
            add(APP_POS if rnd.random() < 0.65 else APP_NEG, d)
        d += dt.timedelta(days=1)

    # 5) Fashion sizing complaints, steady.
    for _ in range(90):
        add(SIZING, _rand_date(rnd, START, END), rnd.choice(fashion) if fashion else None)

    # 6) Background praise and service posts across categories.
    for _ in range(420):
        cat = rnd.choice(list(by_cat))
        add(PRAISE if rnd.random() < 0.7 else SERVICE, _rand_date(rnd, START, END), rnd.choice(by_cat[cat]))

    posts.sort(key=lambda p: p.posted_at)
    for i, p in enumerate(posts, start=1):  # stable ids in time order
        p.external_id = f"seed-{i:05d}"
    return posts
