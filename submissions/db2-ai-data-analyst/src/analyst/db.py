"""Thin, typed helpers over the native ibm_db driver."""
from __future__ import annotations

import datetime as dt
import decimal
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import ibm_db

from .config import Settings, settings


class Db2Error(RuntimeError):
    """A Db2 error with the SQLSTATE / SQLCODE pulled out of the driver message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.sqlstate = _extract(message, "SQLSTATE=")
        self.sqlcode = _extract(message, "SQLCODE=")


def _extract(msg: str, key: str) -> str | None:
    i = msg.find(key)
    if i < 0:
        return None
    tail = msg[i + len(key):]
    return tail.split()[0].strip(" ,;") if tail else None


def _err(conn=None, stmt=None) -> Db2Error:
    if stmt is not None:
        msg = ibm_db.stmt_errormsg(stmt)
    elif conn is not None:
        msg = ibm_db.stmt_errormsg() or ibm_db.conn_errormsg(conn)
    else:
        msg = ibm_db.conn_errormsg()
    return Db2Error(msg or "Unknown Db2 error")


def to_jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    return v


class Db2:
    """One connection per (thread, privilege level); cheap enough for a demo server."""

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self._local = threading.local()

    def _conn(self, read_only: bool):
        attr = "ro" if read_only else "rw"
        conn = getattr(self._local, attr, None)
        if conn is not None:
            try:
                if ibm_db.active(conn):
                    return conn
            except Exception:
                pass
        try:
            conn = ibm_db.connect(self.cfg.conn_str(read_only=read_only), "", "")
        except Exception as e:  # ibm_db raises plain Exception on connect failures
            raise Db2Error(str(e)) from e
        setattr(self._local, attr, conn)
        return conn

    def close(self) -> None:
        for attr in ("ro", "rw"):
            conn = getattr(self._local, attr, None)
            if conn is not None:
                try:
                    ibm_db.close(conn)
                except Exception:
                    pass
                setattr(self._local, attr, None)

    # -- execution ---------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None, read_only: bool = False) -> int:
        conn = self._conn(read_only)
        try:
            stmt = ibm_db.prepare(conn, sql)
            if not stmt:
                raise _err(conn)
            ok = ibm_db.execute(stmt, tuple(params)) if params else ibm_db.execute(stmt)
            if not ok:
                raise _err(stmt=stmt)
            n = ibm_db.num_rows(stmt)
            ibm_db.free_stmt(stmt)
            return n
        except Db2Error:
            raise
        except Exception as e:
            raise Db2Error(str(e)) from e

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        conn = self._conn(False)
        try:
            stmt = ibm_db.prepare(conn, sql)
            if not stmt:
                raise _err(conn)
            res = ibm_db.execute_many(stmt, tuple(tuple(r) for r in rows))
            if res is None or res is False:
                raise _err(stmt=stmt)
        except Db2Error:
            raise
        except Exception as e:
            raise Db2Error(str(e)) from e

    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        read_only: bool = False,
        max_rows: int | None = None,
    ) -> tuple[list[str], list[list[Any]], bool]:
        """Returns (columns, rows, truncated)."""
        conn = self._conn(read_only)
        try:
            stmt = ibm_db.prepare(conn, sql)
            if not stmt:
                raise _err(conn)
            ok = ibm_db.execute(stmt, tuple(params)) if params else ibm_db.execute(stmt)
            if not ok:
                raise _err(stmt=stmt)
            ncols = ibm_db.num_fields(stmt)
            cols = [ibm_db.field_name(stmt, i) for i in range(ncols)]
            rows: list[list[Any]] = []
            truncated = False
            while True:
                row = ibm_db.fetch_tuple(stmt)
                if not row:
                    break
                if max_rows is not None and len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append([to_jsonable(v) for v in row])
            ibm_db.free_stmt(stmt)
            return cols, rows, truncated
        except Db2Error:
            raise
        except Exception as e:
            raise Db2Error(str(e)) from e

    def query_dicts(self, sql: str, params: Sequence[Any] | None = None, read_only: bool = False) -> list[dict]:
        cols, rows, _ = self.query(sql, params, read_only=read_only)
        return [dict(zip(cols, r)) for r in rows]

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        _, rows, _ = self.query(sql, params, max_rows=1)
        return rows[0][0] if rows else None

    def callproc(self, name: str, params: Sequence[Any]) -> tuple:
        """CALL a stored procedure; returns the parameter tuple with OUT values filled in."""
        conn = self._conn(False)
        try:
            res = ibm_db.callproc(conn, name, tuple(params))
        except Exception as e:
            raise Db2Error(str(e)) from e
        if res is None or res is False:
            raise _err(conn)
        if isinstance(res, tuple):
            stmt, *values = res
            try:
                ibm_db.free_stmt(stmt)
            except Exception:
                pass
            return tuple(values)
        return ()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        conn = self._conn(False)
        ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
        try:
            yield
            ibm_db.commit(conn)
        except Exception:
            ibm_db.rollback(conn)
            raise
        finally:
            ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)

    def server_info(self) -> dict:
        conn = self._conn(False)
        info = ibm_db.server_info(conn)
        return {"dbms": info.DBMS_NAME, "version": info.DBMS_VER, "database": self.cfg.db2_database}


db = Db2()
