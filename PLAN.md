# Scenario 3 — Multi-Agent Research System — Plan

**Folder:** `Multi_agent_system/`
**Exam:** CCA-F, Scenario 3 (Multi-Agent Research System)
**Built on:** Claude Agent SDK, Python 3.11+, Streamlit
**Commands and expected output:** `cmd.txt`
**Status:** built, and **verified against a live run** — see §15 for what that run changed.

**One line:** a research assistant that takes a complex question, splits it into subtopics,
runs one specialized agent per subtopic at the same time, and produces a **cited,
conflict-aware** report saved to `output/research.md`.

**Sibling build:** `Customer_support_agent/`. That build's lesson was *deterministic
enforcement beats prompt guidance*. **This one is different:**

> **An agent inherits nothing. Context, provenance, and failure all have to be carried
> explicitly — or they are silently lost.**

---

## 1. The problem it actually solves

Ask a normal assistant *"What's the EV battery market size 2024–2026? Who agrees, who
doesn't?"* and you get:

- **one answer**, usually from one source
- **conflicts hidden** — you never learn one analyst says $91B and another says $71B
- **provenance lost** — you can't trace which claim came from where
- **silent failure** — if a lookup hit a paywall, nobody told you

Plausible, and unusable for an investor deck or a policy memo. This build fixes all four,
and each fix is a domain the exam tests.

---

## 2. Domain weightage for this scenario

| Domain | Weight | Why this scenario loads it |
|---|---|---|
| **D1 — Agentic Architecture & Orchestration** | **40%** | Hub-and-spoke, subagent spawning, context isolation, parallel dispatch. This *is* the scenario. |
| **D5 — Context Management & Reliability** | **30%** | Provenance, structured error propagation, coverage-gap detection, re-dispatch. |
| **D4 — Prompt Engineering & Structured Output** | **15%** | One JSON contract for four agents; goal + quality criteria, not procedural steps. |
| **D2 — Tool Design & MCP Integration** | **10%** | Per-agent tool grants, enforced per session. |
| **D3 — Configuration & Workflows** | **5%** | Thin on purpose — the Feature Review build covers D3 at 30%. |

**D1 + D5 = 70% of this scenario.**

---

## 3. What is covered, and where it lives

### D1 — Agentic Architecture (40%)

| Concept | Where | Anti-pattern you can run |
|---|---|---|
| Hub-and-spoke | `coordinator.py: run_research()` — the hub; every agent returns to it | let agents call each other |
| Orchestrator-workers | the hub holds no research capability; `agents/*.py` hold it all | one agent with all tools (super-agent) |
| Per-agent tool grant | `coordinator.py: agent_options()` sets `tools` **and** `allowed_tools` per session | one shared tool surface |
| Task decomposition | `plan_subtopics()` + `models.py: check_decomposition()` (5+ subtopics) | `--narrow` — one angle, called coverage |
| Parallel dispatch | `asyncio.gather` over one session per subtopic | `--sequential` |
| Separation of concerns | `agents/synthesis.py: TOOLS = ["Read"]` — no search tool exists for it | give it search and it re-researches |
| Measured, not claimed | `ResearchResult.parallel_saving` — "130s of work in 103s of waiting" | trusting a transcript that says "in parallel" |

### D5 — Context & Reliability (30%)

| Concept | Where | Anti-pattern you can run |
|---|---|---|
| **No inheritance** | `collect_prompt()` / `synthesis_prompt()` build each agent's whole world | "continue from the findings above" — there is no above |
| Provenance | `models.py: Finding` — `source_url`, `source_title`, `timestamp`, `retrieved_at`, `confidence` | fold the source into the claim text |
| Two dates, not one | `timestamp` (source published) vs `retrieved_at` (we fetched) | conflate them; a 2019 figure passes for current |
| Structured errors | `models.py: WorkerError` | `--silent-errors` |
| Silent failure caught | `parse_worker_output()` — findings=[] + errors=[] → `empty_result` | remove it and a timeout reads as coverage |
| Never crash the run | `run_agent()` catches everything; a dead session becomes a typed error | one agent's exception loses the other five |
| Real timeouts | `AGENT_TIMEOUT_S` → a genuine `timeout` error, retryable | hang forever |
| access_failure ≠ empty_result | `models.py: BLOCKS_COVERAGE` | collapsing both into "no data" |
| Coverage gaps | `coverage_notes()`, graded against the query file's list | let the system grade its own coverage |

