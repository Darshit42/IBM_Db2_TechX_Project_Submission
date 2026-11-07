"""Pick a sensible chart for a result set (used to validate / back up the LLM's choice)."""
from __future__ import annotations

import re

CHART_TYPES = {"bar", "line", "pie", "number", "table"}
_TIME_NAME = re.compile(r"(DATE|DAY|WEEK|MONTH|QTR|QUARTER|YEAR|YR|PERIOD)", re.I)
_TIME_VAL = re.compile(r"^\d{4}(-\d{2}(-\d{2})?|-Q[1-4])?$")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def numeric_columns(cols: list[str], rows: list[list]) -> list[str]:
    out = []
    for i, c in enumerate(cols):
        vals = [r[i] for r in rows if r[i] is not None]
        if vals and all(_is_num(v) for v in vals):
            out.append(c)
    return out


def is_time_column(name: str, rows: list[list], idx: int) -> bool:
    if _TIME_NAME.search(name):
        return True
    vals = [str(r[idx]) for r in rows[:20] if r[idx] is not None]
    return bool(vals) and all(_TIME_VAL.match(v) for v in vals)


def infer(cols: list[str], rows: list[list]) -> dict:
    if not rows or not cols:
        return {"type": "table"}
    nums = numeric_columns(cols, rows)
    if len(rows) == 1 and len(nums) >= 1 and len(cols) <= 3:
        return {"type": "number", "y": nums[:3]}
    cats = [c for c in cols if c not in nums]
    if not nums:
        return {"type": "table"}
    if not cats:
        # e.g. YEAR as an integer: treat the first column as the x axis
        if len(cols) >= 2 and is_time_column(cols[0], rows, 0):
            return {"type": "line", "x": cols[0], "y": [c for c in nums if c != cols[0]][:3]}
        return {"type": "table"}
    x = cats[0]
    xi = cols.index(x)
    if is_time_column(x, rows, xi) and len(rows) >= 3:
        series = cats[1] if len(cats) > 1 else None
        return {"type": "line", "x": x, "y": nums[:3], "series": series}
    if len(cats) > 1 and len(nums) >= 1:
        return {"type": "bar", "x": x, "y": nums[:1], "series": cats[1]}
    if len(rows) <= 6 and len(nums) == 1:
        return {"type": "pie", "x": x, "y": nums}
    if len(rows) <= 40:
        return {"type": "bar", "x": x, "y": nums[:2]}
    return {"type": "table"}


def validate(spec: dict | None, cols: list[str], rows: list[list]) -> dict:
    """Keep the LLM's chart if it references real columns, else fall back to inference."""
    if not spec or spec.get("type") not in CHART_TYPES:
        return infer(cols, rows)
    t = spec["type"]
    if t == "table":
        return {"type": "table"}
    nums = set(numeric_columns(cols, rows))
    y = [c for c in (spec.get("y") or []) if c in nums]
    if not y:
        return infer(cols, rows)
    if t == "number":
        return {"type": "number", "y": y[:3]} if len(rows) == 1 else infer(cols, rows)
    x = spec.get("x")
    if x not in cols:
        return infer(cols, rows)
    series = spec.get("series") if spec.get("series") in cols and spec.get("series") != x else None
    if t == "pie" and (len(rows) > 12 or series):
        t = "bar"
    return {"type": t, "x": x, "y": [c for c in y if c != x][:3] or y[:1], "series": series}
