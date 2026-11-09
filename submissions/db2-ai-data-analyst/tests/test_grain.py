"""Grain check: control-query planning (pure) and a live Db2 proof (skips without Db2)."""
import pytest

from analyst.grain import AggCheck, GrainReport, _differs, plan_controls

COLS = {
    "ORDERS": ["ORDER_ID", "CUSTOMER_ID", "STORE_ID", "ORDER_DATE", "STATUS", "PAYMENT", "SHIPPING_FEE"],
    "ORDER_ITEMS": ["ORDER_ID", "LINE_NO", "PRODUCT_ID", "QUANTITY", "UNIT_PRICE", "DISCOUNT_PCT"],
    "PRODUCTS": ["PRODUCT_ID", "SKU", "PRODUCT_NAME", "CATEGORY", "SUBCATEGORY", "BRAND", "UNIT_COST", "LIST_PRICE"],
    "V_SALES": ["ORDER_ID", "LINE_NO", "ORDER_DATE", "STATUS", "PRODUCT_ID", "CATEGORY", "NET_REVENUE"],
    "SOCIAL_POSTS": ["POST_ID", "PRODUCT_ID", "POST_DATE", "SENTIMENT"],
}
KEYS = {"ORDERS": ["ORDER_ID"], "ORDER_ITEMS": ["ORDER_ID", "LINE_NO"], "PRODUCTS": ["PRODUCT_ID"],
        "V_SALES": ["ORDER_ID", "LINE_NO"], "SOCIAL_POSTS": ["POST_ID"]}

FANOUT = """SELECT p.CATEGORY, SUM(o.SHIPPING_FEE) AS FEES
FROM RETAIL.ORDERS o JOIN RETAIL.ORDER_ITEMS i ON i.ORDER_ID = o.ORDER_ID
JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = i.PRODUCT_ID
WHERE o.ORDER_DATE >= CURRENT DATE - 12 MONTHS
GROUP BY p.CATEGORY ORDER BY FEES DESC FETCH FIRST 10 ROWS ONLY"""


def plan(sql):
    return plan_controls(sql, COLS, KEYS, "RETAIL")


def test_fanout_query_gets_control_queries_on_the_orders_grain():
    controls, reason = plan(FANOUT)
    assert reason is None and len(controls) == 1
    c = controls[0]
    assert c.aggregate == "SUM(o.SHIPPING_FEE)"
    assert c.grain.qualified == "RETAIL.ORDERS"
    # Db2 idioms survive the round trip and the result shaping is removed.
    for q in (c.naive_sql, c.exact_sql):
        assert "CURRENT DATE - 12 MONTHS" in q
        assert "GROUP BY" not in q and "FETCH FIRST" not in q and "ORDER BY" not in q
    assert "SELECT DISTINCT o.ORDER_ID AS ZZK0, o.SHIPPING_FEE AS ZZV" in c.exact_sql


def test_single_table_query_needs_no_check():
    assert plan("SELECT SUM(NET_REVENUE) FROM RETAIL.V_SALES") == ([], None)


def test_count_distinct_and_count_star_are_grain_safe():
    sql = ("SELECT p.SUBCATEGORY, COUNT(*) AS N, COUNT(DISTINCT o.ORDER_ID) AS ORDERS FROM RETAIL.ORDERS o "
           "JOIN RETAIL.ORDER_ITEMS i ON i.ORDER_ID = o.ORDER_ID "
           "JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = i.PRODUCT_ID GROUP BY p.SUBCATEGORY")
    controls, reason = plan(sql)
    assert controls == [] and reason == "no checkable aggregates"


def test_unqualified_columns_resolve_through_the_catalog():
    sql = ("SELECT CATEGORY, SUM(SHIPPING_FEE) AS FEES FROM RETAIL.ORDERS "
           "JOIN RETAIL.V_SALES ON RETAIL.V_SALES.ORDER_ID = RETAIL.ORDERS.ORDER_ID GROUP BY CATEGORY")
    controls, _ = plan(sql)
    assert [c.grain.qualified for c in controls] == ["RETAIL.ORDERS"]
    assert "RETAIL.ORDERS.ORDER_ID AS ZZK0" in controls[0].exact_sql


def test_view_grain_and_wrapped_aggregates():
    sql = ("SELECT p.BRAND, DECIMAL(SUM(v.NET_REVENUE), 14, 2) AS REVENUE, AVG(sp.SENTIMENT) AS MOOD "
           "FROM RETAIL.V_SALES v JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = v.PRODUCT_ID "
           "JOIN RETAIL.SOCIAL_POSTS sp ON sp.PRODUCT_ID = p.PRODUCT_ID GROUP BY p.BRAND")
    controls, _ = plan(sql)
    grains = {c.aggregate: c.grain.qualified for c in controls}
    assert grains == {"SUM(v.NET_REVENUE)": "RETAIL.V_SALES", "AVG(sp.SENTIMENT)": "RETAIL.SOCIAL_POSTS"}
    rev = next(c for c in controls if c.grain.table == "V_SALES")
    assert "v.ORDER_ID AS ZZK0, v.LINE_NO AS ZZK1" in rev.exact_sql