### D4 — Prompt Engineering & Structured Output (15%)

- **One contract, four agents** — `agents/__init__.py: CONTRACT`.
- **Refusal is part of the contract** — "findings: [] together with errors: [] is INVALID".
- **Goal + quality criteria, not procedure** — each `agents/*.py` prompt says what good
  output is and what the agent must *not* do.
- **Five required headings** in `agents/report_generation.py`, printed even when empty.

### D2 — Tool Design (10%)

```
web_search         ["WebSearch", "WebFetch", "Read"]
document_analysis  ["Read", "Grep", "Glob"]     cannot search
synthesis          ["Read"]                     NO search tools
report_generation  ["Read"]                     NO search tools
```

Enforced in `agent_options()` by setting **both** `tools` (what exists) and `allowed_tools`
(what is auto-approved). Verified live: the synthesis agent's tool list came back empty.

### D3 — Configuration (5%)

Thin on purpose. `CLAUDE.md` hierarchy, `.claudeignore` vs `permissions.deny`,
`setting_sources`, skills vs commands — that is the Feature Review scenario's material.

---

## 4. Architecture

```
              query file (TOPIC / TIMEFRAME / SUBTOPICS)
                            │
                 ┌──────────▼──────────┐
                 │   HUB (coordinator) │   holds NO research tools
                 │ plan → dispatch →   │   asyncio.gather
                 │ collect → check     │
                 └──┬──────┬──────┬────┘
      all at once   │      │      │
                    │      │      │
            ┌───────▼─┐ ┌──▼─────┐ ┌▼────────┐ ┌───────────┐
            │  WEB    │ │  DOC   │ │SYNTHESIS│ │  REPORT   │
            │ SEARCH  │ │ANALYSIS│ │         │ │           │
            │WebSearch│ │Read    │ │Read     │ │Read       │
            │WebFetch │ │Grep    │ │NO SEARCH│ │NO SEARCH  │
            │Read     │ │Glob    │ │         │ │           │
            └────┬────┘ └───┬────┘ └────┬────┘ └─────┬─────┘
                 └──── JSON findings / errors ───────┘
                            │
                    output/research.md
```

Each box is **its own session** with its own tool grant and its own prompt. They share
nothing — which is why every prompt has to carry its whole world.

---

## 5. What the hub does, step by step

| Step | What happens | Where |
|---|---|---|
| **1. READ** | parse the query file: TOPIC, TIMEFRAME, subtopics to cover | `load_query()` |
| **2. DECOMPOSE** | subtopics from the file, or planned by an agent when you type a free question | `plan_subtopics()`, checked by `check_decomposition()` |
| **3. DISPATCH** | one session per subtopic, `asyncio.gather` — plus the document agent if `documents/` has files | `run_research()` |
| **4. COLLECT** | each agent's JSON → typed findings and errors | `parse_worker_output()` |
| **5. CHECK** | conflicts, coverage gaps, what is worth retrying | `find_conflicts()`, `coverage_notes()`, `needs_redelegation()` |
| **6. REPORT** | synthesis reconciles → report agent writes → saved to `output/` | `run_agent()`, `save_output()` |

**Steps 4 and 5 are code, not prompts.** Asking a model "did you cover everything?" in the
session that produced the plan gets you "yes" — it is reviewing its own reasoning.

---

## 6. The four agents

