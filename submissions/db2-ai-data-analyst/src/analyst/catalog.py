"""Build an LLM-friendly description of the business schema straight from the Db2 catalog.

Nothing is hard-coded: tables, views, columns, types, keys, foreign keys and the
COMMENT ON remarks all come from SYSCAT, plus a few distinct sample values for
low-cardinality text columns (so the model writes 'COMPLETED', not 'Completed').
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .db import Db2


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    remark: str | None
    samples: list[str] = field(default_factory=list)


@dataclass
class Table:
    name: str
    kind: str  # T or V
    remark: str | None
    row_count: int | None
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[str] = field(default_factory=list)


class Catalog:
    def __init__(self, db: Db2, schema: str):
        self.db = db
        self.schema = schema.upper()
        self._tables: list[Table] | None = None
        self._lock = threading.Lock()

    def tables(self, refresh: bool = False) -> list[Table]:
        with self._lock:
            if self._tables is None or refresh:
                self._tables = self._load()
            return self._tables

    def _load(self) -> list[Table]:
        s = self.schema
        trows = self.db.query_dicts(
            """
            SELECT RTRIM(TABNAME) AS TABNAME, TYPE, REMARKS, CARD
            FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE IN ('T','V')
            ORDER BY TYPE, TABNAME
            """,
            [s],
        )
        tables = {
            r["TABNAME"]: Table(r["TABNAME"], r["TYPE"], r["REMARKS"],
                                None if r["CARD"] is None or r["CARD"] < 0 else int(r["CARD"]))
            for r in trows
        }
        for c in self.db.query_dicts(
            """
            SELECT RTRIM(TABNAME) AS TABNAME, RTRIM(COLNAME) AS COLNAME, RTRIM(TYPENAME) AS TYPENAME,
                   LENGTH, SCALE, NULLS, REMARKS, KEYSEQ
            FROM SYSCAT.COLUMNS WHERE TABSCHEMA = ? ORDER BY TABNAME, COLNO
            """,
            [s],
        ):
            t = tables.get(c["TABNAME"])
            if not t:
                continue
            typ = c["TYPENAME"]
            if typ in ("VARCHAR", "CHARACTER"):
                typ = f"{typ}({c['LENGTH']})"
            elif typ == "DECIMAL":
                typ = f"DECIMAL({c['LENGTH']},{c['SCALE']})"
            t.columns.append(Column(c["COLNAME"], typ, c["NULLS"] == "Y", c["REMARKS"]))
            if c["KEYSEQ"]:
                t.primary_key.append(c["COLNAME"])
        for r in self.db.query_dicts(
            """
            SELECT RTRIM(TABNAME) AS TABNAME, RTRIM(REFTABNAME) AS REFTABNAME,
                   FK_COLNAMES, PK_COLNAMES
            FROM SYSCAT.REFERENCES WHERE TABSCHEMA = ?
            """,
            [s],
        ):
            t = tables.get(r["TABNAME"])
            if t:
                fk = ",".join(r["FK_COLNAMES"].split())
                pk = ",".join(r["PK_COLNAMES"].split())
                t.foreign_keys.append(f"({fk}) -> {r['REFTABNAME']}({pk})")
        self._sample_values(tables)
        return list(tables.values())

    def _sample_values(self, tables: dict[str, Table]) -> None:
        for t in tables.values():
            if t.kind != "T":
                continue
            for col in t.columns:
                if not col.type.startswith("VARCHAR") or col.name.endswith(("NAME", "EMAIL", "SKU")):
                    continue
                try:
                    cols, rows, _ = self.db.query(
                        f'SELECT DISTINCT "{col.name}" FROM "{self.schema}"."{t.name}" '
                        f"FETCH FIRST 13 ROWS ONLY"
                    )
                except Exception:
                    continue
                if len(rows) <= 12:
                    col.samples = sorted(str(r[0]) for r in rows if r[0] is not None)

    # -- renderings ---------------------------------------------------------
    def prompt_text(self) -> str:
        out = [f"Db2 schema {self.schema} (always qualify tables as {self.schema}.<TABLE>):"]
        for t in self.tables():
            kind = "VIEW" if t.kind == "V" else "TABLE"
            head = f"\n{kind} {self.schema}.{t.name}"
            if t.row_count:
                head += f"  (~{t.row_count:,} rows)"
            out.append(head)
            if t.remark:
                out.append(f"  -- {t.remark}")
            for c in t.columns:
                line = f"  {c.name} {c.type}"
                if c.name in t.primary_key:
                    line += " PK"
                if c.remark:
                    line += f"  -- {c.remark}"
                if c.samples:
                    line += f"  values: {', '.join(c.samples)}"
                out.append(line)
            for fk in t.foreign_keys:
                out.append(f"  FK {fk}")
        return "\n".join(out)

    def to_json(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "kind": "view" if t.kind == "V" else "table",
                "remark": t.remark,
                "rows": t.row_count,
                "primary_key": t.primary_key,
                "foreign_keys": t.foreign_keys,
                "columns": [
                    {"name": c.name, "type": c.type, "remark": c.remark, "samples": c.samples}
                    for c in t.columns
                ],
            }
            for t in self.tables()
        ]
