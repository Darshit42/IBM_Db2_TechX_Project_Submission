"""Semantic query memory stored in Db2 using the native VECTOR type.

Every verified (question, SQL) pair is stored with its embedding. For a new question
we fetch the nearest neighbours with VECTOR_DISTANCE(..., COSINE) and feed them to the
LLM as few-shot examples - so the analyst gets better as users approve answers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, settings
from .db import Db2
from .embeddings import Embedder, to_vector_literal


@dataclass
class Example:
    memory_id: int
    question: str
    sql: str
    distance: float
    source: str

    @property
    def similarity(self) -> float:
        return round(1.0 - self.distance, 4)

    def to_dict(self) -> dict:
        return {
            "id": self.memory_id,
            "question": self.question,
            "sql": self.sql,
            "similarity": self.similarity,
            "source": self.source,
        }


class QueryMemory:
    def __init__(self, db: Db2, embedder: Embedder, cfg: Settings = settings):
        self.db = db
        self.embedder = embedder
        self.cfg = cfg
        self.table = f"{cfg.app_schema}.QUERY_MEMORY"

    def _vec_expr(self) -> str:
        return f"VECTOR(CAST(? AS CLOB(1M)), {self.cfg.embed_dim}, FLOAT32)"

    def add(self, question: str, sql: str, source: str = "USER") -> int:
        dup = self.db.query_dicts(
            f"SELECT MEMORY_ID FROM {self.table} WHERE LOWER(QUESTION) = LOWER(?) FETCH FIRST 1 ROW ONLY",
            [question],
        )
        vec = to_vector_literal(self.embedder.embed(question))
        if dup:
            mid = int(dup[0]["MEMORY_ID"])
            self.db.execute(
                f"UPDATE {self.table} SET SQL_TEXT = ?, EMBEDDING = {self._vec_expr()}, SOURCE = ? WHERE MEMORY_ID = ?",
                [sql, vec, source, mid],
            )
            return mid
        self.db.execute(
            f"INSERT INTO {self.table} (QUESTION, SQL_TEXT, EMBEDDING, SOURCE) VALUES (?, ?, {self._vec_expr()}, ?)",
            [question, sql, vec, source],
        )
        return int(self.db.scalar(f"SELECT MAX(MEMORY_ID) FROM {self.table}"))

    def search(self, question: str, k: int | None = None, max_distance: float = 0.6) -> list[Example]:
        k = k or self.cfg.memory_k
        vec = to_vector_literal(self.embedder.embed(question))
        rows = self.db.query_dicts(
            f"""
            SELECT MEMORY_ID, QUESTION, SQL_TEXT, SOURCE,
                   VECTOR_DISTANCE(EMBEDDING, {self._vec_expr()}, COSINE) AS DIST
            FROM {self.table}
            ORDER BY DIST
            FETCH FIRST {int(k)} ROWS ONLY
            """,
            [vec],
        )
        out = [
            Example(int(r["MEMORY_ID"]), r["QUESTION"], r["SQL_TEXT"], float(r["DIST"]), r["SOURCE"])
            for r in rows
            if r["DIST"] is not None and float(r["DIST"]) <= max_distance
        ]
        if out:
            ids = ",".join(str(e.memory_id) for e in out)
            self.db.execute(f"UPDATE {self.table} SET TIMES_USED = TIMES_USED + 1 WHERE MEMORY_ID IN ({ids})")
        return out

    def list(self, limit: int = 100) -> list[dict]:
        return self.db.query_dicts(
            f"""
            SELECT MEMORY_ID, QUESTION, SOURCE, TIMES_USED, CREATED_AT
            FROM {self.table} ORDER BY CREATED_AT DESC FETCH FIRST {int(limit)} ROWS ONLY
            """
        )

    def delete(self, memory_id: int) -> None:
        self.db.execute(f"DELETE FROM {self.table} WHERE MEMORY_ID = ?", [memory_id])

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT COUNT(*) FROM {self.table}") or 0)


# Curated examples loaded at setup time (the "SEED" memory).
SEED_EXAMPLES: list[tuple[str, str]] = [
    (
        "What was total revenue last month?",
        """SELECT SUM(NET_REVENUE) AS REVENUE
FROM RETAIL.V_SALES
WHERE STATUS = 'COMPLETED'
  AND ORDER_DATE >= FIRST_DAY(CURRENT DATE - 1 MONTH)
  AND ORDER_DATE <  FIRST_DAY(CURRENT DATE)""",
    ),
    (
        "Show monthly revenue and gross profit trend",
        """SELECT VARCHAR_FORMAT(ORDER_DATE, 'YYYY-MM') AS MONTH,
       SUM(NET_REVENUE) AS REVENUE, SUM(GROSS_PROFIT) AS GROSS_PROFIT
FROM RETAIL.V_SALES
WHERE STATUS = 'COMPLETED'
GROUP BY VARCHAR_FORMAT(ORDER_DATE, 'YYYY-MM')
ORDER BY MONTH""",
    ),
    (
        "Top 10 products by revenue this year",
        """SELECT p.PRODUCT_NAME, p.CATEGORY, SUM(v.NET_REVENUE) AS REVENUE