| Agent | Tools | Does | Must not |
|---|---|---|---|
| **web_search** | WebSearch, WebFetch, Read | researches ONE subtopic; prefers primary sources; fetches before quoting; records the source's own date; stops at 3 sources | quote a search snippet; guess a date; chase other subtopics |
| **document_analysis** | Read, Grep, Glob | pulls figures out of `documents/*.txt`; Grep/Glob before reading; copies numbers with units; carries page/section | search the web; paraphrase a number |
| **synthesis** | Read *(no search)* | groups findings; splits well-established vs contested; keeps every source; says why sources differ | average conflicting estimates; drop a source while shortening |
| **report_generation** | Read *(no search)* | writes the report, five required headings | add claims not in its input; omit an empty heading |

```
## Summary                    3-5 sentences, every number cited
## Well-established findings  where the sources agree
## Contested findings         each disagreement, every position, with its source
## Coverage notes             what was NOT reached, and why
## Sources                    numbered, deduplicated
```

---

## 7. The data contract

```python
# models.py
Finding      claim, evidence, source_url, source_title, timestamp, retrieved_at,
             confidence, worker_id, subtopic
WorkerError  error_type, message, attempted_action, is_retryable, partial_results,
             worker_id, subtopic
AgentRun     role, subtopic, text, tools_used, elapsed, cost_usd, failure

error_type ∈ access_failure | empty_result | timeout | rate_limit | parse_failure | tool_denied
```

`access_failure` vs `empty_result` is the distinction the exam asks about: the first means
*we never got to look*, the second means *we looked and there is nothing*.

**Query file format** (`sample_queries/*.txt`):

```
TOPIC: EV battery market size, and where analyst estimates disagree
TIMEFRAME: 2024-2026
SUBTOPICS:
- global market size estimates by year
- ...
```

### Key functions in `models.py`

| Function | Does |
|---|---|
| `make_finding()` | **raises** if `source_url` is blank or a placeholder like "various sources" |
| `parse_worker_output()` | agent JSON → `(findings, errors)`; bad finding → `parse_failure` with the payload kept; empty+empty → explicit `empty_result` |
| `find_conflicts()` | groups by the **question** (subtopic + content words, numbers stripped) and flags groups whose numbers disagree |
| `lead_of()` | lead figure: highest confidence, then most recent. Presentation only — the others stay |
| `split_findings()` | → `(well_established, contested)` |
| `check_decomposition()` | fewer than 5 subtopics, duplicates, one-word subtopics |
| `coverage_notes()` | subtopics planned but never answered |
| `needs_redelegation()` | worth one more try? Only if something **retryable** failed |
| `ResearchResult.parallel_saving` | "N agents: X s of work in Y s of waiting" — parallelism in seconds |

---

## 8. Files

```
Multi_agent_system/
├── cmd.txt                          how to run, what to expect, every function explained
├── PLAN.md                          this file
├── models.py                        the data + the checks
├── coordinator.py                   the hub + CLI
├── app.py                           Streamlit UI
├── agents/
│   ├── __init__.py                  shared JSON contract + build() + tool_table()
│   ├── web_search.py                agent 1
│   ├── document_analysis.py         agent 2
│   ├── synthesis.py                 agent 3 -- no search tools
│   └── report_generation.py         agent 4 -- no search tools
├── sample_queries/                  5 query files, one per use case in §10
├── documents/                       YOUR .txt files -- the document agent reads these
│   ├── README.md                    why .txt only, and what the samples show
│   └── sample_*.txt                 3 fictional samples for class demos
├── output/                          every finished run
│   ├── research.md                  the latest report, stable path
│   └── <timestamp>_<topic>/         research.md + findings.json per run
├── tests/                           23 tests, offline
├── pyproject.toml
└── .env.example
```

---

## 9. How to run it

**Run everything from inside this folder** — `uv` looks for `pyproject.toml` and `.venv`
where you are standing; one folder up you get `error: Failed to spawn: streamlit`.

```bash
cd Multi_agent_system
uv venv
uv pip install claude-agent-sdk python-dotenv streamlit pytest
cp .env.example .env          # then add ANTHROPIC_API_KEY
```

