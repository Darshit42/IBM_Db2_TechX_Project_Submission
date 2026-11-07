import pytest

from analyst.guardrails import GuardrailError, check, clean


def test_plain_select_gets_row_cap():
    out = check("SELECT * FROM RETAIL.ORDERS", max_rows=100)
    assert out.sql.endswith("FETCH FIRST 100 ROWS ONLY")
    assert any("row cap" in n for n in out.notes)


def test_existing_small_fetch_is_kept():
    sql = "SELECT * FROM RETAIL.ORDERS FETCH FIRST 5 ROWS ONLY"
    assert check(sql, max_rows=100).sql == sql


def test_large_fetch_is_reduced():
    out = check("SELECT * FROM RETAIL.ORDERS FETCH FIRST 100000 ROWS ONLY", max_rows=500)
    assert out.sql.endswith("FETCH FIRST 500 ROWS ONLY")


def test_fetch_first_row_only_not_duplicated():
    sql = "SELECT MAX(ORDER_DATE) FROM RETAIL.ORDERS FETCH FIRST ROW ONLY"
    assert check(sql).sql.count("FETCH") == 1


def test_limit_rewritten_to_fetch_first():
    out = check("SELECT * FROM RETAIL.ORDERS ORDER BY ORDER_DATE DESC LIMIT 10")
    assert "LIMIT" not in out.sql
    assert out.sql.endswith("FETCH FIRST 10 ROWS ONLY")


def test_with_cte_allowed():
    check("WITH x AS (SELECT 1 AS A FROM SYSIBM.SYSDUMMY1) SELECT A FROM x")


def test_markdown_fence_and_semicolon_stripped():
    assert clean("```sql\nSELECT 1 FROM SYSIBM.SYSDUMMY1;\n```") == "SELECT 1 FROM SYSIBM.SYSDUMMY1"


@pytest.mark.parametrize("sql", [
    "DELETE FROM RETAIL.ORDERS",
    "UPDATE RETAIL.ORDERS SET STATUS = 'X'",
    "DROP TABLE RETAIL.ORDERS",
    "INSERT INTO RETAIL.REGIONS VALUES (9, 'X', 'Y')",
    "CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE RETAIL.ORDERS')",
    "MERGE INTO RETAIL.ORDERS o USING RETAIL.ORDERS s ON 1=1 WHEN MATCHED THEN DELETE",
])
def test_writes_blocked(sql):
    with pytest.raises(GuardrailError):
        check(sql)


def test_multiple_statements_blocked():
    with pytest.raises(GuardrailError):
        check("SELECT 1 FROM SYSIBM.SYSDUMMY1; DROP TABLE RETAIL.ORDERS")


def test_semicolon_inside_string_is_fine():
    check("SELECT * FROM RETAIL.CUSTOMERS WHERE FULL_NAME = 'a;b'")


def test_keyword_inside_string_is_fine():
    check("SELECT * FROM RETAIL.ORDERS WHERE STATUS = 'DELETE ME'")


def test_hidden_write_in_cte_blocked():
    with pytest.raises(GuardrailError):
        check("WITH d AS (SELECT * FROM OLD TABLE (DELETE FROM RETAIL.ORDERS)) SELECT * FROM d")


def test_app_schema_blocked():
    with pytest.raises(GuardrailError):
        check("SELECT * FROM ANALYST.QUERY_AUDIT", app_schema="ANALYST")


def test_admin_views_blocked():
    with pytest.raises(GuardrailError):
        check("SELECT * FROM SYSIBMADM.ENV_INST_INFO")


def test_for_update_blocked():
    with pytest.raises(GuardrailError):
        check("SELECT * FROM RETAIL.ORDERS FOR UPDATE")


def test_comment_hiding_statement_blocked():
    with pytest.raises(GuardrailError):
        check("SELECT 1 FROM SYSIBM.SYSDUMMY1 /* x */ ; DELETE FROM RETAIL.ORDERS")


def test_empty_rejected():
    with pytest.raises(GuardrailError):
        check("   ")
