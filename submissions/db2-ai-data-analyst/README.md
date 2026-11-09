# Db2 AI Data Analyst

> **One-line pitch:** the AI analyst that Db2 fact-checks. Ask a question in plain English and get a chart, a short answer and the exact SQL. Db2 validates and cost-checks the SQL before it runs, **proves the totals are not inflated by a bad join**, and for *why* questions pulls in **what customers are saying on Reddit, X and review sites** with one hybrid vector + relational query.

**The wrong-number problem.** Generated SQL can run without an error and still be wrong. Asked for *"total shipping fees by product category"*, a model typically joins `ORDERS` to `ORDER_ITEMS` and sums `SHIPPING_FEE`, which counts each order's fee once per order line. On this dataset the query returns **₹10.99 L instead of ₹5.55 L (1.98× inflated)**. This app runs two Db2 control queries over the same joins and catches the mismatch. If the model wrote the query, Db2's evidence goes back to it and only the corrected result is shown. The same check works on **any existing report or dashboard query**: paste it into the *Verify* tab and Db2 proves whether its totals are right.

**The why problem.** SQL tells you *what* happened ("smartwatch returns jumped to 38% in Mar–Jul 2026"). The customer posts stored next to the sales data in Db2 tell you *why* ("strap snapped after two weeks"), lined up month by month.

![Answer card with chart and Db2 trace](docs/screenshots/01-answer.jpg)

---