| Command | Key? | Does |
|---|---|---|
| `uv run coordinator.py --demo` | no | the four checks, offline, ~1s |
| `uv run pytest -q` | no | 23 tests |
| `uv run streamlit run app.py` | Run tab does | the interface |
| `uv run coordinator.py query_ev_batteries.txt` | yes | a real run |

**Flags:** `--demo`, `--sequential`, `--silent-errors`, `--narrow`, `--model`.
**Exit code:** 0 when coverage is 100%, 1 otherwise.
**Output:** `output/<timestamp>_<topic>/research.md` + `findings.json`, and the latest
report is always copied to `output/research.md`.

**Per-agent caps** (`coordinator.py`): `AGENT_MAX_TURNS=20`, `AGENT_BUDGET_USD=0.30`,
`AGENT_TIMEOUT_S=240`. A timeout becomes a real, retryable, structured error.

**Measured cost**, same query before and after the token pass:

| | Before | After |
|---|---|---|
| whole run | $0.3173 / 168s | **$0.0941 / 94s** |
| one web_search agent | $0.262 / 103s | **$0.044 / 27s** |

What did it: `thinking={"type": "disabled"}` on the collectors (thinking is billed as
output, and there is one collector per subtopic); at most 2 fetches and 2 findings each;
per-agent `MAX_TURNS`; a trimmed `CONTRACT`; and a lean synthesis payload (7 fields,
evidence capped at 120 chars, instead of every field of every finding). Synthesis and the
report writer keep thinking — they run once, hold no tools, and do the only hard judgement.

A 6-subtopic run is roughly **$0.30–0.60 and 1–2 minutes**.

**The UI**, two tabs:

- **Run** — query-file dropdown, `.txt` upload box for your own documents, live log, then
  metrics (coverage / findings / conflicts / time / cost), the parallelism measurement,
  **the report itself with a download button and its saved path**, an *Agent sessions*
  table showing each agent's time, cost and tools used, coverage notes, contested
  findings, well-established findings, errors, sources, and a list of past reports.
- **Learn** — the four checks as tables. No API key needed.

---

## 10. The five sample queries, and the use case behind each

| Query file | Use case | What would go wrong without this |
|---|---|---|
| `query_ev_batteries.txt` | startup sizing a market for an investor deck | averaging a $91B estimate with a $71B one gives a number no analyst will back |
| `query_clinical_evidence.txt` | VC due diligence on a healthtech device | an n=512 trial and an n=48 pilot treated as equal evidence |
| `query_competitor_intel.txt` | PM planning a roadmap | vendor marketing and review sites disagree; picking the flattering one |
| `query_literature_review.txt` | literature review for a thesis | "15% on one dataset, 5% on another" collapsed into "roughly 10%" |
| `query_remote_work_policy.txt` | policy research | a −20% city figure reported as the national number |

**The subtopic list is the yardstick** — the run is graded against it, so the system cannot
declare success by shortening its own list.

---

## 11. How to see each idea fail

Run a query file once correctly, then flip **one** switch. Two at once tells you nothing.

| Switch | CLI | What you see |
|---|---|---|
| Parallel dispatch off | `--sequential` | same report; `parallel_saving` drops to zero saved seconds; wall clock ≈ the sum |
| Structured errors off | `--silent-errors` | an agent that returns nothing is dropped instead of recorded |
| Broad decomposition off | `--narrow` | one subtopic, researched well, reported as full coverage |

Four checks you can watch offline (`uv run coordinator.py --demo`, or the **Learn** tab):

1. **Silent failure** — `{"findings": [], "status": "ok"}` → `empty_result`.
2. **Lost provenance** — a claim sourced to "various sources" → refused, `parse_failure`.
3. **Conflict** — 91 vs 71 → both kept, one marked lead; averaging would print 81.
4. **Coverage** — four subtopics, four outcomes; only the retryable gap is re-dispatched.

### Tests — `uv run pytest -q` → **23 passed**, offline