def test_cte_and_derived_tables_are_skipped_not_guessed():
    cte = ("WITH s AS (SELECT PRODUCT_ID, SUM(NET_REVENUE) AS R FROM RETAIL.V_SALES GROUP BY PRODUCT_ID) "
           "SELECT p.BRAND, SUM(s.R) FROM s JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = s.PRODUCT_ID GROUP BY p.BRAND")
    assert plan(cte) == ([], "query pre-aggregates in CTEs")
    derived = ("SELECT p.BRAND, SUM(x.R) FROM (SELECT PRODUCT_ID, NET_REVENUE AS R FROM RETAIL.V_SALES) x "
               "JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = x.PRODUCT_ID GROUP BY p.BRAND")
    assert plan(derived)[1] == "joins a derived table"


def test_mixed_table_expressions_are_not_checked():
    sql = ("SELECT SUM(i.QUANTITY * p.UNIT_COST) AS COGS FROM RETAIL.ORDER_ITEMS i "
           "JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = i.PRODUCT_ID")
    assert plan(sql)[0] == []


def test_unparseable_sql_is_skipped():
    assert plan("SELECT FROM WHERE")[1].startswith("could not parse")


def test_difference_rules():
    base = dict(aggregate="SUM(x)", grain_table="T", status="?")
    assert _differs(AggCheck(**base, joined_rows=200, source_rows=100, naive_value=20.0, exact_value=10.0))
    assert not _differs(AggCheck(**base, joined_rows=100, source_rows=100, naive_value=10.0, exact_value=10.0))
    # filtered joins can shrink rows: never a fan-out
    assert not _differs(AggCheck(**base, joined_rows=50, source_rows=80, naive_value=5.0, exact_value=9.0))
    # tiny float noise is tolerated
    assert not _differs(AggCheck(**base, joined_rows=101, source_rows=100, naive_value=10.001, exact_value=10.0))


def test_feedback_cites_db2_evidence():
    chk = AggCheck("SUM(o.SHIPPING_FEE)", "RETAIL.ORDERS", "fail", 101979, 51445, 1098910.0, 555049.0)
    msg = GrainReport("fail", [chk]).feedback()
    assert "51,445 source rows became 101,979 joined rows" in msg
    assert "1.98x inflated" in msg and "CTE" in msg


# --------------------------------------------------------------------------- live Db2
@pytest.fixture(scope="module")
def checker():
    try:
        from analyst.catalog import Catalog
        from analyst.config import settings
        from analyst.db import Db2
        from analyst.grain import GrainChecker

        db = Db2()
        db.server_info()
        return GrainChecker(db, Catalog(db, settings.data_schema), settings.data_schema)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Db2 not reachable: {e}")


def test_db2_catches_inflated_shipping_fees(checker):
    report = checker.check(FANOUT)
    assert report.status == "fail"
    c = report.failures[0]
    assert c.joined_rows > c.source_rows
    assert c.factor > 1.5


def test_db2_passes_a_correct_join(checker):
    sql = ("SELECT p.PRODUCT_NAME, SUM(v.NET_REVENUE) AS REVENUE FROM RETAIL.V_SALES v "
           "JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = v.PRODUCT_ID WHERE v.STATUS = 'COMPLETED' "
           "GROUP BY p.PRODUCT_NAME ORDER BY REVENUE DESC FETCH FIRST 10 ROWS ONLY")
    report = checker.check(sql)
    assert report.status == "pass"
    assert report.checks[0].naive_value == report.checks[0].exact_value


def test_verify_endpoint_flags_the_dashboard_query(checker):
    from analyst.agent import Analyst

    out = Analyst(checker.db).verify(FANOUT)
    assert out["status"] == "OK" and out["rows"]
    assert out["grain"]["status"] == "fail"
    assert out["grain_caught"][0]["aggregate"] == "SUM(o.SHIPPING_FEE)"
    assert out["audit_id"]  # verification is audited like any question


def test_catalog_lists_view_values_for_filters(checker):
    text = checker.catalog.prompt_text()
    view = text[text.index("VIEW RETAIL.V_SALES"):]  # views are listed last
    sub = next(line for line in view.splitlines() if line.strip().startswith("SUBCATEGORY"))
    assert "Smartwatches" in sub