## Table of Contents
- [Problem Statement](#problem-statement)
- [Solution Overview](#solution-overview)
- [IBM Db2 Integration](#ibm-db2-integration)
- [AI / LLM Architecture](#ai--llm-architecture)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Setup & Installation](#setup--installation)
- [Configuration](#configuration)
- [Running the Project](#running-the-project)
- [Testing & Verification](#testing--verification)
- [Screenshots & Demo](#screenshots--demo)
- [Known Limitations & Future Roadmap](#known-limitations--future-roadmap)
- [Team Members & Credits](#team-members--credits)

---

## Problem Statement

- Business teams have questions like *"Which product line is driving returns this year?"* or *"Is the new store pulling its weight?"*. Today each one becomes a ticket to a data team, and the answer arrives days later.
- Generic text-to-SQL tools get the answer quickly, but they are risky on an enterprise database:
  - they can write `DELETE`s or run a cross-join that slows everyone down;
  - they make up column names;
  - **their SQL can run cleanly and still return a wrong total** when a join repeats rows before a `SUM`;
  - they only see the numbers, never the customer feedback that explains them;
  - they don't learn from their mistakes;
  - nobody can audit what they ran.
- **Target users:** business analysts, managers and operations staff who know their business but not SQL, and the DBAs who need to trust whatever tool those users are given.

## Solution Overview

The app is an analyst copilot. Each question goes through these steps:

1. **Memory lookup:** finds similar questions that were already answered and verified. These are stored as vectors in Db2.
2. **SQL generation:** Gemini writes Db2 SQL (through LangChain structured output). The schema it sees is read live from the Db2 catalog, including the `COMMENT ON` descriptions.
3. **Guardrails:** checks that the query is a single, read-only statement with a row cap.
4. **Db2 EXPLAIN:** the optimizer checks the SQL and estimates its cost. A query over the cost budget is blocked before it runs.
5. **Read-only execution:** the query runs as a Db2 user that only has `SELECT` grants.
6. **Db2 grain check:** for every `SUM` / `COUNT` / `AVG` in a join query, the app finds the table the value comes from and asks Db2 for the same aggregate over *distinct* primary keys of that table. If the two totals differ, the join double-counted rows. See [Db2 grain check](#db2-grain-check-catching-wrong-numbers).
7. **Self-correction:** if Db2 rejects the SQL (for example `SQL0206N … SQLSTATE=42703`) or the grain check fails, Db2's evidence goes back to the model and it tries again. A result with 0 rows is sent back once so the model can re-check its filter values against the catalog's sample values (for example `'Smartwatch'` vs `'Smartwatches'`).
8. **Customer voice:** for *why* questions the model also requests social evidence. One Db2 statement ranks posts by `VECTOR_DISTANCE` while filtering on date, product and platform, and another groups the matches by month. See [Voice of the customer](#voice-of-the-customer-social-posts-in-db2).
9. **Insight:** a chart is chosen automatically, Gemini writes a 2–4 sentence answer that cites the actual numbers (and the customer posts when present), and suggests 3 follow-up questions.
10. **Audit:** every request is logged through a Db2 stored procedure.
11. **Learning:** when you click 👍, the question and SQL are re-checked and stored as a `VECTOR` embedding for future questions.

### Db2 grain check: catching wrong numbers

For a query such as

```sql
SELECT p.CATEGORY, SUM(o.SHIPPING_FEE) AS FEES
FROM RETAIL.ORDERS o JOIN RETAIL.ORDER_ITEMS i ON i.ORDER_ID = o.ORDER_ID
JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = i.PRODUCT_ID GROUP BY p.CATEGORY
```

`grain.py` parses the SQL (sqlglot), sees that `SHIPPING_FEE` belongs to `ORDERS` (primary key `ORDER_ID`), and runs two control queries in Db2 as the read-only user, over exactly the same `FROM` / `JOIN` / `WHERE`:

```sql
SELECT COUNT(*) AS JOINED_ROWS, SUM(o.SHIPPING_FEE) AS NAIVE_VALUE FROM <same joins and filters>;
SELECT COUNT(*) AS SOURCE_ROWS, SUM(ZZG.ZZV) AS EXACT_VALUE
FROM (SELECT DISTINCT o.ORDER_ID AS ZZK0, o.SHIPPING_FEE AS ZZV FROM <same joins and filters>) AS ZZG;
```

Db2 answers 51,445 source orders, 101,979 joined rows, ₹10.99 L naive against ₹5.55 L exact. The check fails, the agent tells the model what Db2 found, the model aggregates `ORDERS` in a CTE before joining, and the answer card says *"Db2 caught a wrong number … 1.98× inflated. The query was rewritten and re-verified."* Gemini 2.5 often avoids this trap by pre-aggregating in a CTE, in which case the check reports *skipped* instead of claiming a verification it did not do. To audit queries written by people or BI tools, the **Verify** tab (`POST /api/verify`) runs any pasted `SELECT` through the guardrails, EXPLAIN, a read-only run and the grain check, and audits it like a question. Correct joins (for example `V_SALES` joined to `PRODUCTS`) pass and get a *"Totals verified by Db2"* badge. `COUNT(*)`, `COUNT(DISTINCT …)` and CTE-based queries are grain-safe or skipped rather than guessed. Every catch is stored in the audit row and counted with `JSON_EXISTS(DETAILS, '$.grain_caught[0]')`.

### Voice of the customer: social posts in Db2

- `RETAIL.SOCIAL_POSTS` holds posts from **Reddit, X and review sites**: platform, subreddit or handle, timestamp, text, linked `PRODUCT_ID` / `STORE_ID` / brand, topic, a sentiment score from -1 to +1, and engagement. It is an ordinary table, so the model can also query it with SQL (*"monthly post volume and sentiment by platform"*).
- Gemini embeddings of each post are stored in `ANALYST.SOCIAL_EMBEDDINGS` as `VECTOR(768, FLOAT32)`. The read-only query user cannot see that table.
- For a *why* question the model returns a `social` search request with its SQL (semantic phrase plus optional date range, category, brand and platform). The app then runs:

```sql
SELECT sp.PLATFORM, sp.CHANNEL, sp.POST_DATE, sp.SENTIMENT, sp.POST_TEXT, p.PRODUCT_NAME,
       VECTOR_DISTANCE(e.EMBEDDING, VECTOR(CAST(? AS CLOB(1M)), 768, FLOAT32), COSINE) AS DIST
FROM ANALYST.SOCIAL_EMBEDDINGS e
JOIN RETAIL.SOCIAL_POSTS sp ON sp.POST_ID = e.POST_ID
LEFT JOIN RETAIL.PRODUCTS p ON p.PRODUCT_ID = sp.PRODUCT_ID
WHERE sp.POST_DATE BETWEEN ? AND ? AND UPPER(p.SUBCATEGORY) = UPPER(?)
ORDER BY DIST FETCH FIRST 18 ROWS ONLY
```

  plus a `GROUP BY VARCHAR_FORMAT(POST_DATE, 'YYYY-MM')` over the semantic matches for a mentions and sentiment timeline. The answer card shows that timeline next to the closest posts, and the summary connects them to the numbers.
- **Where the posts come from:**
  - `src/ingest_social.py reddit "<query>" --subreddit r/...` uses the official Reddit OAuth API (free "script" app credentials in `.env`).
  - `src/ingest_social.py x "<query>"` uses the X API v2 recent-search endpoint (needs a paid X API tier).
  - `src/ingest_social.py file export.csv` imports CSV/JSON exports from any platform or social-listening tool.
  - Each import links posts to products, stores and brands from the Db2 catalog, scores sentiment and topic, and embeds the text in batches.
- **Demo data:** the retailer and its brands are fictional, so real posts never mention them. `setup_db.py` loads about 1,270 deterministic posts written to match the sales storylines: the Kestrel smartwatch strap and battery complaints peaking in Apr–May 2026, festive-season delivery delays and sale chatter, the Mathura store opening buzz followed by queue complaints, more app and UPI posts as online grows, and Fashion sizing complaints. No scraping is involved.

Other features:

- **Social tab:** platform stats and a semantic search box over the posts.
- **Verify tab:** paste any report or dashboard SQL and get Db2's verdict on its totals (a double-counting example is preloaded).
- **Follow-up questions** keep context, e.g. *"now split that by region"*.
- **"How Db2 answered this" panel:** shows memory hits with similarity scores, every SQL attempt, the final SQL, the EXPLAIN cost and plan operators (`TBSCAN`, `IXSCAN`, `HSJOIN` …), and the audit id.
- **Sidebar:** history (read from the Db2 audit table), the vector memory (entries can be deleted) and a live schema browser.
- **CSV export**, plus light and dark themes.
- **Offline mode:** without an API key, a local embedder takes over and questions that closely match a verified memory still get answered, so the Db2 features can be demonstrated without any key.

## IBM Db2 Integration

- **Db2 edition and environment:** IBM Db2 Community Edition **12.1.5** in Docker (`icr.io/db2_community/db2`), started with the included `docker-compose.yml`. The native `VECTOR` type needs 12.1.2 or later.
- **Connector / driver:** the native **`ibm_db`** Python driver, using prepared statements with parameter markers, `execute_many` batch loading, `callproc` with OUT parameters, explicit transactions, and two connections with different privilege levels.
- **Database schema / data models:**
  - **`RETAIL` schema (the data being analysed):** seven tables (`REGIONS`, `STORES`, `CUSTOMERS`, `PRODUCTS`, `ORDERS`, `ORDER_ITEMS`, `SOCIAL_POSTS`) with PK, FK, UNIQUE and CHECK constraints, 7 secondary indexes, and the reporting view `V_SALES`, which computes net revenue and gross profit per order line (grain `ORDER_ID, LINE_NO`). Every object has a `COMMENT ON` description.
  - **`ANALYST` schema (the app's own data):**
    - `QUERY_MEMORY` stores each verified question and its SQL with a **`VECTOR(768, FLOAT32)`** embedding.
    - `SOCIAL_EMBEDDINGS` stores a **`VECTOR(768, FLOAT32)`** embedding per social post (FK to `RETAIL.SOCIAL_POSTS`, `ON DELETE CASCADE`).
    - `QUERY_AUDIT` logs every request.
    - The SQL PL procedure **`LOG_QUERY`** writes audit rows and returns the new id through an `OUT` parameter.
    - The view `V_AUDIT_DAILY` feeds the usage stats.
  - **Dataset:** about 52k orders, 103k order lines and 1,270 social posts, generated with fixed seeds. The data includes a festive-season peak, the online channel growing over time, a store that opened mid-period, and a smartwatch return spike in 2026, each with matching customer conversation.
- **Key queries and operations:**

| What | How |
|---|---|
| Semantic retrieval | `VECTOR_DISTANCE(EMBEDDING, VECTOR(CAST(? AS CLOB(1M)), 768, FLOAT32), COSINE) … ORDER BY DIST FETCH FIRST 4 ROWS ONLY` |
| Hybrid social search | `VECTOR_DISTANCE` ranking joined to `SOCIAL_POSTS`, `PRODUCTS`, `STORES` with date / category / brand / platform predicates, and a monthly `GROUP BY` over the semantic matches |
| Grain check (wrong-number detection) | Control queries over the generated `FROM`/`JOIN`/`WHERE`: `AGG(expr)` vs `AGG(expr)` over `SELECT DISTINCT <primary key>, expr`, run as the read-only user |
| Audit analytics | `JSON_EXISTS(DETAILS, '$.grain_caught[0]')` over the audit CLOB |
| Pre-execution validation and cost guardrail | `EXPLAIN PLAN SET QUERYNO = n FOR <sql>`, then reads `EXPLAIN_STATEMENT.TOTAL_COST`, `EXPLAIN_OPERATOR` and `EXPLAIN_STREAM` |
| Schema grounding | `SYSCAT.TABLES`, `SYSCAT.COLUMNS`, `SYSCAT.REFERENCES` (+ `REMARKS`) and distinct sample values for low-cardinality columns |
| Least privilege | `GRANT CONNECT` and `GRANT SELECT ON RETAIL.*` to `ANALYST_RO`. Generated SQL runs only on that connection |
| Audit trail | `CALL ANALYST.LOG_QUERY(?, …, ?)` with an OUT `AUDIT_ID` |
| Optimizer statistics | `CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE … WITH DISTRIBUTION AND DETAILED INDEXES ALL')` |
| Explain infrastructure | `CALL SYSPROC.SYSINSTALLOBJECTS('EXPLAIN','C',…)` |

## AI / LLM Architecture

- **Framework:** **LangChain** (`langchain-google-genai`): `ChatGoogleGenerativeAI.with_structured_output()` with Pydantic schemas, plus `GoogleGenerativeAIEmbeddings`.
- **Model provider and models:** Google **Gemini 2.5 Flash** writes the SQL and the summaries. **`gemini-embedding-001`** produces the 768-dimension question embeddings. Both are configurable in `.env`.
- **Fallback handling:** if a key or model is rate-limited (429 / quota), LangChain `with_fallbacks` tries every configured key (`GOOGLE_API_KEYS`) on the primary model, then on `GEMINI_FALLBACK_MODELS` (default `gemini-2.5-flash-lite`). Embeddings fail over across keys in the same way. Failures in memory search or the summary step degrade gracefully and never break the answer.
- **How Db2 context reaches the model:**
  - The prompt is built from the **Db2 catalog** (schema, comments and sample values) plus the **nearest verified examples** found by Db2 vector search.
  - When a query fails, Db2's **EXPLAIN or runtime error text** goes back into the conversation so the model can fix it.
  - When the **grain check** fails, Db2's control-query numbers (source rows, joined rows, naive and exact totals) go back to the model as a correction.
  - The **result rows** from Db2, and for *why* questions the **matching customer posts and their monthly timeline**, are passed to a second structured call that writes the answer and the follow-up questions.
  - If the model's chart choice doesn't match the real result columns, `charts.validate()` replaces it.
- Full design: **[architecture.md](architecture.md)**

## Tech Stack

- **Backend / core:** Python 3.11, FastAPI, Uvicorn, Pydantic v2, sqlglot (SQL parsing for the grain check), httpx (connectors)
- **Database:** IBM Db2 Community Edition 12.1.5 (Docker)
- **Frontend / UI:** vanilla HTML/CSS/JS, Chart.js 4, IBM Plex fonts
- **AI:** LangChain, Google Gemini 2.5 Flash, gemini-embedding-001
- **Tooling:** Docker Compose, pytest

## Prerequisites

- **Docker** (Docker Desktop on Windows or macOS) with at least 4 GB of RAM available for Db2.
- **Python 3.10 or later**
- A **Google Gemini API key**. A free key is available at <https://aistudio.google.com/apikey>. Without one the app still runs in offline mode.

## Setup & Installation

```bash
# 1. Go to the submission folder
cd submissions/db2-ai-data-analyst

# 2. Python environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configuration (set DB2_PASSWORD, DB2_RO_PASSWORD and GOOGLE_API_KEY)
cp .env.example .env               # Windows: copy .env.example .env

# 4. Start Db2. The first boot takes about 5-10 minutes while the database is created.
docker compose up -d
docker logs -f db2-analyst         # wait for "(*) Setup has completed."

# 5. Create the schema, load the data, run RUNSTATS, create the EXPLAIN tables
#    and the read-only user, and seed the vector memory
python src/setup_db.py
```

`setup_db.py` can be run again safely: on an existing database it upgrades in place (creates missing objects, loads the social posts if absent, refreshes grants and memory). Use `--reset` to rebuild everything, `--memory` to rebuild only the vector memory, or `--social` to reload the seeded posts. Embedding the posts on the Gemini free tier takes a few minutes because of its per-minute limit; the setup waits and rotates keys automatically.

> **Already have Db2 elsewhere (Db2 on Cloud or an existing instance)?** Skip step 4 and point the `DB2_*` variables at it. Use `DB2_SECURITY=SSL` for Db2 on Cloud. The read-only user step is skipped automatically if the Docker container isn't available.

## Configuration

| Variable | Purpose |
|---|---|
| `DB2_HOST`, `DB2_PORT`, `DB2_DATABASE`, `DB2_USER`, `DB2_PASSWORD` | Admin connection. `docker-compose.yml` reads the same values |
| `DB2_RO_USER`, `DB2_RO_PASSWORD` | The least-privilege user that runs generated SQL |
| `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GEMINI_EMBED_MODEL`, `EMBED_DIM` | Gemini settings |
| `GOOGLE_API_KEYS`, `GEMINI_FALLBACK_MODELS` | Optional extra keys and fallback models used on quota errors |
| `MAX_QUERY_COST` | EXPLAIN cost budget in timerons. Queries estimated above it are blocked |
| `MAX_ROWS`, `MAX_SQL_RETRIES`, `MEMORY_K` | Row cap, self-correction attempts, number of few-shot examples |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `X_BEARER_TOKEN` | Optional social connectors for `src/ingest_social.py` |

> **Security warning:** never commit `.env`. It is gitignored, and `.env.example` contains placeholders only.

## Running the Project

```bash
python src/app.py
# open http://localhost:8000
```

Try these:

- *Why did smartwatch returns spike in 2026?* (return-rate SQL plus the matching Reddit / X / review posts and their timeline)
- *Total shipping fees collected by product category*: if the model joins before summing, watch Db2 catch the double-counted fees
- **Verify tab** → *Verify with Db2* on the preloaded dashboard query: ₹10.99 L reported vs ₹5.55 L exact
- *What are customers complaining about during the festive season?*
- *Show monthly revenue and gross profit trend*
- *Which subcategory had the highest return rate in 2026?* (should surface the smartwatch return spike)
- *How did online vs store revenue share change by quarter?*
- *How is the new Mathura store doing compared to other North stores?*
- then a follow-up: *now only for 2026*
- *Delete all cancelled orders* (blocked by the guardrails)

The API can also be used directly:

```bash
curl -X POST localhost:8000/api/ask -H "Content-Type: application/json" \
     -d '{"question": "Top 10 products by revenue this year"}'
```

## Testing & Verification

```bash
pytest tests -q
```

- `test_guardrails.py` (22 tests) checks that DML, DDL, `CALL`, multiple statements, writes hidden in comments or CTEs, admin views and app-schema access are all blocked, and that `LIMIT` is rewritten and row caps applied.
- `test_charts_embeddings.py` checks the chart heuristics, the offline embedder and the consistency of the generated dataset.
- `test_db2_integration.py` runs against the **live Db2** and is skipped if Db2 can't be reached. It checks that:
  - every seed query passes EXPLAIN and runs as the read-only user;
  - EXPLAIN catches a bad column (`SQLSTATE 42703`);
  - **the read-only user cannot write, and cannot read the app schema**;
  - vector search returns the right neighbour;
  - the stored procedure returns its OUT parameter;
  - the catalog prompt contains the comments and sample values.

- `test_config_fallbacks.py` checks key and model de-duplication and the order of the fallback chain.
- `test_grain.py` checks control-query planning (grain table resolution through aliases and the catalog, view grain, Db2 idioms such as `CURRENT DATE - 12 MONTHS` surviving the rewrite, CTEs and derived tables skipped), and against live Db2 that the inflated shipping-fee query **fails** and a correct join **passes**.
- `test_grain.py` also runs `/api/verify`'s code path on the dashboard query (flagged and audited) and checks that view columns carry sample values for filters.
- `test_social.py` checks sentiment and topic labelling, entity linking, the seeded storylines, the Reddit and X connectors (mocked HTTP), CSV/JSON import, and against live Db2 that the hybrid search finds the Kestrel complaints with a timeline that peaks in Mar–Jul 2026.

Current result: **82 passed** (live Db2 tests included).

To check the Db2 side by hand:

```sql
-- vector memory
SELECT MEMORY_ID, QUESTION, SOURCE, TIMES_USED FROM ANALYST.QUERY_MEMORY;
-- audit trail written by the stored procedure
SELECT AUDIT_ID, QUESTION, STATUS, EST_COST, ATTEMPTS, ELAPSED_MS FROM ANALYST.QUERY_AUDIT ORDER BY AUDIT_ID DESC;
-- social posts and their vectors
SELECT PLATFORM, COUNT(*), AVG(SENTIMENT) FROM RETAIL.SOCIAL_POSTS GROUP BY PLATFORM;
SELECT COUNT(*) FROM ANALYST.SOCIAL_EMBEDDINGS;
-- answers where the grain check caught an inflated total
SELECT AUDIT_ID, QUESTION FROM ANALYST.QUERY_AUDIT WHERE JSON_EXISTS(DETAILS, '$.grain_caught[0]');
-- grants for the read-only user
SELECT TABNAME, SELECTAUTH, INSERTAUTH, DELETEAUTH FROM SYSCAT.TABAUTH WHERE GRANTEE = 'ANALYST_RO';
```

## Screenshots & Demo

- **Demo video:** _link added after recording_
- Screenshots are in `docs/screenshots/`:

| | |
|---|---|
| ![Answer](docs/screenshots/01-answer.jpg) | ![Trace](docs/screenshots/02-trace.jpg) |
| Chart, answer and follow-up suggestions | EXPLAIN plan, vector-memory hits, self-correction log |
| ![Blocked](docs/screenshots/03-blocked.jpg) | ![Memory](docs/screenshots/04-memory.jpg) |
| A write attempt blocked by the guardrails | Vector memory and history read from Db2 |

## Known Limitations & Future Roadmap

**Current limitations:**
- The guardrails are keyword-based. The read-only Db2 user is the real security boundary, and the guardrails sit in front of it.
- The EXPLAIN cost budget is one global number. It doesn't vary by user or workload.
- The single-user demo has no login, so history and memory are shared.
- The dataset is synthetic, including the social posts (the brands are fictional, so live posts would not match them). The Reddit and X connectors are tested against mocked API responses only.
- The grain check covers top-level `SUM` / `COUNT` / `AVG` over joins of base tables and `V_SALES`. Queries that pre-aggregate in CTEs or derived tables are reported as *skipped*, not verified.
- Sentiment for imported posts uses a small lexicon; the seeded posts carry their own labels.

**Next steps:**
- Map app users to Db2 users or roles, and use **Row and Column Access Control (RCAC)** so each user only sees permitted rows.
- Run long queries under **Db2 Workload Manager** thresholds in addition to the EXPLAIN check.
- Create vector indexes on `QUERY_MEMORY` and `SOCIAL_EMBEDDINGS` once they grow large.
- Schedule the connectors and alert when a product's negative-post volume jumps (a Db2 Administrative Task Scheduler job).
- Add a "suggest an index" action that uses the Db2 Design Advisor (`db2advis`) for slow plans.
- Add support for Db2 on Cloud and watsonx.ai Granite models.

## Team Members & Credits

| Name | GitHub Handle | Role / Contribution |
|---|---|---|
| Darshit | [@Darshit42](https://github.com/Darshit42) | Design, Db2 integration, AI workflow, UI |

**Third-party licences:** `ibm_db` (Apache-2.0), sqlglot (MIT), httpx (BSD-3), FastAPI (MIT), Uvicorn (BSD-3), Pydantic (MIT), LangChain / langchain-google-genai (MIT), python-dotenv (BSD-3), Chart.js (MIT), IBM Plex fonts (OFL-1.1). IBM Db2 Community Edition is used under its own licence terms.