| File | Tests | Covers |
|---|---|---|
| `test_decomposition.py` | 6 | 5+ subtopics; duplicates and vague subtopics; every query file checked |
| `test_error_propagation.py` | 8 | timeout → structured error; `access_failure` ≠ `empty_result`; partials survive; only retryable retries; undispatched subtopic → gap |
| `test_conflict_resolution.py` | 9 | both figures kept; lead by confidence then recency; nothing averaged |

---

## 12. Not built

- **Automatic re-dispatch** — gaps are detected and flagged retryable, but the second
  attempt is not fired automatically. `needs_redelegation()` decides; nothing calls it yet.
- **Scratchpad + resume** — findings are saved to `output/` but not reloaded into a run.
- **MCP servers** — the D2 material here is tool grants, not custom MCP tools.
- **D3 config layer** — the Feature Review build carries it.

---

## 13. Anti-patterns the exam offers as plausible answers

1. **Narrow decomposition** — "EV battery market" split into only "lithium-ion pricing".
2. **Silent failure** — agent times out, returns empty success, coverage looks full.
3. **Lost provenance** — a summarization step drops the source; the report cannot cite.
4. **Super-agent** — give synthesis every tool "so it can double-check".
5. **Averaging conflicts** — $91B and $71B reported as $81B, a number nobody published.
6. **Assumed inheritance** — "continue from the findings above" to an empty context.
7. **Sequential dispatch by habit** — six independent searches, one after another.
8. **Speculative caching** — telling an agent to cache context synthesis "might need".

---

## 14. Status

Built, run live, and corrected from what the live run showed. `--demo`, `pytest` and the
Learn tab need no API key.

---

## 15. Demo map — every concept, and the exact line that shows it

Open the file, jump to the line, point at it. Line numbers are from this commit; if you
edit the code, re-check with `grep -n`.

### D1 — Agentic Architecture (40%)

| # | Concept | Where | What to say standing at that line |
|---|---|---|---|
| 1 | Hub-and-spoke, six steps | `coordinator.py:342` `run_research()` | This is the hub. It plans, dispatches, collects, checks, reports — and owns no research capability of its own. |
| 2 | **Parallel dispatch** | `coordinator.py:377` `asyncio.gather` | Every agent starts here, together. The run costs the slowest agent, not the sum. |
| 3 | Sequential anti-pattern |  `coordinator.py:381-386` | The `else` branch: `await` inside a loop. Same report, longer wait. This is what `--sequential` runs. |
| 4 | **Per-agent tool grant** | `coordinator.py:183-184` | `tools` (what exists) **and** `allowed_tools` (what is auto-approved), set per session. This is where the architecture is actually enforced. |
| 5 | Decomposition | `coordinator.py:315` `plan_subtopics()` | Subtopics come from the query file, or get planned live if you typed a free question. |
| 6 | Decomposition, checked | `models.py:261` `check_decomposition()` + `models.py:32` `MIN_SUBTOPICS = 5` | Fewer than 5 subtopics is flagged as narrow — in code, not by asking the model. |
| 7 | Availability as control | `coordinator.py:370-371` + `coordinator.py:94` `local_documents()` | No documents → the document agent is never dispatched. An agent that is never created cannot be used out of habit. |
| 8 | Separation of concerns | `agents/synthesis.py:19` `TOOLS = ["Read"]` | The most instructive line in the project. No search tool exists for it, so it cannot re-research. |
| 9 | Parallelism **measured** | `models.py:355` `parallel_saving` | "2 agents: 130s of work in 103s of waiting." A transcript that says "in parallel" is not evidence; this is. |
| 10 | One agent's death ≠ run's death | `coordinator.py:191` `run_agent()` | It never raises. A crash here would lose the other five agents' work. |

### D5 — Context & Reliability (30%)

