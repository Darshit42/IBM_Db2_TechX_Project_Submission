"""One-shot database setup for the Db2 AI Data Analyst.

    python src/setup_db.py            # create schema, load data, stats, memory
    python src/setup_db.py --reset    # drop and recreate everything
    python src/setup_db.py --memory   # only (re)build the vector memory
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyst.config import settings  # noqa: E402
from analyst.db import Db2, Db2Error  # noqa: E402
from analyst.embeddings import Embedder  # noqa: E402
from analyst.explain import ensure_explain_tables  # noqa: E402
from analyst.memory import SEED_EXAMPLES, QueryMemory  # noqa: E402
from analyst import seed_data  # noqa: E402

SQL_DIR = Path(__file__).resolve().parent / "analyst" / "sql"
DATA_TABLES = ["ORDER_ITEMS", "ORDERS", "PRODUCTS", "CUSTOMERS", "STORES", "REGIONS"]


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def wait_for_db(db: Db2, timeout: int = 900) -> None:
    start = time.time()
    while True:
        try:
            info = db.server_info()
            print(f"    connected: {info['dbms']} {info['version']} / {info['database']}")
            return
        except Db2Error as e:
            if time.time() - start > timeout:
                raise
            print(f"    waiting for Db2 ({str(e)[:80]}...)", flush=True)
            time.sleep(10)


def check_version(db: Db2) -> None:
    ver = db.server_info()["version"]  # e.g. 12.01.0200
    parts = [int(p) for p in ver.split(".")[:3]]
    if parts[0] < 12 or (parts[0] == 12 and (parts[1] < 1 or (parts[1] == 1 and parts[2] < 200))):
        print(f"    WARNING: Db2 {ver} detected; the VECTOR type needs Db2 12.1.2 or later.")


def render_sql() -> list[str]:
    text = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    text = (text.replace("{DATA}", settings.data_schema)
                .replace("{APP}", settings.app_schema)
                .replace("{DIM}", str(settings.embed_dim)))
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split("@") if s.strip()]


def drop_all(db: Db2) -> None:
    """Views and procedures first, then tables children-first (FK order)."""
    for schema in (settings.app_schema, settings.data_schema):
        for v in db.query_dicts(
            "SELECT RTRIM(TABNAME) AS N FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = 'V'", [schema]
        ):
            db.execute(f'DROP VIEW "{schema}"."{v["N"]}"')
        for p in db.query_dicts(
            "SELECT RTRIM(SPECIFICNAME) AS N FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA = ? AND ROUTINETYPE = 'P'",
            [schema],
        ):
            db.execute(f'DROP SPECIFIC PROCEDURE "{schema}"."{p["N"]}"')
        tables = [t["N"] for t in db.query_dicts(
            "SELECT RTRIM(TABNAME) AS N FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = 'T'", [schema]
        )]
        ordered = [t for t in DATA_TABLES if t in tables] + [t for t in tables if t not in DATA_TABLES]
        for t in ordered:
            db.execute(f'DROP TABLE "{schema}"."{t}"')


def create_schema(db: Db2) -> None:
    for stmt in render_sql():
        try:
            db.execute(stmt)
        except Db2Error as e:
            if e.sqlstate in ("42710",):  # object already exists
                continue
            print(f"    failed statement:\n{stmt[:300]}")
            raise


def load_data(db: Db2) -> None:
    ds = seed_data.generate()
    s = settings.data_schema
    plan = [
        ("REGIONS", ds.regions, 3),
        ("STORES", ds.stores, 6),
        ("CUSTOMERS", ds.customers, 7),
        ("PRODUCTS", ds.products, 8),
        ("ORDERS", ds.orders, 7),
        ("ORDER_ITEMS", ds.items, 6),
    ]
    for table, rows, ncols in plan:
        t0 = time.time()
        sql = f"INSERT INTO {s}.{table} VALUES ({', '.join('?' * ncols)})"
        batch = 2000
        with db.transaction():
            for i in range(0, len(rows), batch):
                db.execute_many(sql, rows[i:i + batch])
        print(f"    {table:<12} {len(rows):>8,} rows  ({time.time() - t0:.1f}s)")


def runstats(db: Db2) -> None:
    for t in DATA_TABLES:
        db.execute(
            f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {settings.data_schema}.{t} "
            f"WITH DISTRIBUTION AND DETAILED INDEXES ALL')"
        )
    print("    statistics collected for the optimizer")


def create_ro_user(container: str) -> bool:
    """Create an OS user inside the Db2 container (Db2 authenticates against the OS)."""
    u, p = settings.db2_ro_user, settings.db2_ro_password
    if not (u and p):
        return False
    script = f"id -u {u} >/dev/null 2>&1 || useradd -M -s /sbin/nologin {u}; echo '{u}:{p}' | chpasswd"
    try:
        subprocess.run(["docker", "exec", container, "bash", "-c", script],
                       check=True, capture_output=True, text=True, timeout=60)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"    could not create OS user in container '{container}': {e}")
        return False


def grant_read_only(db: Db2) -> None:
    u = settings.db2_ro_user.upper()
    db.execute(f"GRANT CONNECT ON DATABASE TO USER {u}")
    for t in db.query_dicts(
        "SELECT RTRIM(TABNAME) AS N FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE IN ('T','V')",
        [settings.data_schema],
    ):
        db.execute(f'GRANT SELECT ON "{settings.data_schema}"."{t["N"]}" TO USER {u}')
    print(f"    {u} may only SELECT from {settings.data_schema}.* (no access to {settings.app_schema})")


def build_memory(db: Db2) -> None:
    emb = Embedder()
    mem = QueryMemory(db, emb)
    print(f"    embedding provider: {emb.provider} ({settings.embed_dim} dims)")
    db.execute(f"DELETE FROM {settings.app_schema}.QUERY_MEMORY WHERE SOURCE = 'SEED'")
    for q, sql in SEED_EXAMPLES:
        mem.add(q, sql.replace("RETAIL.", f"{settings.data_schema}."), source="SEED")
    print(f"    {len(SEED_EXAMPLES)} seed examples stored as VECTOR({settings.embed_dim}, FLOAT32)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate all objects")
    ap.add_argument("--memory", action="store_true", help="only rebuild the vector memory")
    ap.add_argument("--container", default="db2-analyst", help="Docker container for the read-only user")
    args = ap.parse_args()

    db = Db2()
    step("Connecting to Db2")
    wait_for_db(db)
    check_version(db)

    if args.memory:
        step("Rebuilding vector memory")
        build_memory(db)
        return

    exists = db.scalar(
        "SELECT COUNT(*) FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = 'ORDERS'", [settings.data_schema]
    )
    if exists and not args.reset:
        print(f"    {settings.data_schema}.ORDERS already exists - use --reset to rebuild. Refreshing memory only.")
        build_memory(db)
        return
    if args.reset:
        step("Dropping existing objects")
        drop_all(db)

    step("Creating tables, indexes, views, stored procedure and VECTOR memory table")
    create_schema(db)
    step("Loading synthetic retail data")
    load_data(db)
    step("RUNSTATS")
    runstats(db)
    step("Creating EXPLAIN tables")
    print("    created" if ensure_explain_tables(db) else "    already present")
    step("Least-privilege query user")
    if settings.db2_ro_user and create_ro_user(args.container):
        grant_read_only(db)
    else:
        print("    skipped (DB2_RO_USER not set or container not reachable); queries run as the main user")
    step("Seeding semantic query memory")
    build_memory(db)
    print("\nDone. Start the app with:  python src/app.py")


if __name__ == "__main__":
    main()
