"""The analyst pipeline (LangChain + Gemini over IBM Db2).

    question
      -> semantic memory lookup in Db2 (VECTOR_DISTANCE)          [memory.py]
      -> Gemini writes Db2 SQL + chart spec (structured output)
      -> static guardrails (read-only, single statement, row cap)  [guardrails.py]
      -> Db2 EXPLAIN: validate + cost budget                       [explain.py]
      -> execute as least-privilege Db2 user
      -> Db2 grain check: control queries prove totals are not
         inflated by a join fan-out                                [grain.py]
      -> on any Db2 error or failed grain check: feed Db2's evidence back, retry
      -> optional voice-of-customer evidence: hybrid vector +
         relational search over social posts in Db2               [social.py]
      -> Gemini summarises the result + suggests follow-ups
      -> audit row written through a Db2 stored procedure
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import charts, guardrails
from .catalog import Catalog
from .config import Settings, settings
from .db import Db2, Db2Error
from .embeddings import Embedder
from .explain import Plan, explain
from .grain import GrainChecker, GrainReport
from .memory import Example, QueryMemory
from .social import SocialFilter, SocialStore

log = logging.getLogger("analyst")


# --------------------------------------------------------------------------- schemas
class ChartSpec(BaseModel):
    type: Literal["bar", "line", "pie", "number", "table"] = Field(
        description="bar for category comparisons, line for time series, pie for <=6 parts of a whole, "
                    "number for a single KPI row, table otherwise")
    x: Optional[str] = Field(default=None, description="Result column for the x axis / categories")
    y: list[str] = Field(default_factory=list, description="Numeric result column(s) to plot")
    series: Optional[str] = Field(default=None, description="Optional column that splits data into series")


class SocialSearch(BaseModel):
    query: str = Field(description="Short semantic search phrase describing what customers would say, "
                                   "e.g. 'smartwatch strap broke, returning it'")
    date_from: Optional[str] = Field(default=None, description="ISO date lower bound (YYYY-MM-DD) or null")
    date_to: Optional[str] = Field(default=None, description="ISO date upper bound (YYYY-MM-DD) or null")
    category: Optional[str] = Field(default=None, description="PRODUCTS.CATEGORY filter or null")
    subcategory: Optional[str] = Field(default=None, description="PRODUCTS.SUBCATEGORY filter or null")
    brand: Optional[str] = Field(default=None, description="Brand filter or null")
    platform: Optional[Literal["REDDIT", "X", "REVIEW"]] = Field(default=None, description="Platform filter or null")


class SqlDraft(BaseModel):
    reasoning: str = Field(description="One or two sentences on how the question maps to the schema")
    sql: str = Field(description="A single Db2 SELECT (or WITH) statement, no trailing semicolon")
    title: str = Field(description="Short title for the result, e.g. 'Monthly revenue, 2025'")
    chart: ChartSpec
    social: Optional[SocialSearch] = Field(
        default=None,
        description="Fill ONLY when customer voice helps answer: 'why' questions about sales/returns/ratings, "
                    "or questions about complaints, sentiment, what people say, Reddit, X/Twitter or reviews. "
                    "Otherwise null.")


class Insight(BaseModel):
    answer: str = Field(description="2-4 sentence plain-English answer citing concrete numbers from the data")
    follow_ups: list[str] = Field(description="Three short follow-up questions the user might ask next")


SYSTEM_SQL = """You are a senior data analyst who writes SQL for IBM Db2 12.1 (LUW).
Today is {today}. The data covers {date_range}.

Rules:
- Output ONE query. Normally a read-only SELECT / WITH ... SELECT.
- If the user explicitly asks to change data or schema (delete, update, insert, drop ...), do NOT
  substitute a different query: write the exact statement they asked for. A separate safety layer
  decides whether it may run, and the user must see that decision.
- For "highest / lowest / best / worst" questions return the top ~10 ranked rows (not just one), so
  the answer has context.
- When comparing entities that existed for different lengths of time (e.g. stores with different
  OPENED_ON dates, customers with different SIGNUP_DATEs), normalise (per day / per month active).