| # | Concept | Where | What to say |
|---|---|---|---|
| 11 | **Context isolation** | `coordinator.py:238` `collect_prompt()`, lines `254` / `257` | "OVERALL TOPIC… YOUR SUBTOPIC, and only this." There is no conversation above this. If it is not written here, the agent does not have it. |
| 12 | Findings passed explicitly | `coordinator.py:264` `synthesis_prompt()` | Synthesis gets the other agents' findings *in its prompt*, because it inherits nothing. |
| 13 | **Provenance** | `models.py:41` `Finding`, field at `:56` | `source_url` sits beside the claim, never inside the sentence — so the next agent that shortens the sentence cannot delete the citation. |
| 14 | Two dates, not one | `models.py:61-62` | `timestamp` = when the SOURCE was published. `retrieved_at` = when we fetched it. Conflate them and a 2019 figure passes for current. |
| 15 | Placeholder sources refused | `models.py:101` `BAD_SOURCES` + `models.py:110` `make_finding()` | "various sources" raises. An uncitable claim in a cited report is worse than a missing one. |
| 16 | **Structured errors** | `models.py:71` `WorkerError` | `error_type`, `is_retryable`, `attempted_action`, `partial_results`. "Something failed" gives the hub none of that. |
| 17 | **access_failure ≠ empty_result** | `models.py:87` `ERROR_TYPES`, `models.py:98` `BLOCKS_COVERAGE` | Never looked vs looked and found nothing. The first is a gap; the second is an answer. |
| 18 | **Silent failure caught** | `models.py:169` `if not findings and not errors:` | The single most important branch. An agent that says nothing has still said something. |
| 19 | Timeout → typed error | `coordinator.py:91` `AGENT_TIMEOUT_S` + `coordinator.py:218` `asyncio.wait_for` | A hang becomes a real, retryable `timeout` error instead of a frozen demo. |
| 20 | Coverage gaps in code | `models.py:280` `coverage_notes()`, called at `coordinator.py:407` | Asking the model "did you cover everything?" in the session that made the plan gets you "yes". |
| 21 | Retry policy | `models.py:99` `RETRY_BY_DEFAULT` + `models.py:303` `needs_redelegation()` | Only retryable failures. Re-asking after an `empty_result` buys the same silence at full price. |
| 22 | **Conflicts kept** | `models.py:217` `find_conflicts()`, `:239` `lead_of()`, `:250` `split_findings()` | Choosing a lead is presentation. Deleting the other figure is a research error. |
| 23 | Conflict grouping | `models.py:200` `_question_key()` | Groups by the *question*, numbers stripped — because the number is the answer, not the question. See §16 for why this line exists. |

### D4 — Prompt Engineering & Structured Output (15%)

