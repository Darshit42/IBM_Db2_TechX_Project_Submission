"""FastAPI server for the Db2 AI Data Analyst.

    python src/app.py           -> http://localhost:8000
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from analyst import guardrails  # noqa: E402
from analyst.agent import Analyst, Turn  # noqa: E402
from analyst.config import settings  # noqa: E402
from analyst.db import Db2Error, db  # noqa: E402
from analyst.explain import explain  # noqa: E402
from analyst.social import SocialFilter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Db2 AI Data Analyst", version="1.0.0")
_analyst: Analyst | None = None


def analyst() -> Analyst:
    global _analyst
    if _analyst is None:
        _analyst = Analyst(db)
    return _analyst


# ----------------------------------------------------------------------------- models
class HistoryItem(BaseModel):
    question: str = Field(max_length=1000)
    sql: str = Field(max_length=20000)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=10)


class FeedbackRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    sql: str = Field(min_length=6, max_length=20000)


class SqlRequest(BaseModel):
    sql: str = Field(min_length=6, max_length=20000)


# ----------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    try:
        info = db.server_info()
        return {"ok": True, "db2": info, "llm": settings.gemini_model if settings.llm_enabled else None,
                "embeddings": analyst().embedder.provider, "read_only_user": bool(settings.db2_ro_user)}
    except Db2Error as e:
        raise HTTPException(503, f"Db2 not reachable: {e}")


@app.post("/api/ask")
def ask(req: AskRequest):
    history = [Turn(h.question, h.sql) for h in req.history]
    return analyst().ask(req.question, history).to_dict()


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    try:
        mid = analyst().remember(req.question, req.sql)
    except guardrails.GuardrailError as e:
        raise HTTPException(400, str(e))
    except Db2Error as e:
        raise HTTPException(400, f"Query no longer valid: {e}")
    return {"memory_id": mid, "memory_size": analyst().memory.count()}


@app.post("/api/explain")
def explain_sql(req: SqlRequest):
    try:
        checked = guardrails.check(req.sql, settings.app_schema, settings.max_rows)
        return {"sql": checked.sql, "plan": explain(db, checked.sql).to_dict()}
    except guardrails.GuardrailError as e:
        raise HTTPException(400, str(e))
    except Db2Error as e:
        raise HTTPException(400, str(e))


@app.get("/api/schema")
def schema(refresh: bool = False):
    if refresh:
        analyst().catalog.tables(refresh=True)
    return {"schema": settings.data_schema, "tables": analyst().catalog.to_json()}


@app.get("/api/memory")
def memory_list():
    return {"items": analyst().memory.list()}


@app.delete("/api/memory/{memory_id}")
def memory_delete(memory_id: int):
    analyst().memory.delete(memory_id)
    return {"deleted": memory_id}


@app.get("/api/history")
def history(limit: int = 30):
    limit = max(1, min(limit, 200))
    rows = db.query_dicts(
        f"""SELECT AUDIT_ID, ASKED_AT, QUESTION, STATUS, EST_COST, ROW_COUNT, ELAPSED_MS, ATTEMPTS
            FROM {settings.app_schema}.QUERY_AUDIT ORDER BY AUDIT_ID DESC FETCH FIRST {limit} ROWS ONLY"""
    )
    return {"items": rows}


@app.get("/api/stats")
def stats():
    s = settings.app_schema
    totals = db.query_dicts(
        f"""SELECT COUNT(*) AS QUESTIONS,
                   SUM(CASE WHEN STATUS = 'OK' THEN 1 ELSE 0 END) AS SUCCEEDED,
                   SUM(CASE WHEN STATUS = 'BLOCKED' THEN 1 ELSE 0 END) AS BLOCKED,
                   SUM(CASE WHEN ATTEMPTS > 1 AND STATUS = 'OK' THEN 1 ELSE 0 END) AS SELF_CORRECTED,
                   AVG(ELAPSED_MS) AS AVG_MS
            FROM {s}.QUERY_AUDIT"""
    )[0]
    daily = db.query_dicts(f"SELECT * FROM {s}.V_AUDIT_DAILY ORDER BY DAY DESC FETCH FIRST 14 ROWS ONLY")
    try:  # SQL/JSON over the audit CLOB: how often Db2 caught an inflated total
        totals["GRAIN_CAUGHT"] = db.scalar(
            f"SELECT COUNT(*) FROM {s}.QUERY_AUDIT WHERE JSON_EXISTS(DETAILS, '$.grain_caught[0]')")
    except Db2Error:
        totals["GRAIN_CAUGHT"] = None
    return {"totals": totals, "daily": daily, "memory_size": analyst().memory.count(),
            "social": analyst().social.stats()}


@app.get("/api/social/search")
def social_search(q: str, date_from: str | None = None, date_to: str | None = None,
                  category: str | None = None, brand: str | None = None, platform: str | None = None):
    """Hybrid search: Db2 VECTOR_DISTANCE ranking + relational filters, plus a monthly timeline."""
    f = SocialFilter(query=q[:300], date_from=date_from, date_to=date_to, category=category,
                     brand=brand, platform=platform)
    try:
        return analyst().social.evidence(f, k=10).to_dict()
    except (Db2Error, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.app_port)
