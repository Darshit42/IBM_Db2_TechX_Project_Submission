import math

from analyst import charts
from analyst.embeddings import hash_embed, to_vector_literal
from analyst.seed_data import generate


# ----------------------------------------------------------------------- charts
def test_single_row_is_kpi():
    assert charts.infer(["REVENUE"], [[12345.6]])["type"] == "number"


def test_month_series_is_line():
    cols = ["MONTH", "REVENUE"]
    rows = [["2025-01", 10.0], ["2025-02", 12.0], ["2025-03", 9.0]]
    spec = charts.infer(cols, rows)
    assert spec["type"] == "line" and spec["x"] == "MONTH"


def test_few_categories_is_pie():
    rows = [["CARD", 1.0], ["UPI", 2.0], ["COD", 3.0]]
    assert charts.infer(["PAYMENT", "REVENUE"], rows)["type"] == "pie"


def test_two_dimensions_use_series():
    rows = [["North", "STORE", 1.0], ["North", "ONLINE", 2.0], ["South", "STORE", 3.0], ["South", "ONLINE", 1.0]]
    spec = charts.infer(["REGION", "CHANNEL", "REVENUE"], rows)
    assert spec["type"] == "bar" and spec["series"] == "CHANNEL"


def test_text_only_is_table():
    assert charts.infer(["NAME", "CITY"], [["a", "b"]])["type"] == "table"


def test_validate_rejects_unknown_columns():
    rows = [["2025-01", 1.0], ["2025-02", 2.0], ["2025-03", 3.0]]
    spec = charts.validate({"type": "bar", "x": "NOPE", "y": ["REVENUE"]}, ["MONTH", "REVENUE"], rows)
    assert spec["x"] == "MONTH"


def test_validate_keeps_good_spec():
    rows = [["A", 1.0], ["B", 2.0]]
    spec = charts.validate({"type": "bar", "x": "CAT", "y": ["REV"]}, ["CAT", "REV"], rows)
    assert spec == {"type": "bar", "x": "CAT", "y": ["REV"], "series": None}


# ----------------------------------------------------------------------- embeddings
def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_hash_embed_is_unit_length_and_deterministic():
    v = hash_embed("monthly revenue trend", 768)
    assert len(v) == 768
    assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6)
    assert v == hash_embed("monthly revenue trend", 768)


def test_hash_embed_similar_questions_are_closer():
    a = hash_embed("show monthly revenue trend", 768)
    b = hash_embed("monthly revenue trend by month", 768)
    c = hash_embed("which customers returned smartwatches", 768)
    assert _cos(a, b) > _cos(a, c)


def test_vector_literal_format():
    assert to_vector_literal([0.5, -0.25]) == "[0.500000,-0.250000]"


# ----------------------------------------------------------------------- data
def test_seed_data_is_deterministic_and_consistent():
    ds1 = generate(n_customers=200, orders_per_day=5)
    ds2 = generate(n_customers=200, orders_per_day=5)
    assert ds1.orders[:50] == ds2.orders[:50]
    order_ids = {o[0] for o in ds1.orders}
    product_ids = {p[0] for p in ds1.products}
    customer_ids = {c[0] for c in ds1.customers}
    store_ids = {s[0] for s in ds1.stores}
    assert all(i[0] in order_ids and i[2] in product_ids for i in ds1.items)
    assert all(o[1] in customer_ids and o[2] in store_ids for o in ds1.orders)
    assert {o[4] for o in ds1.orders} <= {"COMPLETED", "RETURNED", "CANCELLED"}