| # | Concept | Where | What to say |
|---|---|---|---|
| 24 | **One contract, four agents** | `agents/__init__.py:36` `CONTRACT` | One shape for all four, so the hub has one parser and no agent invents its own way of saying "failed". |
| 25 | Refusal is in the contract | `agents/__init__.py:59-60` | "Never write source_url: ''" and "findings: [] with errors: [] is INVALID output." |
| 26 | Goal + criteria, not steps | `agents/web_search.py:20` `PROMPT` | Each prompt says what good output is and what the agent must *not* do — not a procedure to follow. |
| 27 | Cost control in the prompt | `agents/web_search.py:33` | "Stop after 3 sources, then return the JSON immediately." |
| 28 | Never average | `agents/synthesis.py:41` | Stated in the prompt **and** enforced in code (#22). Guidance plus enforcement. |
| 29 | Five required headings | `agents/report_generation.py:20` | Printed even when empty, because "None" is information and a missing heading is not. |

### D2 — Tool Design (10%)

| # | Concept | Where | What to say |
|---|---|---|---|
| 30 | The grants themselves | `web_search.py:18`, `document_analysis.py:15`, `synthesis.py:19`, `report_generation.py:12` | Four files, four `TOOLS` lists. This is the architecture, written down. |
| 31 | The grant map | `agents/__init__.py:91` `tool_table()` | Printed in the UI sidebar — read straight out of the agent files, never hand-typed. |
| 32 | **The receipt** | `models.py:330` `AgentRun.tools_used` → shown at `app.py:164` | After a run, the Agent sessions table shows synthesis with "— none —". The grant is proven, not asserted. |

### D3 — Configuration (5%)

| # | Concept | Where | What to say |
|---|---|---|---|
| 33 | Key never touches our code | `coordinator.py:57` `load_dotenv` before the SDK import | It goes into the environment; the bundled CLI reads it. Import order matters. |
| 34 | Output as configuration | `coordinator.py:452` `save_output()` | Every run writes `research.md` + `findings.json`; the latest is always at `output/research.md`. |

### The anti-patterns, live

| Switch | CLI | Line that changes behaviour |
|---|---|---|
| Parallel off | `--sequential` | `coordinator.py:381` |
| Errors silent | `--silent-errors` | `coordinator.py:397` (`if not payload and not structured_errors: continue`) |
| Narrow decomposition | `--narrow` | `coordinator.py:338` (`planned[:1]`) |

---

## 16. What the live run changed

The first version followed the textbook Scenario 3 answer literally: **one coordinator
agent with `Task` in `allowedTools`, spawning subagents.** It ran, cost $0.136, and
returned **zero findings** with a confident message saying seven agents were working. Four
things were wrong, and all four are worth more than the original plan:

1. **The tool arrives as `Agent`, not `Task`.** Telemetry counting `Task` blocks counted
   zero and reported "the coordinator never delegated" while it was delegating.
2. **Subagents run as background tasks.** The coordinator's turn ended before any result
   returned — "I'll be notified automatically when it completes" — so the run finished with
   nothing. A follow-up turn asking for results got "the agent is still running".
3. **`allowed_tools=["Task"]` does not restrict the coordinator.** In a live run it called
   `WebSearch` itself. The super-agent anti-pattern arrived through the config, silently.
4. **`tools=["Task"]` does restrict — too much.** It applies to the whole session, so the
   subagents lost `WebSearch` and failed.

**The fix:** the hub is Python. Each agent is its own session with its own tool grant, and
`asyncio.gather` runs them together. Every exam concept survives, and two get stronger:

- tool restriction is now **real and measurable** — the synthesis agent's `tools_used` came
  back empty
- parallelism is measured in **wall-clock seconds** — "2 agents: 130s of work in 103s of
  waiting" — instead of inferred from the shape of a message

**Correction six — the system's own signature failure, in its own code.** A later run hit
the account's API spend cap. All seven agent sessions returned in 4 seconds at $0.000 with
zero tool calls, having done nothing. The reply text was `API Error: 400 You have reached
your specified API usage limits`. Because `ResultMessage.is_error` was never read, each
dead session was recorded as `empty_result` — *we looked and there is nothing* — and the
run announced **100% coverage** over a report with no findings.

That is exactly the anti-pattern this project exists to prevent, written by the person who
built the prevention. The fix: `run_agent()` reads `is_error` and `terminal_reason`, and
`_session_error()` maps the reply to `rate_limit` (a spend cap — not retryable in this run)
or `api_error`. Both are in `BLOCKS_COVERAGE`, so the same run now prints:

```
!! NO RESEARCH HAPPENED -- every agent session failed before doing any work.
   API Error: 400 You have reached your specified API usage limits...
6 subtopics | 0 findings | 7 errors | 6 gaps | coverage 0%
```

Two smaller bugs fell out of the same path: a failed planner returned an empty subtopic
list and `result.subtopics[0]` raised `IndexError`, and the planner's own failure was never
recorded. Both fixed.

**The lesson, stated plainly:** a check only exists where you actually wrote it. Silent
failure was caught at the *parsing* layer and not at the *session* layer, so it walked
straight through the gap.

A fifth correction came from the same series of runs: `find_conflicts()` grouped by subtopic alone and
flagged five unrelated findings as contradicting each other. The report agent, reading the
same findings, correctly wrote "Contested findings: None". The code was wrong and the model
was right, so grouping now keys on the **question** (subtopic + content words, numbers
stripped), and the same five findings now produce zero conflicts.
