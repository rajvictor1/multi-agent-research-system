"""coordinator.py -- the hub, and the command line.

WHAT THE COORDINATOR DOES, IN ORDER
  1. READ       the query file: TOPIC, TIMEFRAME, and the subtopics to cover
  2. DECOMPOSE  into 5+ subtopics (taken from the file, or planned by an agent)
  3. DISPATCH   one agent per subtopic, all at the same time
  4. COLLECT    JSON findings + JSON errors from each
  5. CHECK      conflicts, coverage gaps, what is worth retrying -- all in code
  6. REPORT     synthesis agent reconciles, report agent writes, saved to output/

WHY THE HUB IS PYTHON AND NOT A PROMPT  (this was learned from a live run)
  The textbook Scenario 3 answer is: give one coordinator agent the Task tool and let it
  spawn subagents. We built that first. Measured, on this CLI, it does not work:

    - the tool arrives as "Agent", not "Task", so naive telemetry counts zero dispatches
    - subagents run as BACKGROUND tasks. The coordinator's turn ends before any result
      comes back, so it reports "agents are running, I'll notify you" and returns nothing
    - allowed_tools=["Task"] does NOT restrict the coordinator. In a live run it called
      WebSearch itself, which is the super-agent anti-pattern arriving through the config
    - tools=["Task"] DOES restrict, but it restricts the whole session, so the subagents
      lose WebSearch too and fail

  So the hub is this file. Each agent is its OWN session with its OWN tool grant, and
  asyncio.gather runs them at once. Everything the exam cares about survives, and two
  things get stronger: tool restriction is now real (measured: the synthesis agent used
  zero tools), and parallelism is measured in wall-clock seconds rather than inferred
  from the shape of a message.

RUN IT
  python coordinator.py --demo                     offline, no API key
  python coordinator.py query_ev_batteries.txt
  python coordinator.py "any question you like"
  streamlit run app.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):          # Windows consoles are cp1252; the model
    try:                                     # writes em dashes. Don't let printing kill
        _s.reconfigure(encoding="utf-8", errors="replace")   # a finished run.
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

from claude_agent_sdk import (  # noqa: E402  (imported after load_dotenv, on purpose)
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock  # noqa: E402

import agents  # noqa: E402
from models import (  # noqa: E402
    MIN_SUBTOPICS,
    AgentRun,
    ResearchResult,
    WorkerError,
    check_decomposition,
    coverage_notes,
    find_conflicts,
    lead_of,
    needs_redelegation,
    parse_worker_output,
    split_findings,
)

MODEL = "claude-haiku-4-5-20251001"
QUERY_DIR = HERE / "sample_queries"
DOCS_DIR = HERE / "documents"      # what the document analysis agent reads
OUTPUT_DIR = HERE / "output"       # every finished report lands here

# Per-agent caps. An agent that runs away costs real money, and a class demo that takes
# ten minutes is a class demo nobody watches.
AGENT_MAX_TURNS = 20
AGENT_BUDGET_USD = 0.30
AGENT_TIMEOUT_S = 240              # exceeded -> a real, structured `timeout` error


def local_documents() -> list[str]:
    """Files the document analysis agent can actually read.

    If this comes back empty, that agent is never dispatched. Spawning an agent to search
    an empty folder costs money and returns an access_failure.

    Plain text only. A PDF has to be extracted before the agent can read it, and that
    extraction is what makes a document run expensive -- a 200-page PDF can cost more
    tokens than the entire rest of the research. Convert to .txt first and you pay for
    the paragraphs you actually need.

    rglob, so you can sort files into subfolders (documents/ev/, documents/clinical/).
    """
    if not DOCS_DIR.exists():
        return []
    return sorted(p.relative_to(DOCS_DIR).as_posix() for p in DOCS_DIR.rglob("*.txt"))


# ---------------------------------------------------------------------------
# Query files
# ---------------------------------------------------------------------------

def load_query(source: str) -> dict:
    """Read a query file, or take the text as-is if it is not a path.

    File format (see sample_queries/):

        TOPIC: EV battery market size
        TIMEFRAME: 2024-2026
        SUBTOPICS:
        - global market size estimates
        - regional demand split

    The SUBTOPICS list is what the run is graded against: if an agent comes back without
    covering one, it shows up as a coverage note rather than as silence.
    """
    path = Path(source)
    if not path.is_absolute():
        for candidate in (HERE / source, QUERY_DIR / source, QUERY_DIR / f"{source}.txt"):
            if candidate.exists():
                path = candidate
                break

    if not path.exists():
        return {"topic": source, "timeframe": "", "subtopics": [], "text": source}

    text = path.read_text(encoding="utf-8")
    topic = timeframe = ""
    subtopics: list[str] = []
    in_subtopics = False

    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("TOPIC:"):
            topic, in_subtopics = stripped.split(":", 1)[1].strip(), False
        elif upper.startswith("TIMEFRAME:"):
            timeframe, in_subtopics = stripped.split(":", 1)[1].strip(), False
        elif upper.startswith("SUBTOPICS:"):
            in_subtopics = True
        elif upper.startswith("USE CASE:"):
            in_subtopics = False
        elif in_subtopics and stripped.startswith("-"):
            subtopics.append(stripped.lstrip("- ").strip())

    return {"topic": topic or text.strip()[:120], "timeframe": timeframe,
            "subtopics": subtopics, "text": text.strip()}


def query_files() -> list[str]:
    """The sample query files, for the UI dropdown."""
    return sorted(p.name for p in QUERY_DIR.glob("*.txt")) if QUERY_DIR.exists() else []


# ---------------------------------------------------------------------------
# One agent = one session, with its own tools and its own context
# ---------------------------------------------------------------------------

def agent_options(role: str) -> ClaudeAgentOptions:
    """The tool grant is enforced here, per agent, and it is real.

    `tools` restricts what exists; `allowed_tools` auto-approves it. Both are set to the
    same list so the synthesis agent genuinely cannot search -- verified in a live run,
    where it used zero tools.

    `thinking` and `max_turns` come from the agent's own file, because the right budget is
    a property of the job. Searching is mechanical, so the collectors run with thinking
    disabled -- and there is one collector per subtopic, so that is where a run's cost
    actually lives. Synthesis keeps it: deciding whether two figures really disagree is
    the one hard judgement, and it is a single tool-less session.
    """
    module = {m.NAME: m for m in agents.MODULES}[role]
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=module.PROMPT + agents.CONTRACT,
        tools=module.TOOLS,
        allowed_tools=module.TOOLS,
        thinking=getattr(module, "THINKING", None),
        max_turns=getattr(module, "MAX_TURNS", AGENT_MAX_TURNS),
        max_budget_usd=AGENT_BUDGET_USD,
        cwd=str(HERE),
    )


def tool_label(block: ToolUseBlock) -> str:
    """A one-line version of what an agent just asked for, for the live log.

    This is the "behind the scenes" view: not that an agent is busy, but what it is
    actually doing right now -- which query it typed, which page it opened.
    """
    data = block.input if isinstance(block.input, dict) else {}
    for key in ("query", "url", "pattern", "file_path", "path"):
        if data.get(key):
            return f"{block.name} · {str(data[key])[:70]}"
    return block.name


def _session_error(role: str, subtopic: str, text: str) -> WorkerError:
    """Turn a failed session into a typed error the report can print.

    Both cases below block coverage, so a run that hit them can never claim 100%.
    """
    lowered = text.lower()
    if "usage limit" in lowered or "rate limit" in lowered or "429" in lowered:
        # An account spend cap is not retryable in this run -- it is retryable in
        # September. Saying "retryable" here would send the coordinator back for more.
        return WorkerError(
            error_type="rate_limit", worker_id=role, subtopic=subtopic,
            message=text[:250] or "API usage limit reached",
            attempted_action=subtopic, is_retryable=False)
    return WorkerError(
        error_type="api_error", worker_id=role, subtopic=subtopic,
        message=text[:250] or "the agent session failed before doing any work",
        attempted_action=subtopic, is_retryable=True)


async def run_agent(role: str, subtopic: str, prompt: str, say=None) -> AgentRun:
    """Run one agent to completion. Never raises -- failures come back as data.

    A crash here would take the whole run down and lose the other agents' work. Every
    failure mode instead becomes a typed error the report can print:
      timeout   -> the agent exceeded AGENT_TIMEOUT_S
      tool_denied / access_failure etc. -> whatever the agent itself reported
      parse_failure -> it answered, but not in the contract's shape
    """
    started = time.monotonic()
    run = AgentRun(role=role, subtopic=subtopic)
    note = say or (lambda _line: None)
    note(f"  ▶ {role} · {subtopic[:46]} — starting")

    async def _drive() -> None:
        async with ClaudeSDKClient(options=agent_options(role)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            run.tools_used.append(block.name)
                            # Streamed live. With agents running together these lines
                            # interleave, which is the point: you can watch six searches
                            # happening at the same moment.
                            note(f"    · {role[:12]:<12} {tool_label(block)}")
                        elif isinstance(block, TextBlock):
                            run.text = block.text
                elif isinstance(message, ResultMessage):
                    run.cost_usd = message.total_cost_usd or 0.0
                    run.terminal_reason = getattr(message, "terminal_reason", "") or ""
                    # A session can end "successfully" and have done nothing at all.
                    # is_error is the only honest signal: an API error, an auth failure or
                    # a spend cap all arrive here as ordinary-looking text. Read it, or
                    # you will record seven dead sessions as seven empty results and
                    # report full coverage -- which is what this system exists to prevent.
                    if message.is_error or run.terminal_reason == "api_error":
                        run.failure = _session_error(
                            role, subtopic, (message.result or run.text or "").strip())
                        run.text = ""      # not research output; never parse it

    try:
        await asyncio.wait_for(_drive(), timeout=AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        run.failure = WorkerError(
            error_type="timeout", worker_id=role, subtopic=subtopic,
            message=f"agent exceeded {AGENT_TIMEOUT_S}s",
            attempted_action=subtopic, is_retryable=True)
    except Exception as exc:                       # network, auth, SDK -- still data
        run.failure = WorkerError(
            error_type="access_failure", worker_id=role, subtopic=subtopic,
            message=f"{type(exc).__name__}: {exc}"[:300],
            attempted_action=subtopic, is_retryable=True)

    run.elapsed = time.monotonic() - started
    outcome = run.failure.error_type if run.failure else "returned"
    note(f"  ✔ {role} · {subtopic[:46]} — {outcome} "
         f"({run.elapsed:.0f}s, ${run.cost_usd:.3f}, {len(run.tools_used)} tool calls)")
    return run


# ---------------------------------------------------------------------------
# Context packing -- each agent is told everything, because it inherits nothing
# ---------------------------------------------------------------------------

def collect_prompt(subtopic: str, query: dict, docs: list[str]) -> str:
    """The whole world, as the collecting agent will see it.

    There is no conversation above this. No shared state, no earlier turn. If it is not
    written here, the agent does not have it -- which is exactly why this function exists
    instead of a comfortable assumption.
    """
    timeframe = f"\nTIMEFRAME: {query['timeframe']}" if query["timeframe"] else ""
    document_note = (
        "\n\nLOCAL DOCUMENTS you may read (paths are relative to the working directory):\n"
        + "\n".join(f"  - documents/{name}" for name in docs)
        + "\nPrefer these for anything they cover; they are private evidence the web does "
          "not have. Always record the file name and section as the source."
        if docs else ""
    )
    return f"""\
