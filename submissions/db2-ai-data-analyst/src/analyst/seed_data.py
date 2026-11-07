"""Deterministic synthetic retail dataset (no external downloads needed).

The data has realistic structure so the analyst has interesting things to find:
seasonality with a festive-season peak (Oct-Nov) and a December bump, steady growth
of the online channel, regional differences, discount-heavy categories, and a
return-rate spike for one product line in 2026.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field

SEED = 2026
START = dt.date(2024, 11, 1)
END = dt.date(2026, 10, 31)

REGIONS = [
    (1, "North", "India"), (2, "South", "India"), (3, "West", "India"),
    (4, "East", "India"), (5, "Central", "India"), (6, "Online", "India"),
]
CITIES = {
    1: ["Delhi", "Noida", "Gurugram", "Chandigarh", "Lucknow", "Mathura"],
    2: ["Bengaluru", "Chennai", "Hyderabad", "Kochi"],
    3: ["Mumbai", "Pune", "Ahmedabad", "Surat"],
    4: ["Kolkata", "Bhubaneswar", "Guwahati"],
    5: ["Indore", "Bhopal", "Nagpur"],
}
FIRST = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Ananya", "Diya", "Saanvi", "Meera", "Riya",
         "Arjun", "Rohan", "Kavya", "Neha", "Priya", "Rahul", "Sneha", "Vikram", "Pooja", "Karan",
         "Tanvi", "Nikhil", "Aisha", "Farhan", "Zoya", "Harpreet", "Simran", "Lakshmi", "Suresh", "Divya"]
LAST = ["Sharma", "Verma", "Gupta", "Iyer", "Nair", "Reddy", "Patel", "Shah", "Das", "Bose",
        "Singh", "Khan", "Mehta", "Joshi", "Rao", "Menon", "Chopra", "Agarwal", "Kulkarni", "Mishra"]

# category -> (subcategories, brands, price range, margin range, base weight, discount bias)
CATALOG = {
    "Electronics": (["Smartphones", "Laptops", "Headphones", "Smartwatches", "Accessories"],
                    ["Voltix", "Nimbus", "Auralis", "Kestrel"], (999, 89999), (0.08, 0.22), 1.0, 0.6),
    "Home & Kitchen": (["Cookware", "Appliances", "Furniture", "Decor"],
                       ["Hearthly", "Casa Nova", "Ember"], (299, 34999), (0.25, 0.45), 1.1, 0.8),
    "Fashion": (["Menswear", "Womenswear", "Footwear", "Ethnic Wear"],
                ["Threadline", "Saffron", "UrbanKnot"], (399, 7999), (0.40, 0.65), 1.5, 1.4),
    "Grocery": (["Staples", "Snacks", "Beverages", "Organic"],
                ["DailyHarvest", "GreenLeaf", "Masala Co"], (49, 1499), (0.12, 0.30), 2.4, 0.3),
    "Beauty": (["Skincare", "Haircare", "Fragrance", "Makeup"],
               ["Glowra", "Petal & Co", "Velvet"], (149, 4999), (0.45, 0.70), 1.0, 1.0),
    "Sports": (["Fitness", "Cycling", "Outdoor", "Team Sports"],
               ["Stride", "Apex", "Trailmark"], (299, 24999), (0.25, 0.45), 0.6, 0.7),
    "Books & Toys": (["Fiction", "Education", "Board Games", "Kids Toys"],
                     ["Inkwell", "PlayNest", "BrainBox"], (99, 2999), (0.30, 0.50), 0.7, 0.5),
}
PAYMENTS = ["CARD", "UPI", "WALLET", "COD", "INVOICE"]


@dataclass
class Dataset:
    regions: list = field(default_factory=list)
    stores: list = field(default_factory=list)
    customers: list = field(default_factory=list)
    products: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    items: list = field(default_factory=list)


def _season(d: dt.date) -> float:
    """Demand multiplier by date: festive peak in Oct/Nov, December bump, weekend lift."""
    m = {1: 0.85, 2: 0.8, 3: 0.9, 4: 0.9, 5: 0.95, 6: 0.9, 7: 0.85, 8: 1.0,
         9: 1.05, 10: 1.55, 11: 1.45, 12: 1.2}[d.month]
    weekend = 1.25 if d.weekday() >= 5 else 1.0
    days = (d - START).days
    growth = 1.0 + 0.35 * days / (END - START).days  # business grows ~35% over 2 years
    return m * weekend * growth


def generate(n_customers: int = 3000, orders_per_day: float = 55.0, seed: int = SEED) -> Dataset:
    global _DEFECT_CACHE
    _DEFECT_CACHE = None
    rnd = random.Random(seed)
    ds = Dataset(regions=list(REGIONS))

    # Stores ----------------------------------------------------------------
    sid = 100
    for region_id, cities in CITIES.items():
        for city in cities:
            sid += 1
            opened = START - dt.timedelta(days=rnd.randint(200, 2500))
            ds.stores.append((sid, f"Bazaar {city}", city, region_id, "STORE", opened))
    # Mathura flagship opened mid-way so "new store performance" questions work.
    ds.stores = [s if s[2] != "Mathura" else (s[0], s[1], s[2], s[3], s[4], dt.date(2025, 6, 15))
                 for s in ds.stores]
    ds.stores.append((900, "Bazaar Online", "Online", 6, "ONLINE", dt.date(2019, 1, 1)))
    physical = [s for s in ds.stores if s[4] == "STORE"]
    store_weight = {s[0]: rnd.uniform(0.6, 1.6) for s in physical}

    # Customers -------------------------------------------------------------
    all_cities = [c for cs in CITIES.values() for c in cs]
    for cid in range(1, n_customers + 1):
        fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        seg = rnd.choices(["CONSUMER", "SMB", "ENTERPRISE"], [0.82, 0.14, 0.04])[0]
        tier = rnd.choices(["NONE", "SILVER", "GOLD", "PLATINUM"], [0.55, 0.25, 0.14, 0.06])[0]
        signup = START - dt.timedelta(days=rnd.randint(0, 1400)) + dt.timedelta(days=rnd.randint(0, 600))
        signup = min(signup, END)
        email = f"{fn}.{ln}{cid}@example.com".lower()
        ds.customers.append((cid, f"{fn} {ln}", email, rnd.choice(all_cities), seg, tier, signup))
    # Heavier buyers: higher tiers and businesses order more often.
    cust_weight = [
        (c[0], {"NONE": 1, "SILVER": 1.8, "GOLD": 2.8, "PLATINUM": 4.5}[c[5]]
         * {"CONSUMER": 1, "SMB": 2, "ENTERPRISE": 3}[c[4]], c[6])
        for c in ds.customers
    ]

    # Products --------------------------------------------------------------
    pid = 1000
    cat_products: dict[str, list] = {}
    for cat, (subs, brands, (lo, hi), (mlo, mhi), _w, _d) in CATALOG.items():
        for sub in subs:
            for _ in range(rnd.randint(6, 9)):
                pid += 1
                brand = rnd.choice(brands)
                price = round(math.exp(rnd.uniform(math.log(lo), math.log(hi))) / 10) * 10 - 1
                price = max(price, lo)
                cost = round(price * (1 - rnd.uniform(mlo, mhi)), 2)
                name = f"{brand} {sub[:-1] if sub.endswith('s') else sub} {rnd.choice(['Lite', 'Pro', 'Max', 'Classic', 'Plus', 'Prime', 'Go', 'X'])}"
                sku = f"{cat[:3].upper()}-{pid}"
                row = (pid, sku, name, cat, sub, brand, cost, float(price))
                ds.products.append(row)
                cat_products.setdefault(cat, []).append(row)
    cats = list(CATALOG)
    cat_w = [CATALOG[c][4] for c in cats]

    # Orders ----------------------------------------------------------------
    oid = 500000
    d = START
    while d <= END:
        progress = (d - START).days / (END - START).days
        online_share = 0.28 + 0.22 * progress  # online grows from 28% to 50%
        n = int(rnd.gauss(orders_per_day * _season(d), 6))
        eligible = [c for c in cust_weight if c[2] <= d] or cust_weight
        open_stores = [s for s in physical if s[5] <= d]
        cids = rnd.choices([c[0] for c in eligible], [c[1] for c in eligible], k=max(n, 0))
        for cid in cids:
            oid += 1
            if rnd.random() < online_share:
                store_id = 900
                payment = rnd.choices(PAYMENTS, [0.35, 0.4, 0.12, 0.11, 0.02])[0]
                fee = 0.0 if rnd.random() < 0.6 else float(rnd.choice([40, 49, 79, 99]))
            else:
                store_id = rnd.choices([s[0] for s in open_stores], [store_weight[s[0]] for s in open_stores])[0]
                payment = rnd.choices(PAYMENTS, [0.38, 0.45, 0.1, 0.0, 0.07])[0]
                fee = 0.0
            festive = d.month in (10, 11)
            lines = rnd.choices([1, 2, 3, 4, 5], [0.45, 0.28, 0.15, 0.08, 0.04])[0]
            order_cats = set()
            for line_no in range(1, lines + 1):
                cat = rnd.choices(cats, cat_w)[0]
                order_cats.add(cat)
                prod = rnd.choice(cat_products[cat])
                qty = 1 if prod[7] > 5000 else rnd.choices([1, 2, 3, 4, 6], [0.6, 0.2, 0.1, 0.06, 0.04])[0]
                disc_bias = CATALOG[cat][5]
                disc = 0.0
                if rnd.random() < 0.25 * disc_bias + (0.3 if festive else 0):
                    disc = float(rnd.choice([5, 10, 15, 20, 25, 30, 40]) if festive else rnd.choice([5, 10, 15]))
                ds.items.append((oid, line_no, prod[0], qty, prod[7], disc))
            # Status: smartwatches from one brand had a defect wave in 2026 -> returns spike.
            p_return = 0.04
            if "Fashion" in order_cats:
                p_return = 0.09
            if d.year == 2026 and 3 <= d.month <= 7 and any(
                    it[2] in _defect_ids(cat_products) for it in ds.items[-lines:]):
                p_return = 0.38
            r = rnd.random()
            status = "RETURNED" if r < p_return else ("CANCELLED" if r < p_return + 0.025 else "COMPLETED")
            ds.orders.append((oid, cid, store_id, d, status, payment, fee))
        d += dt.timedelta(days=1)
    return ds


_DEFECT_CACHE: set[int] | None = None


def _defect_ids(cat_products: dict) -> set[int]:
    global _DEFECT_CACHE
    if _DEFECT_CACHE is None:
        _DEFECT_CACHE = {p[0] for p in cat_products["Electronics"]
                         if p[4] == "Smartwatches" and p[5] == "Kestrel"}
        if not _DEFECT_CACHE:  # guarantee the storyline exists regardless of brand draw
            _DEFECT_CACHE = {p[0] for p in cat_products["Electronics"] if p[4] == "Smartwatches"}
    return _DEFECT_CACHE
