"""Use the Db2 optimizer (EXPLAIN) as a pre-execution guardrail and plan inspector.

Before running any generated query we ask Db2 to EXPLAIN it. That gives us:
  * a syntax / semantic validation pass (bad column names fail here, cheaply);
  * the optimizer's estimated total cost (timerons) - expensive queries are blocked;
  * the estimated result cardinality and the access plan operators
    (e.g. TBSCAN vs IXSCAN), which the UI shows to the user.
"""
from __future__ import annotations

import itertools
import os
import time
from dataclasses import dataclass, field

from .db import Db2, Db2Error

_counter = itertools.count(int(time.time()) % 1_000_000 * 10 + os.getpid() % 10)


@dataclass
class Plan:
    total_cost: float
    est_rows: float | None
    operators: list[dict] = field(default_factory=list)

    @property
    def tables_scanned(self) -> list[str]:
        return sorted({o["object"] for o in self.operators if o["type"] == "TBSCAN" and o.get("object")})

    @property
    def indexes_used(self) -> list[str]:
        return sorted({o["object"] for o in self.operators if o["type"] == "IXSCAN" and o.get("object")})

    def to_dict(self) -> dict:
        return {
            "total_cost": round(self.total_cost, 2),
            "est_rows": None if self.est_rows is None else round(self.est_rows, 1),
            "operators": self.operators,
            "tables_scanned": self.tables_scanned,
            "indexes_used": self.indexes_used,
        }


def ensure_explain_tables(db: Db2) -> bool:
    """Create the EXPLAIN tables for the current user if they do not exist."""
    exists = db.scalar(
        "SELECT COUNT(*) FROM SYSCAT.TABLES WHERE TABSCHEMA = CURRENT USER AND TABNAME = 'EXPLAIN_STATEMENT'"
    )
    if exists:
        return False
    db.execute("CALL SYSPROC.SYSINSTALLOBJECTS('EXPLAIN', 'C', CAST(NULL AS VARCHAR(128)), CURRENT USER)")
    return True


def explain(db: Db2, sql: str) -> Plan:
    queryno = next(_counter) % 2_000_000_000
    # EXPLAIN cannot take parameter markers for the statement text itself.
    db.execute(f"EXPLAIN PLAN SET QUERYNO = {queryno} FOR {sql}")

    rows = db.query_dicts(
        """
        SELECT TOTAL_COST, EXPLAIN_TIME
        FROM EXPLAIN_STATEMENT
        WHERE QUERYNO = ? AND EXPLAIN_LEVEL = 'P'
        ORDER BY EXPLAIN_TIME DESC
        FETCH FIRST 1 ROW ONLY
        """,
        [queryno],
    )
    if not rows:
        raise Db2Error("EXPLAIN produced no plan.")
    explain_time = rows[0]["EXPLAIN_TIME"]
    total_cost = float(rows[0]["TOTAL_COST"] or 0)

    ops = db.query_dicts(
        """
        SELECT o.OPERATOR_ID, o.OPERATOR_TYPE, o.TOTAL_COST,
               (SELECT MAX(RTRIM(s.OBJECT_SCHEMA) || '.' || RTRIM(s.OBJECT_NAME))
                  FROM EXPLAIN_STREAM s
                 WHERE s.EXPLAIN_TIME = o.EXPLAIN_TIME AND s.SOURCE_VERSION = o.SOURCE_VERSION
                   AND s.STMTNO = o.STMTNO AND s.SECTNO = o.SECTNO
                   AND s.TARGET_ID = o.OPERATOR_ID AND s.OBJECT_NAME IS NOT NULL) AS OBJ
        FROM EXPLAIN_OPERATOR o
        JOIN EXPLAIN_STATEMENT st
          ON st.EXPLAIN_TIME = o.EXPLAIN_TIME AND st.STMTNO = o.STMTNO AND st.SECTNO = o.SECTNO
         AND st.SOURCE_VERSION = o.SOURCE_VERSION AND st.EXPLAIN_LEVEL = 'P'
        WHERE st.QUERYNO = ? AND o.EXPLAIN_TIME = ?
        ORDER BY o.OPERATOR_ID
        """,
        [queryno, explain_time],
    )
    est = db.scalar(
        """
        SELECT MAX(STREAM_COUNT) FROM EXPLAIN_STREAM s
        WHERE s.EXPLAIN_TIME = ? AND s.TARGET_ID = 1 AND s.SOURCE_TYPE = 'O'
        """,
        [explain_time],
    )
    operators = [
        {
            "id": int(o["OPERATOR_ID"]),
            "type": (o["OPERATOR_TYPE"] or "").strip(),
            "cost": round(float(o["TOTAL_COST"] or 0), 2),
            "object": o["OBJ"],
        }
        for o in ops
    ]
    return Plan(total_cost=total_cost, est_rows=None if est is None else float(est), operators=operators)


def purge_old_plans(db: Db2, days: int = 1) -> None:
    """EXPLAIN_INSTANCE cascades to the other explain tables."""
    try:
        db.execute(f"DELETE FROM EXPLAIN_INSTANCE WHERE EXPLAIN_TIME < CURRENT TIMESTAMP - {int(days)} DAYS")
    except Db2Error:
        pass
