"""Static safety checks applied to LLM-generated SQL before Db2 ever sees it.

This is defence in depth, layered with:
  1. Db2 EXPLAIN cost check (optimizer estimate) in ``explain.py``;
  2. execution under a Db2 user that only holds SELECT on the business schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "CREATE", "ALTER", "TRUNCATE", "GRANT",
    "REVOKE", "CALL", "EXECUTE", "IMPORT", "LOAD", "EXPORT", "REORG", "RUNSTATS", "SET",
    "DECLARE", "LOCK", "COMMIT", "ROLLBACK", "RENAME", "COMMENT", "BEGIN",
}
# System catalogs are allowed only for explicit schema questions; sensitive admin
# routines and the app's own audit/memory tables are never allowed.
BLOCKED_OBJECTS = ("SYSIBMADM.", "SYSPROC.", "SYSFUN.ADMIN", "ADMIN_CMD", "SYSTOOLS.")

_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.I | re.M)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.I)


class GuardrailError(ValueError):
    pass


@dataclass
class CheckedSQL:
    sql: str
    notes: list[str]


def clean(sql: str) -> str:
    """Strip markdown fences, trailing semicolons and surrounding whitespace."""
    sql = _FENCE_RE.sub("", sql.strip()).strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def _strip_literals(sql: str) -> str:
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return _STRING_RE.sub("''", sql)


def check(sql: str, app_schema: str = "ANALYST", max_rows: int = 500) -> CheckedSQL:
    notes: list[str] = []
    sql = clean(sql)
    if not sql:
        raise GuardrailError("Empty SQL.")
    if len(sql) > 20000:
        raise GuardrailError("SQL is unreasonably long.")

    bare = _strip_literals(sql)
    if ";" in bare:
        raise GuardrailError("Only a single statement is allowed.")

    upper = bare.upper()
    first = upper.lstrip("( \n\t").split(None, 1)[0] if upper.strip() else ""
    if first not in ("SELECT", "WITH", "VALUES"):
        raise GuardrailError(f"Only read-only queries are allowed (statement starts with {first or '?'}).")

    tokens = set(re.findall(r"[A-Z_][A-Z0-9_]*", upper))
    bad = sorted(tokens & FORBIDDEN)
    if bad:
        raise GuardrailError(f"Forbidden keyword(s): {', '.join(bad)}.")

    compact = re.sub(r"\s+", "", upper)
    for obj in BLOCKED_OBJECTS:
        if obj.replace(" ", "") in compact:
            raise GuardrailError(f"Access to {obj.rstrip('.')} is not allowed.")
    if re.search(rf"\b{re.escape(app_schema.upper())}\s*\.", upper):
        raise GuardrailError(f"The application schema {app_schema} is not queryable.")

    if re.search(r"\bFOR\s+UPDATE\b", upper):
        raise GuardrailError("FOR UPDATE is not allowed.")

    # Translate a trailing LIMIT n (common LLM habit) to Db2's FETCH FIRST syntax.
    m = _LIMIT_RE.search(sql)
    if m:
        sql = sql[: m.start()].rstrip() + f" FETCH FIRST {m.group(1)} ROWS ONLY"
        notes.append("Rewrote LIMIT to FETCH FIRST.")

    # Enforce a row cap on the outermost query.
    m = _TRAILING_FETCH_RE.search(sql)
    if not m:
        sql = f"{sql}\nFETCH FIRST {max_rows} ROWS ONLY"
        notes.append(f"Added row cap of {max_rows}.")
    else:
        n = int(m.group(1) or 1)
        if n > max_rows:
            sql = sql[: m.start()] + f"FETCH FIRST {max_rows} ROWS ONLY"
            notes.append(f"Reduced row limit from {n} to {max_rows}.")
    return CheckedSQL(sql=sql, notes=notes)


_TRAILING_FETCH_RE = re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\s+(\d+)?\s*ROWS?\s+ONLY\s*$", re.I)
