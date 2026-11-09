"""Grain check: catch "runs fine, wrong number" SQL by letting Db2 prove the total.

The classic text-to-SQL failure is not a syntax error, it is a join that repeats rows
before an aggregate: SUM(o.SHIPPING_FEE) after joining ORDERS to ORDER_ITEMS counts each
order's fee once per line; SUM(v.NET_REVENUE) after joining sales to social posts counts
revenue once per post. The query succeeds and the number is inflated.

For every SUM / COUNT / AVG in a join query we work out which table the aggregated
expression comes from (its "grain" table) and ask Db2 two control questions over the very
same FROM / JOIN / WHERE:

    naive : AGG(expr) over the joined rows                        (what the query computes)
    exact : AGG(expr) over DISTINCT grain-table primary keys       (each source row once)

If they differ, the join fanned the grain table out and the answer is wrong. The agent
then feeds Db2's evidence back to the model, which rewrites the query (typically by
aggregating in a CTE before joining). Both control queries run as the read-only user.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field

import sqlglot
from sqlglot import exp

from .db import Db2, Db2Error

log = logging.getLogger("analyst.grain")

# Views have no declared primary key; declare their grain explicitly.
VIEW_GRAIN = {"V_SALES": ["ORDER_ID", "LINE_NO"]}
TOLERANCE = 0.005  # relative difference treated as a real discrepancy

_CURRENT = re.compile(r"\bCURRENT\s+(DATE|TIMESTAMP|TIME|SCHEMA|USER)\b", re.I)
_DURATION = re.compile(r"\b(\d+(?:\.\d+)?)\s+(YEARS?|MONTHS?|DAYS?|HOURS?|MINUTES?|SECONDS?)\b", re.I)


@dataclass
class AggCheck:
    aggregate: str            # e.g. SUM(o.SHIPPING_FEE)
    grain_table: str          # e.g. RETAIL.ORDERS
    status: str               # pass | fail | skipped
    joined_rows: int | None = None
    source_rows: int | None = None
    naive_value: float | None = None
    exact_value: float | None = None
    reason: str | None = None

    @property
    def factor(self) -> float | None:
        if self.naive_value is None or not self.exact_value:
            return None
        return round(self.naive_value / self.exact_value, 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["factor"] = self.factor
        return d


@dataclass
class GrainReport:
    status: str  # pass | fail | skipped | n/a
    checks: list[AggCheck] = field(default_factory=list)
    reason: str | None = None

    @property
    def failures(self) -> list[AggCheck]:
        return [c for c in self.checks if c.status == "fail"]

    def feedback(self) -> str:
        """Evidence for the model, phrased as a Db2 finding."""
        parts = []
        for c in self.failures:
            parts.append(
                f"{c.aggregate} is computed after a join that repeats rows of {c.grain_table}: "
                f"{c.source_rows:,} source rows became {c.joined_rows:,} joined rows, so the total is "
                f"{_fmt(c.naive_value)} instead of {_fmt(c.exact_value)} ({c.factor}x inflated)."
            )
        return ("Db2 grain check failed (the query runs, but the numbers are wrong). " + " ".join(parts)
                + " Aggregate each table at its own grain first (e.g. in a CTE grouped by the join key), "
                  "then join the aggregated results.")

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "checks": [c.to_dict() for c in self.checks]}


def _fmt(v: float | None) -> str:
    return "NULL" if v is None else f"{v:,.2f}"


# --------------------------------------------------------------------------- parsing
def _protect(sql: str) -> tuple[str, dict[str, str]]:
    """Hide Db2 idioms sqlglot cannot parse (CURRENT DATE, labeled durations) behind identifiers."""
    subs: dict[str, str] = {}

    def rep(m: re.Match) -> str:
        key = f"ZZDB2PH{len(subs)}"
        subs[key] = m.group(0)
        return key

    sql = _CURRENT.sub(rep, sql)
    sql = _DURATION.sub(rep, sql)
    return sql, subs


def _restore(sql: str, subs: dict[str, str]) -> str:
    for k in sorted(subs, key=len, reverse=True):
        sql = sql.replace(k, subs[k])
    return sql


@dataclass
class _Source:
    alias: str
    schema: str
    table: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass
class ControlQueries:
    aggregate: str
    grain: _Source
    naive_sql: str
    exact_sql: str


def plan_controls(sql: str, columns_of: dict[str, list[str]], keys_of: dict[str, list[str]],
                  default_schema: str) -> tuple[list[ControlQueries], str | None]:
    """Build the control queries for ``sql``. Pure function (no database) so it is unit-testable.

    columns_of / keys_of map an unqualified table name (upper case) to its columns / grain key.
    Returns (controls, reason_if_not_applicable).
    """
    protected, subs = _protect(sql)
    try:
        tree = sqlglot.parse_one(protected)
    except Exception as e:  # noqa: BLE001 - sqlglot raises several error types
        return [], f"could not parse SQL for the grain check ({type(e).__name__})"
    if not isinstance(tree, exp.Select):
        return [], "set operations are not checked"
    if tree.args.get("with_") or tree.args.get("with"):
        return [], "query pre-aggregates in CTEs"

    from_ = tree.args.get("from_") or tree.args.get("from")
    joins = tree.args.get("joins") or []
    if not from_ or not joins:
        return [], None  # single source: nothing can fan out
    sources: list[_Source] = []
    for node in [from_.this] + [j.this for j in joins]:
        if not isinstance(node, exp.Table):
            return [], "joins a derived table"
        name = node.name.upper()
        schema = (node.db or default_schema).upper()
        sources.append(_Source(node.alias or name, schema, name))
    by_alias = {s.alias.upper(): s for s in sources}

    controls: list[ControlQueries] = []
    seen: set[str] = set()
    for sel in tree.expressions:
        for agg in sel.find_all(exp.Sum, exp.Count, exp.Avg):
            arg = agg.this
            if isinstance(arg, exp.Distinct) or isinstance(arg, exp.Star) or arg is None:
                continue  # COUNT(*) / COUNT(DISTINCT ...) are grain-safe by construction
            if agg.find_ancestor(exp.Window):
                continue
            src = _grain_source(arg, sources, by_alias, columns_of)
            if src is None:
                continue
            keys = keys_of.get(src.table)
            if not keys:
                continue
            label = agg.sql()
            if label in seen:
                continue
            seen.add(label)
            controls.append(_build(tree, agg, arg, src, keys, subs))
    return controls, None if controls else "no checkable aggregates"


def _grain_source(arg, sources, by_alias, columns_of) -> _Source | None:
    """The single source table all columns of the aggregate argument come from."""
    found: set[str] = set()
    for col in arg.find_all(exp.Column):
        q = (col.table or "").upper()
        if q:
            if q not in by_alias:
                return None
            found.add(q)
        else:
            owners = [s.alias.upper() for s in sources if col.name.upper() in columns_of.get(s.table, [])]
            if len(owners) != 1:
                return None
            found.add(owners[0])
    if len(found) != 1:
        return None
    return by_alias[found.pop()]


def _build(tree: exp.Select, agg, arg, src: _Source, keys: list[str], subs) -> ControlQueries:
    base = tree.copy()
    for k in ("group", "having", "order", "limit", "offset", "fetch", "qualify", "distinct"):
        base.set(k, None)
    fn = type(agg).__name__.upper()  # SUM | COUNT | AVG

    naive = base.copy()
    naive.set("expressions", [
        exp.alias_(exp.Count(this=exp.Star()), "JOINED_ROWS"),
        exp.alias_(exp.Anonymous(this=fn, expressions=[arg.copy()]), "NAIVE_VALUE"),
    ])

    inner = base.copy()
    if src.alias.upper() != src.table:
        key_cols = [exp.column(k, table=src.alias) for k in keys]
    else:
        key_cols = [exp.Column(this=exp.to_identifier(k), table=exp.to_identifier(src.table),
                               db=exp.to_identifier(src.schema)) for k in keys]
    inner.set("expressions",
              [exp.alias_(c, f"ZZK{i}") for i, c in enumerate(key_cols)] + [exp.alias_(arg.copy(), "ZZV")])
    inner.set("distinct", exp.Distinct())
    exact_sql = (f"SELECT COUNT(*) AS SOURCE_ROWS, {fn}(ZZG.ZZV) AS EXACT_VALUE "
                 f"FROM ({inner.sql()}) AS ZZG")
    return ControlQueries(
        aggregate=_restore(agg.sql(), subs),
        grain=src,
        naive_sql=_restore(naive.sql(), subs),
        exact_sql=_restore(exact_sql, subs),
    )


# --------------------------------------------------------------------------- execution
class GrainChecker:
    def __init__(self, db: Db2, catalog, data_schema: str):
        self.db = db
        self.catalog = catalog
        self.schema = data_schema.upper()

    def _maps(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        cols, keys = {}, {}
        for t in self.catalog.tables():
            cols[t.name] = [c.name for c in t.columns]
            keys[t.name] = VIEW_GRAIN.get(t.name) or list(t.primary_key)
        return cols, keys

    def check(self, sql: str) -> GrainReport:
        cols, keys = self._maps()
        controls, reason = plan_controls(sql, cols, keys, self.schema)
        if not controls:
            if reason in (None, "no checkable aggregates"):
                return GrainReport(status="n/a", reason=reason and "aggregates are grain-safe (COUNT(*) / DISTINCT)")
            return GrainReport(status="skipped", reason=reason)
        checks: list[AggCheck] = []
        for c in controls:
            chk = AggCheck(aggregate=c.aggregate, grain_table=c.grain.qualified, status="skipped")
            try:
                jr, naive = self.db.query(c.naive_sql, read_only=True)[1][0]
                sr, exact = self.db.query(c.exact_sql, read_only=True)[1][0]
            except Db2Error as e:
                chk.reason = f"control query failed: {str(e)[-160:]}"
                log.info("grain control failed for %s: %s\n%s\n%s", c.aggregate, e, c.naive_sql, c.exact_sql)
                checks.append(chk)
                continue
            chk.joined_rows, chk.source_rows = int(jr or 0), int(sr or 0)
            chk.naive_value = None if naive is None else float(naive)
            chk.exact_value = None if exact is None else float(exact)
            chk.status = "fail" if _differs(chk) else "pass"
            checks.append(chk)
        if any(c.status == "fail" for c in checks):
            status = "fail"
        elif any(c.status == "pass" for c in checks):
            status = "pass"
        else:
            status = "skipped"
        return GrainReport(status=status, checks=checks)


def _differs(c: AggCheck) -> bool:
    if c.joined_rows is not None and c.source_rows is not None and c.joined_rows <= c.source_rows:
        return False
    a, b = c.naive_value, c.exact_value
    if a is None or b is None:
        return False
    if b == 0:
        return abs(a) > 0.0
    return abs(a - b) / abs(b) > TOLERANCE