- Use Db2 syntax: FETCH FIRST n ROWS ONLY (never LIMIT), CURRENT DATE, YEAR()/MONTH()/QUARTER(),
  VARCHAR_FORMAT(d, 'YYYY-MM') for month buckets, DAYS(d) for day arithmetic, d - 1 MONTH, FIRST_DAY(d),
  LAG/LEAD/RANK() OVER (...), NULLIF for safe division, DECIMAL(x, p, s) for rounding.
- Always qualify table names with the schema. Only use tables/columns listed below.
- Revenue = sum of NET_REVENUE from V_SALES with STATUS = 'COMPLETED' unless the user says otherwise.
- "Last month"/"this year" etc. are relative to CURRENT DATE. If the data ends before that window,
  still write the correct relative query.
- Give every computed column a short UPPER_SNAKE_CASE alias. Order results meaningfully.
- Return at most ~50 rows for rankings unless asked for more.
- SOCIAL_POSTS holds customer posts (Reddit, X, reviews) with TOPIC and SENTIMENT. Use it in SQL for
  volume / sentiment questions. For "why" questions, write SQL for the business metric (e.g. monthly
  return rate) AND fill `social` so the app retrieves matching posts by semantic vector search.

{schema}
"""

SYSTEM_SUMMARY = """You explain query results to business users. Be concise, specific and accurate:
quote actual numbers from the rows (currency is INR: write amounts with the ₹ sign in Indian units,
e.g. ₹1.47 Cr or ₹8.2 L; round percentages to one decimal),
point out the most notable pattern, and never invent data that is not in the rows.
Today is {today}; the dataset covers {date_range}. Periods before today are actuals, not projections.
If a compared period is only partly covered by the data, say so explicitly, because it distorts the
comparison.
When customer posts are provided, connect them to the numbers (timing, volume, what people complain
about), quote at most two short phrases, name the platform, and call them customer posts, not facts."""


@dataclass
class Turn:
    question: str
    sql: str


@dataclass
class AskResult:
    question: str
    status: str  # OK | BLOCKED | ERROR | NO_LLM
    sql: str | None = None
    title: str | None = None
    reasoning: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    chart: dict | None = None
    answer: str | None = None
    follow_ups: list[str] = field(default_factory=list)
    plan: dict | None = None
    examples: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    audit_id: int | None = None
    used_memory_sql: bool = False
    grain: dict | None = None
    grain_caught: list[dict] = field(default_factory=list)
    social: dict | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Analyst:
    def __init__(self, db: Db2, cfg: Settings = settings):
        self.db = db
        self.cfg = cfg
        self.catalog = Catalog(db, cfg.data_schema)
        self.embedder = Embedder(cfg)
        self.memory = QueryMemory(db, self.embedder, cfg)
        self.grain = GrainChecker(db, self.catalog, cfg.data_schema)
        self.social = SocialStore(db, self.embedder, cfg)
        self._llm = None
        self._date_range: str | None = None

    # -- LLM plumbing ---------------------------------------------------------
    @property
    def llm(self):
        """Primary model; see ``_structured`` for the fallback chain actually used."""
        if self._llm is None and self.cfg.llm_enabled:
            self._llm = self._candidates()
        return self._llm[0] if self._llm else None

    def _candidates(self) -> list:
        """One chat model per (model, key), model-major: try every key on the best model first."""
        from langchain_google_genai import ChatGoogleGenerativeAI

        return [
            ChatGoogleGenerativeAI(
                model=model,
                google_api_key=key,
                temperature=0,
                max_retries=1,  # fail over fast on 429/5xx instead of sleeping
                # System instructions require v1beta on the Gemini Developer API.
                api_version="v1beta",
            )
            for model in self.cfg.models
            for key in self.cfg.api_keys
        ]

    def _structured(self, schema):
        """Structured-output runnable with LangChain fallbacks across keys and models."""
        if self.llm is None:
            raise RuntimeError("No Gemini API key configured")
        primary, *rest = [m.with_structured_output(schema) for m in self._llm]
        return primary.with_fallbacks(rest) if rest else primary

    def date_range(self) -> str:
        if self._date_range is None:
            try:
                lo, hi = self.db.query(
                    f"SELECT MIN(ORDER_DATE), MAX(ORDER_DATE) FROM {self.cfg.data_schema}.ORDERS"
                )[1][0]
                self._date_range = f"{lo} to {hi}"
            except Db2Error:
                self._date_range = "unknown dates"
        return self._date_range

    def _sql_messages(self, question: str, examples: list[Example], history: list[Turn],
                      feedback: list[tuple[str, str]]):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        system = SYSTEM_SQL.format(
            today=dt.date.today().isoformat(),
            date_range=self.date_range(),
            schema=self.catalog.prompt_text(),
        )
        msgs = [SystemMessage(system)]
        if examples:
            shots = "\n\n".join(
                f"Q: {e.question}\nSQL:\n{e.sql}" for e in examples
            )
            msgs.append(SystemMessage(
                "Verified examples retrieved from the Db2 query memory (most similar first). "
                "Reuse their patterns where relevant:\n\n" + shots))
        for t in history[-3:]:
            msgs.append(HumanMessage(t.question))
            msgs.append(AIMessage(f"SQL used:\n{t.sql}"))
        msgs.append(HumanMessage(question))
        for bad_sql, err in feedback:
            msgs.append(AIMessage(f"SQL attempt:\n{bad_sql}"))
            msgs.append(HumanMessage(
                f"That query failed in Db2: {err}\nFix it and return a corrected query."))
        return msgs

    def _draft_sql(self, question, examples, history, feedback) -> SqlDraft:
        chain = self._structured(SqlDraft)
        return chain.invoke(self._sql_messages(question, examples, history, feedback))

    def _summarize(self, question: str, sql: str, cols: list[str], rows: list[list],
                   social: dict | None = None, caught: list[dict] | None = None) -> Insight:
        from langchain_core.messages import HumanMessage, SystemMessage

        preview = [dict(zip(cols, r)) for r in rows[:40]]
        body = (f"Question: {question}\n\nSQL:\n{sql}\n\n"
                f"Data available: {self.date_range()}. Before answering, check every period the "
                f"question compares against this range and mention any that are partly missing.\n\n"
                f"Rows returned: {len(rows)} (showing up to 40)\n{json.dumps(preview, default=str)}")
        if caught:
            c = caught[0]
            body += (f"\n\nNote: Db2's grain check rejected an earlier draft whose {c['aggregate']} was "
                     f"inflated {c['factor']}x by a join; the rows above come from the corrected query. "
                     f"Mention this in one short clause.")
        if social and social.get("posts"):
            posts = [{k: p[k] for k in ("platform", "channel", "date", "sentiment", "text")}
                     for p in social["posts"]]
            body += (f"\n\nCustomer posts matching '{social['query']}' (Db2 vector search; "
                     f"{social['total_matches']} matches, by month: "
                     f"{json.dumps(social['timeline'], default=str)}):\n{json.dumps(posts, default=str)}")
        chain = self._structured(Insight)
        system = SYSTEM_SUMMARY.format(today=dt.date.today().isoformat(), date_range=self.date_range())
        return chain.invoke([SystemMessage(system), HumanMessage(body)])

    # -- main entry -----------------------------------------------------------
    def ask(self, question: str, history: list[Turn] | None = None) -> AskResult:
        t0 = time.time()
        question = question.strip()
        res = AskResult(question=question, status="ERROR")
        history = history or []
        try:
            examples = self.memory.search(question)
        except Exception as e:  # Db2 or embedding provider: answer without few-shot examples
            log.warning("memory search failed: %s", e)
            res.notes.append("Vector memory unavailable for this question.")
            examples = []
        res.examples = [e.to_dict() for e in examples]

        try:
            if self.llm is None:
                self._run_from_memory(res, examples)
            else:
                self._run_with_llm(res, question, examples, history)
        except Exception as e:  # never let the API 500 on a model/network hiccup
            log.exception("ask failed")
            res.status = "ERROR"
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                res.error = ("All configured Gemini keys/models are over their quota right now. "
                             "Add another key to GOOGLE_API_KEYS or try again later.")
            else:
                res.error = f"{type(e).__name__}: {msg[:500]}"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        res.audit_id = self._audit(res)
        return res

    def _run_from_memory(self, res: AskResult, examples: list[Example]) -> None:
        """Offline mode: reuse a very close verified query from Db2 memory."""
        if not examples or examples[0].similarity < 0.8:
            res.status = "NO_LLM"
            res.error = ("No GOOGLE_API_KEY configured and no sufficiently similar verified query in memory. "
                         "Add a key to .env, or try one of the example questions.")
            return
        ex = examples[0]
        res.used_memory_sql = True
        res.notes.append(f"Offline mode: reused verified query #{ex.memory_id} (similarity {ex.similarity}).")
        self._execute_candidate(res, ex.sql, attempt=1)
        if res.status == "OK":
            res.title = ex.question
            res.chart = charts.infer(res.columns, res.rows)
            self._check_grain(res)

    def _check_grain(self, res: AskResult) -> GrainReport:
        try:
            report = self.grain.check(res.sql)
        except Exception as e:  # never block an answer because the checker itself broke
            log.warning("grain check failed: %s", e)
            report = GrainReport(status="skipped", reason=f"checker error: {type(e).__name__}")
        res.grain = report.to_dict()
        res.attempts[-1]["grain"] = report.status
        return report

    def _run_with_llm(self, res: AskResult, question: str, examples: list[Example], history: list[Turn]) -> None:
        feedback: list[tuple[str, str]] = []
        draft = None
        last = self.cfg.max_retries + 1
        for attempt in range(1, last + 1):
            draft = self._draft_sql(question, examples, history, feedback)
            res.title, res.reasoning = draft.title, draft.reasoning
            ok = self._execute_candidate(res, draft.sql, attempt)
            if ok:
                report = self._check_grain(res)
                if report.status == "fail":
                    res.grain_caught.extend(c.to_dict() for c in report.failures)
                    if attempt < last:
                        res.attempts[-1].update(stage="grain", error=report.feedback())
                        feedback.append((draft.sql, report.feedback()))
                        res.status = "ERROR"
                        continue
                    res.notes.append("Db2 grain check: this result double-counts rows (see trace); "
                                     "treat the totals with caution.")
                if not res.rows and attempt < last and not res.attempts[-1].get("empty_checked"):
                    # An empty answer is often a wrong literal ('Smartwatch' vs 'Smartwatches').
                    res.attempts[-1].update(stage="empty", empty_checked=True,
                                            error="Db2 returned 0 rows; re-checking filter values.")
                    feedback.append((draft.sql, "The query ran but Db2 returned 0 rows. Check every string "
                                     "literal against the sample values listed in the schema (exact spelling and "
                                     "case) and the date range of the data. If 0 rows is truly correct, return "
                                     "the same query."))
                    res.status = "ERROR"
                    continue
                res.chart = charts.validate(draft.chart.model_dump(), res.columns, res.rows)
                break
            if res.status == "BLOCKED":
                # A write request or an over-budget query should be reported, not "repaired"
                # into something the user did not ask for.
                break
            feedback.append((draft.sql, res.attempts[-1]["error"]))
        if res.status == "OK" and draft is not None and draft.social is not None:
            res.social = self._social_evidence(draft.social)
        if res.status == "OK":
            try:
                fixed = res.grain_caught if res.grain and res.grain["status"] != "fail" else None
                ins = self._summarize(question, res.sql, res.columns, res.rows, res.social, fixed)
                res.answer, res.follow_ups = ins.answer, ins.follow_ups[:3]
            except Exception as e:
                log.warning("summary failed: %s", e)
                res.notes.append("Summary unavailable (model error); showing raw results.")

    def _social_evidence(self, s: SocialSearch) -> dict | None:
        if not self.social.available():
            return None
        f = SocialFilter(query=s.query, date_from=s.date_from, date_to=s.date_to, category=s.category,
                         subcategory=s.subcategory, brand=s.brand, platform=s.platform)
        try:
            ev = self.social.evidence(f)
        except Exception as e:  # bad model-provided date, embedding quota, ...
            log.warning("social search failed: %s", e)
            try:  # retry without the model's filters, the usual culprit
                f = SocialFilter(query=s.query)
                ev = self.social.evidence(f)
            except Exception as e2:  # noqa: BLE001
                log.warning("social search failed again: %s", e2)
                return None
        return ev.to_dict()

    def _execute_candidate(self, res: AskResult, sql: str, attempt: int) -> bool:
        rec: dict = {"attempt": attempt, "sql": sql}
        res.attempts.append(rec)
        # 1) static guardrails
        try:
            checked = guardrails.check(sql, self.cfg.app_schema, self.cfg.max_rows)
        except guardrails.GuardrailError as e:
            rec.update(stage="guardrail", error=str(e))
            res.status, res.error, res.sql = "BLOCKED", str(e), sql
            return False
        res.sql = checked.sql
        rec["sql"] = checked.sql
        guard_prefixes = ("Added row cap", "Reduced row limit", "Rewrote LIMIT")
        res.notes = [n for n in res.notes if not n.startswith(guard_prefixes)] + checked.notes
        # 2) optimizer: validates the SQL and estimates cost
        try:
            plan: Plan = explain(self.db, checked.sql)
        except Db2Error as e:
            rec.update(stage="explain", error=_short(e))
            res.status, res.error = "ERROR", _short(e)
            return False
        res.plan = plan.to_dict()
        rec["cost"] = plan.total_cost
        if plan.total_cost > self.cfg.max_query_cost:
            msg = (f"Estimated cost {plan.total_cost:,.0f} timerons exceeds the budget of "
                   f"{self.cfg.max_query_cost:,}. Try narrowing the question (date range, filters).")
            rec.update(stage="cost", error=msg)
            res.status, res.error = "BLOCKED", msg
            return False
        # 3) execute as the least-privilege user
        try:
            cols, rows, truncated = self.db.query(checked.sql, read_only=True, max_rows=self.cfg.max_rows)
        except Db2Error as e:
            rec.update(stage="execute", error=_short(e))
            res.status, res.error = "ERROR", _short(e)
            return False
        rec.update(stage="ok", rows=len(rows))
        res.columns, res.rows, res.truncated = cols, rows, truncated
        res.status, res.error = "OK", None
        return True

    def _audit(self, res: AskResult) -> int | None:
        details = json.dumps({
            "attempts": res.attempts, "plan": res.plan, "chart": res.chart,
            "memory_ids": [e["id"] for e in res.examples], "notes": res.notes,
            "grain": res.grain, "grain_caught": res.grain_caught,
            "social": ({k: v for k, v in res.social.items() if k != "posts"}
                       | {"post_ids": [p["id"] for p in res.social["posts"]]}) if res.social else None,
        }, default=str)
        try:
            out = self.db.callproc(f"{self.cfg.app_schema}.LOG_QUERY", [
                res.question[:1000], res.sql or "", res.status,
                float(res.plan["total_cost"]) if res.plan else None,
                len(res.rows), res.elapsed_ms, max(len(res.attempts), 1),
                (res.error or "")[:2000] or None, details, 0,
            ])
            return int(out[-1]) if out and out[-1] is not None else None
        except Db2Error as e:
            log.warning("audit failed: %s", e)
            return None

    # -- verify an existing query ----------------------------------------------
    def verify(self, sql: str) -> dict:
        """Audit a hand-written / dashboard query: guardrails, EXPLAIN, run read-only, grain check."""
        res = AskResult(question="[verify] " + sql.strip()[:200], status="ERROR")
        t0 = time.time()
        if self._execute_candidate(res, sql, attempt=1):
            self._check_grain(res)
            res.chart = charts.infer(res.columns, res.rows)
        res.elapsed_ms = int((time.time() - t0) * 1000)
        if res.grain and res.grain["status"] == "fail":
            res.grain_caught = [c for c in res.grain["checks"] if c["status"] == "fail"]
        res.audit_id = self._audit(res)
        return res.to_dict()

    # -- feedback -------------------------------------------------------------
    def remember(self, question: str, sql: str) -> int:
        """User marked an answer as correct: re-validate, then store in vector memory."""
        checked = guardrails.check(sql, self.cfg.app_schema, self.cfg.max_rows)
        explain(self.db, checked.sql)
        return self.memory.add(question, guardrails.clean(sql), source="USER")


def _short(e: Exception) -> str:
    msg = str(e)
    # ibm_db messages look like: [IBM][CLI Driver][DB2/LINUXX8664] SQL0206N  "X" is not valid ... SQLSTATE=42703
    if "]" in msg:
        msg = msg[msg.rfind("]") + 1:].strip()
    return msg[:600]
