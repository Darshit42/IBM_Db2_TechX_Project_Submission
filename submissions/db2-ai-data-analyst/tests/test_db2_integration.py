"""Integration tests against a live Db2 (skipped automatically if Db2 is unreachable).

Run `python src/setup_db.py` first.
"""
import pytest

from analyst.agent import Analyst
from analyst.config import settings
from analyst.db import Db2, Db2Error
from analyst.explain import explain
from analyst.memory import SEED_EXAMPLES


@pytest.fixture(scope="module")
def db():
    d = Db2()
    try:
        d.server_info()
        if not d.scalar("SELECT COUNT(*) FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = 'ORDERS'",
                        [settings.data_schema]):
            pytest.skip("Run src/setup_db.py first")
    except Db2Error as e:
        pytest.skip(f"Db2 not reachable: {e}")
    yield d
    d.close()


@pytest.fixture(scope="module")
def analyst(db):
    return Analyst(db)


def test_data_loaded(db):
    assert db.scalar(f"SELECT COUNT(*) FROM {settings.data_schema}.ORDERS") > 10000
    assert db.scalar(f"SELECT COUNT(*) FROM {settings.data_schema}.V_SALES") > 10000


@pytest.mark.parametrize("question,sql", SEED_EXAMPLES, ids=[q[:40] for q, _ in SEED_EXAMPLES])
def test_seed_examples_explain_and_run(db, question, sql):
    plan = explain(db, sql)
    assert plan.total_cost > 0
    assert plan.operators and plan.operators[0]["type"] == "RETURN"
    cols, rows, _ = db.query(sql, read_only=True, max_rows=50)
    assert cols


def test_explain_reports_index_usage(db):
    plan = explain(db, f"SELECT * FROM {settings.data_schema}.ORDERS WHERE ORDER_ID = 500123")
    assert any("PK" in i or "SQL" in i or "IX" in i for i in plan.indexes_used) or plan.total_cost < 100


def test_explain_catches_bad_column(db):
    with pytest.raises(Db2Error) as ei:
        explain(db, f"SELECT NO_SUCH_COLUMN FROM {settings.data_schema}.ORDERS")
    assert ei.value.sqlstate == "42703"


@pytest.mark.skipif(not settings.db2_ro_user, reason="no read-only user configured")
def test_read_only_user_cannot_write(db):
    with pytest.raises(Db2Error):
        db.execute(f"DELETE FROM {settings.data_schema}.REGIONS WHERE 1 = 0", read_only=True)


@pytest.mark.skipif(not settings.db2_ro_user, reason="no read-only user configured")
def test_read_only_user_cannot_read_app_schema(db):
    with pytest.raises(Db2Error):
        db.query(f"SELECT COUNT(*) FROM {settings.app_schema}.QUERY_AUDIT", read_only=True)


def test_vector_memory_search(analyst):
    hits = analyst.memory.search("monthly revenue and gross profit trend")
    assert hits, "expected at least one memory hit"
    assert "monthly" in hits[0].question.lower()
    assert 0 <= hits[0].distance <= 0.6


def test_catalog_prompt_contains_comments_and_values(analyst):
    text = analyst.catalog.prompt_text()
    assert f"{settings.data_schema}.ORDERS" in text
    assert "COMPLETED" in text  # sample value of ORDERS.STATUS
    assert "V_SALES" in text


def test_stored_procedure_audit(db):
    out = db.callproc(f"{settings.app_schema}.LOG_QUERY",
                      ["pytest audit", "SELECT 1 FROM SYSIBM.SYSDUMMY1", "OK", 1.0, 1, 5, 1, None, "{}", 0])
    audit_id = out[-1]
    assert audit_id and audit_id > 0
    assert db.scalar(f"SELECT QUESTION FROM {settings.app_schema}.QUERY_AUDIT WHERE AUDIT_ID = ?",
                     [audit_id]) == "pytest audit"
    db.execute(f"DELETE FROM {settings.app_schema}.QUERY_AUDIT WHERE AUDIT_ID = ?", [audit_id])


def test_pipeline_blocks_writes(analyst):
    from analyst.agent import AskResult

    r = AskResult(question="x", status="ERROR")
    ok = analyst._execute_candidate(r, f"DELETE FROM {settings.data_schema}.ORDERS", attempt=1)
    assert not ok and r.status == "BLOCKED"


def test_pipeline_runs_valid_sql(analyst):
    from analyst.agent import AskResult

    r = AskResult(question="x", status="ERROR")
    ok = analyst._execute_candidate(
        r, f"SELECT CATEGORY, COUNT(*) AS N FROM {settings.data_schema}.PRODUCTS GROUP BY CATEGORY", attempt=1)
    assert ok and r.status == "OK" and r.plan["total_cost"] > 0 and len(r.rows) == 7