FROM RETAIL.V_SALES v JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = v.PRODUCT_ID
WHERE v.STATUS = 'COMPLETED' AND YEAR(v.ORDER_DATE) = YEAR(CURRENT DATE)
GROUP BY p.PRODUCT_NAME, p.CATEGORY
ORDER BY REVENUE DESC
FETCH FIRST 10 ROWS ONLY""",
    ),
    (
        "Which region has the highest average order value?",
        """WITH o AS (
  SELECT ORDER_ID, REGION_NAME, SUM(NET_REVENUE) AS ORDER_VALUE
  FROM RETAIL.V_SALES WHERE STATUS = 'COMPLETED'
  GROUP BY ORDER_ID, REGION_NAME)
SELECT REGION_NAME, DECIMAL(AVG(ORDER_VALUE), 12, 2) AS AVG_ORDER_VALUE, COUNT(*) AS ORDERS
FROM o GROUP BY REGION_NAME ORDER BY AVG_ORDER_VALUE DESC""",
    ),
    (
        "Return rate by product subcategory",
        """SELECT p.CATEGORY, p.SUBCATEGORY,
       COUNT(DISTINCT o.ORDER_ID) AS ORDERS,
       DECIMAL(100.0 * COUNT(DISTINCT CASE WHEN o.STATUS = 'RETURNED' THEN o.ORDER_ID END)
               / NULLIF(COUNT(DISTINCT o.ORDER_ID), 0), 5, 2) AS RETURN_RATE_PCT
FROM RETAIL.ORDERS o
JOIN RETAIL.ORDER_ITEMS i ON i.ORDER_ID = o.ORDER_ID
JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = i.PRODUCT_ID
GROUP BY p.CATEGORY, p.SUBCATEGORY
ORDER BY RETURN_RATE_PCT DESC""",
    ),
    (
        "Online vs store revenue share by quarter",
        """SELECT YEAR(ORDER_DATE) || '-Q' || QUARTER(ORDER_DATE) AS QTR, CHANNEL,
       SUM(NET_REVENUE) AS REVENUE
FROM RETAIL.V_SALES WHERE STATUS = 'COMPLETED'
GROUP BY YEAR(ORDER_DATE) || '-Q' || QUARTER(ORDER_DATE), CHANNEL
ORDER BY QTR, CHANNEL""",
    ),
    (
        "Who are our top 5 customers by lifetime spend?",
        """SELECT c.CUSTOMER_ID, c.FULL_NAME, c.SEGMENT, c.LOYALTY_TIER,
       SUM(v.NET_REVENUE) AS LIFETIME_SPEND, COUNT(DISTINCT v.ORDER_ID) AS ORDERS
FROM RETAIL.V_SALES v JOIN RETAIL.CUSTOMERS c ON c.CUSTOMER_ID = v.CUSTOMER_ID
WHERE v.STATUS = 'COMPLETED'
GROUP BY c.CUSTOMER_ID, c.FULL_NAME, c.SEGMENT, c.LOYALTY_TIER
ORDER BY LIFETIME_SPEND DESC
FETCH FIRST 5 ROWS ONLY""",
    ),
    (
        "Revenue growth year over year by category",
        """WITH y AS (
  SELECT CATEGORY, YEAR(ORDER_DATE) AS YR, SUM(NET_REVENUE) AS REVENUE
  FROM RETAIL.V_SALES WHERE STATUS = 'COMPLETED'
  GROUP BY CATEGORY, YEAR(ORDER_DATE))
SELECT CATEGORY, YR, REVENUE,
       DECIMAL(100.0 * (REVENUE - LAG(REVENUE) OVER (PARTITION BY CATEGORY ORDER BY YR))
               / NULLIF(LAG(REVENUE) OVER (PARTITION BY CATEGORY ORDER BY YR), 0), 7, 2) AS YOY_PCT
FROM y ORDER BY CATEGORY, YR""",
    ),
    (
        "Best performing stores by revenue per day since opening",
        """SELECT s.STORE_NAME, s.CITY, s.OPENED_ON,
       DECIMAL(SUM(v.NET_REVENUE) / NULLIF(DAYS(MAX(v.ORDER_DATE)) - DAYS(MAX(
         CASE WHEN s.OPENED_ON > DATE('2024-11-01') THEN s.OPENED_ON ELSE DATE('2024-11-01') END)) + 1, 0), 12, 2)
         AS REVENUE_PER_DAY
FROM RETAIL.V_SALES v JOIN RETAIL.STORES s ON s.STORE_ID = v.STORE_ID
WHERE v.STATUS = 'COMPLETED' AND s.CHANNEL = 'STORE'
GROUP BY s.STORE_NAME, s.CITY, s.OPENED_ON
ORDER BY REVENUE_PER_DAY DESC
FETCH FIRST 10 ROWS ONLY""",
    ),
    (
        "How does average discount change during the festive season?",
        """SELECT MONTH(o.ORDER_DATE) AS MONTH_NUM, MONTHNAME(o.ORDER_DATE) AS MONTH,
       DECIMAL(AVG(i.DISCOUNT_PCT), 5, 2) AS AVG_DISCOUNT_PCT, COUNT(*) AS LINES
FROM RETAIL.ORDERS o JOIN RETAIL.ORDER_ITEMS i ON i.ORDER_ID = o.ORDER_ID
GROUP BY MONTH(o.ORDER_DATE), MONTHNAME(o.ORDER_DATE)
ORDER BY MONTH_NUM""",
    ),
]