OVERALL TOPIC (for judging relevance only -- do not research the whole thing):
  {query['topic']}{timeframe}

YOUR SUBTOPIC, and only this:
  {subtopic}
{document_note}

Return the JSON contract. Nothing else."""


def synthesis_prompt(query: dict, findings_json: str, errors_json: str) -> str:
    return f"""\
TOPIC: {query['topic']}  {query['timeframe']}

FINDINGS COLLECTED BY THE OTHER AGENTS (already sourced -- do not re-verify, you cannot):
{findings_json}

SUBTOPICS THAT FAILED (state these as gaps; never fill them by guessing):
{errors_json}

Group these, split them into well-established and contested, and keep every source."""


def report_prompt(query: dict, synthesis_text: str, gaps: list[str]) -> str:
    gap_lines = "\n".join(f"  - {g}" for g in gaps) or "  (none)"
    return f"""\
TOPIC: {query['topic']}  {query['timeframe']}

SYNTHESIZED FINDINGS:
{synthesis_text}

COVERAGE GAPS the run detected in code -- these MUST appear under "Coverage notes":
{gap_lines}

Write the final markdown report with all five required headings. Return the markdown
itself, not JSON."""


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Pull a JSON object out of an agent's reply.

    Agents fence their JSON and apologise around it however firmly you ask. Tolerating
    that here keeps a formatting habit from being reported as a research failure.
    """
    text = re.sub(r"^```[a-z]*\n|\n```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


async def plan_subtopics(query: dict, broad: bool, say
                         ) -> tuple[list[str], WorkerError | None]:
    """Decomposition. From the query file when it lists subtopics, else planned live.

    A query file that already names its subtopics is not a shortcut -- it is the yardstick
    the run gets graded against, so the system cannot declare success by shortening its
    own list.
    """
    failure: WorkerError | None = None
    if query["subtopics"]:
        planned = query["subtopics"]
        say(f"decompose : {len(planned)} subtopics taken from the query file")
    else:
        target = MIN_SUBTOPICS if broad else 1
        run = await run_agent(
            "synthesis", "planning",
            f"You are planning research, not doing it. Break this topic into exactly "
            f"{target} DIFFERENT subtopics that together cover it. Different questions, "
            f"not the same question narrowed.\n\nTOPIC: {query['text']}\n\n"
            f'Return ONLY {{"findings": [], "errors": [], "subtopics": ["...", "..."]}}',
            say)
        planned = extract_json(run.text).get("subtopics") or []
        say(f"decompose : {len(planned)} subtopics planned "
            f"({run.elapsed:.0f}s, ${run.cost_usd:.3f})")
        # The planner can fail like any other agent. If it does, the run has no plan --
        # and a run with no plan must say so, not quietly dispatch nothing.
        failure = run.failure
        if not planned and failure is None:
            failure = WorkerError(
                error_type="parse_failure", worker_id="synthesis", subtopic="planning",
                message="planner returned no subtopics", attempted_action=query["topic"],
                is_retryable=True)

    if not broad:
        planned = planned[:1]          # the anti-pattern: one angle, called coverage
    return planned, failure


async def run_research(source: str, parallel=True, structured_errors=True, broad=True,
                       model=MODEL, on_event=None) -> ResearchResult:
    """One full run. `source` is a query file path/name or the question itself."""

    def say(line: str) -> None:
        print(line)
        if on_event:
            on_event(line)

    query = load_query(source)
    docs = local_documents()
    result = ResearchResult(query=query["text"], topic=query["topic"],
                            timeframe=query["timeframe"])
    started = time.monotonic()

    say(f"topic     : {query['topic']}  {query['timeframe']}")
    say(f"documents : {', '.join(docs) if docs else 'none -- document agent not dispatched'}")

    # --- STEP 1-2: decompose -------------------------------------------------------
    result.subtopics, plan_failure = await plan_subtopics(query, broad, say)
    if plan_failure:
        result.errors.append(plan_failure)
        say(f"PLANNING FAILED: [{plan_failure.error_type}] {plan_failure.message[:120]}")
    result.decomposition_problems = check_decomposition(result.subtopics)
    for problem in result.decomposition_problems:
        say(f"DECOMPOSITION: {problem}")

    # --- STEP 3: dispatch ----------------------------------------------------------
    # One agent per subtopic. The document agent joins only if there are documents --
    # an agent that is never created cannot be dispatched out of habit.
    jobs: list[tuple[str, str]] = [("web_search", s) for s in result.subtopics]
    if docs and result.subtopics:
        jobs.append(("document_analysis", result.subtopics[0]))

    say(f"dispatch  : {len(jobs)} agents, "
        f"{'all at once' if parallel else 'one at a time (anti-pattern)'}")

    if parallel:
        runs = await asyncio.gather(*(
            run_agent(role, sub, collect_prompt(sub, query, docs if role ==
                                                "document_analysis" else []), say)
            for role, sub in jobs))
    else:
        runs = []
        for role, sub in jobs:
            runs.append(await run_agent(
                role, sub, collect_prompt(sub, query,
                                          docs if role == "document_analysis" else []),
                say))

    # --- STEP 4: collect -----------------------------------------------------------
    for run in runs:
        result.agent_runs.append(run)
        if run.failure:
            result.errors.append(run.failure)
            say(f"  [{run.role}] {run.subtopic[:40]} -> {run.failure.error_type}")
            continue

        payload = extract_json(run.text)
        if not payload and not structured_errors:
            continue                    # the anti-pattern: silence, quietly dropped
        findings, errors = parse_worker_output(payload, run.subtopic, run.role)
        result.findings += findings
        result.errors += errors
        say(f"  [{run.role}] {run.subtopic[:40]} -> {len(findings)} findings, "
            f"{len(errors)} errors ({run.elapsed:.0f}s, ${run.cost_usd:.3f})")

    # A run where every session died is not a research result. Say so loudly, once,
    # before the numbers -- otherwise "0 findings" reads like "the internet was empty".
    dead = [e for e in result.errors if e.error_type in ("api_error", "rate_limit")]
    if dead and not result.findings:
        say("")
        say("!! NO RESEARCH HAPPENED -- every agent session failed before doing any work.")
        say(f"   {dead[0].message}")
        say("   Coverage below is 0%, not 100%: a dead session is not an empty result.")
        say("")

    # --- STEP 5: check, in code ----------------------------------------------------
    result.conflicts = find_conflicts(result.findings)
    result.coverage_notes = coverage_notes(result.subtopics, result.findings, result.errors)
    for note in result.coverage_notes:
        subtopic = note.split(" -- ")[0]
        retry = " (retryable)" if needs_redelegation(subtopic, result.errors) else ""
        say(f"GAP       : {note}{retry}")

    # --- STEP 6: synthesize, then write --------------------------------------------
    if result.findings:
        established, contested = split_findings(result.findings)
        say(f"synthesis : {len(established)} agreed, {len(contested)} contested")

        # Trimmed on the way in. Synthesis needs the claim, the number, and who said it --
        # not retrieved_at, not the agent's id, and not 500 characters of quoted evidence.
        # This payload is pasted into one prompt, so every field costs tokens once per
        # finding, and a 20-finding run notices.
        lean_findings = [{"claim": f.claim, "evidence": f.evidence[:120],
                          "source": f.source_title or f.source_url,
                          "url": f.source_url, "date": f.timestamp,
                          "confidence": f.confidence, "subtopic": f.subtopic}
                         for f in result.findings]
        lean_errors = [{"subtopic": e.subtopic, "error_type": e.error_type,
                        "message": e.message[:120]} for e in result.errors]

        syn = await run_agent("synthesis", "reconciliation", synthesis_prompt(
            query, json.dumps(lean_findings, default=str),
            json.dumps(lean_errors, default=str)), say)
        result.agent_runs.append(syn)

        rep = await run_agent("report_generation", "final report", report_prompt(
            query, syn.text or "(synthesis produced nothing)", result.coverage_notes), say)
        result.agent_runs.append(rep)
        result.report = rep.text or syn.text
    else:
        # No findings is a result, not a crash. Say so in the report itself.
        result.report = (
            f"# {query['topic']}\n\n## Summary\n\nNo findings were collected.\n\n"
            "## Well-established findings\n\nNone.\n\n## Contested findings\n\nNone.\n\n"
            "## Coverage notes\n\n"
            + ("\n".join(f"- {n}" for n in result.coverage_notes) or "- nothing dispatched")
            + "\n\n## Sources\n\nNone.\n")
        say("synthesis : skipped -- no findings to reconcile")

    result.seconds = time.monotonic() - started
    result.cost_usd = sum(r.cost_usd for r in result.agent_runs)
    result.output_dir = save_output(result)

    say(result.summary())
    say(f"parallel  : {result.parallel_saving}")
    say(f"saved     : {result.output_dir}")
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_output(result: ResearchResult) -> Path:
    """Write research.md plus the raw data, into a timestamped folder under output/.

    Also refreshes output/research.md so "the latest report" always has one stable path
    you can open without hunting through folders.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", result.topic.lower())[:40].strip("-") or "research"
    folder = OUTPUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "research.md").write_text(result.report or "(no report)", encoding="utf-8")
    (folder / "findings.json").write_text(json.dumps({
        "topic": result.topic,
        "timeframe": result.timeframe,
        "subtopics": result.subtopics,
        "coverage": result.coverage,
        "findings": [f.__dict__ for f in result.findings],
        "errors": [e.__dict__ for e in result.errors],
        "coverage_notes": result.coverage_notes,
        "agents": [{"role": r.role, "subtopic": r.subtopic, "seconds": round(r.elapsed, 1),
                    "cost_usd": r.cost_usd, "tools": sorted(set(r.tools_used))}
                   for r in result.agent_runs],
        "seconds": round(result.seconds, 1),
        "cost_usd": result.cost_usd,
    }, indent=2, default=str), encoding="utf-8")

    (OUTPUT_DIR / "research.md").write_text(result.report or "(no report)", encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

def print_result(r: ResearchResult) -> None:
    print("\n" + "=" * 70)
    print(f"  {r.topic}  {r.timeframe}")
    print("=" * 70)
    print(f"  {r.summary()}")
    print(f"  {r.seconds:.0f}s | ${r.cost_usd:.4f} | {len(r.agent_runs)} agent sessions")
    print(f"  {r.parallel_saving}")

    if r.decomposition_problems:
        print("\n  DECOMPOSITION PROBLEMS:")
        for p in r.decomposition_problems:
            print(f"    - {p}")

    if r.coverage_notes:
        print("\n  COVERAGE NOTES (stated, not hidden):")
        for note in r.coverage_notes:
            print(f"    - {note}")

    if r.errors:
        print("\n  AGENT ERRORS:")
        for e in r.errors:
            retry = "retryable" if e.is_retryable else "terminal"
            print(f"    - [{e.error_type}/{retry}] {e.worker_id}: {e.message}")

    for conflict in r.conflicts:
        lead, why = lead_of(conflict)
        print(f"\n  CONTESTED ({conflict[0].subtopic}) -- both kept, never averaged:")
        for f in conflict:
            mark = "  <- lead, " + why if f is lead else ""
            print(f"    {f.claim}  {f.cite()}  conf={f.confidence}{mark}")

    print(f"\n  report saved: {r.output_dir / 'research.md'}")
    print(f"  latest copy : {OUTPUT_DIR / 'research.md'}")


# ---------------------------------------------------------------------------
# Offline walkthrough
# ---------------------------------------------------------------------------

def demo() -> None:
    """No API key, no cost. The four checks catching the four failures."""

    print("\n1. SILENT FAILURE -- an agent times out and returns an empty success")
    _, errors = parse_worker_output({"findings": [], "status": "ok"},
                                    "2026 projections", "web_search")
    print("   agent sent  : {'findings': [], 'status': 'ok'}")
    print(f"   recorded as : {errors[0].error_type} -- '{errors[0].message}'")
    print("   without this the subtopic reads as covered and vanishes from the report.")

    print("\n2. LOST PROVENANCE -- a claim arrives with its source summarized away")
    findings, errors = parse_worker_output(
        {"findings": [{"claim": "The market reached $91B in 2024.",
                       "source_url": "various sources", "confidence": 0.9}]},
        "market size", "web_search")
    print(f"   kept as finding : {len(findings)}")
    print(f"   recorded as     : {errors[0].error_type} -- '{errors[0].message}'")
    print("   an uncitable claim in a cited report is worse than a missing one.")

    print("\n3. CONFLICT -- two credible sources disagree")
    findings, _ = parse_worker_output({"findings": [
        {"claim": "Market at 91 billion in 2024.", "source_url": "https://marketsandmarkets",
         "source_title": "MarketsandMarkets", "confidence": 0.9, "timestamp": "2026-04-01"},
        {"claim": "Market at 71 billion in 2024.", "source_url": "documents/analyst.txt",
         "source_title": "Meridian (paid, pack-level)", "confidence": 0.8,
         "timestamp": "2026-02-18"}]}, "market size", "web_search")
    conflict = find_conflicts(findings)[0]
    lead, why = lead_of(conflict)
    for f in conflict:
        tag = f"  <- lead, {why}" if f is lead else "  <- kept, contested section"
        print(f"   {f.claim} {f.cite()} conf={f.confidence}{tag}")
    print("   averaging these gives 81 billion, which NO source published. Never average.")

    print("\n4. COVERAGE -- the three ways a planned subtopic can end up")
    planned = ["market size", "supply chain", "cell pricing", "policy changes"]
    _, blocked = parse_worker_output(
        {"errors": [{"error_type": "access_failure", "message": "analyst report paywalled",
                     "attempted_action": "https://example.com/report", "is_retryable": True}]},
        "supply chain", "web_search")
    _, nothing = parse_worker_output(
        {"errors": [{"error_type": "empty_result", "message": "no 2026 figures published",
                     "attempted_action": "2026 cell price", "is_retryable": False}]},
        "cell pricing", "web_search")

    print("   market size  -> 2 findings                      = covered")
    print("   cell pricing -> empty_result (we looked)        = covered, answer is 'none'")
    for note in coverage_notes(planned, findings, blocked + nothing):
        print(f"   GAP: {note}")
    print(f"   re-dispatch supply chain? {needs_redelegation('supply chain', blocked)}"
          "  (retryable)")
    print(f"   re-dispatch cell pricing? {needs_redelegation('cell pricing', nothing)}"
          "  (same query, same silence, full price)")
    print("   found in code -- asking the model 'did you cover it all?' gets you 'yes'.")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Multi-agent research system (CCA-F Scenario 3)")
    p.add_argument("query", nargs="?",
                   help="query file (e.g. query_ev_batteries.txt) or your question. "
                        f"Available: {', '.join(query_files()) or 'none'}")
    p.add_argument("--demo", action="store_true", help="offline walkthrough, no API key")
    p.add_argument("--sequential", action="store_true",
                   help="anti-pattern: dispatch agents one at a time")
    p.add_argument("--silent-errors", action="store_true",
                   help="anti-pattern: let failed agents pass as empty successes")
    p.add_argument("--narrow", action="store_true",
                   help="anti-pattern: research one angle and call it coverage")
    p.add_argument("--model", default=MODEL)
    args = p.parse_args()

    if args.demo or not args.query:
        demo()
        return 0

    result = asyncio.run(run_research(
        args.query,
        parallel=not args.sequential,
        structured_errors=not args.silent_errors,
        broad=not args.narrow,
        model=args.model,
    ))
    print_result(result)
    return 0 if result.coverage == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
