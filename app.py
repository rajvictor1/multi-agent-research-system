"""app.py -- the Streamlit UI.

    streamlit run app.py

Two tabs:
  RUN     pick a query file, watch the agents work, read and download the report.
  LEARN   the same four checks as `python coordinator.py --demo`, offline and free.

The sidebar switches each turn a correct pattern into its anti-pattern. Run a query once
with everything correct, then flip ONE switch and run it again. Flipping two at once tells
you nothing.
"""

from __future__ import annotations

import asyncio

import streamlit as st

import agents
from coordinator import (
    DOCS_DIR,
    OUTPUT_DIR,
    load_query,
    local_documents,
    query_files,
    run_research,
)
from models import (
    coverage_notes,
    find_conflicts,
    lead_of,
    needs_redelegation,
    parse_worker_output,
    split_findings,
)

st.set_page_config(page_title="Multi-Agent Research System", page_icon="🔎", layout="wide")

# ---------------------------------------------------------------- sidebar
st.sidebar.title("Switches")
st.sidebar.caption("All ON = the correct build. Flip one at a time.")

parallel = st.sidebar.toggle(
    "Parallel dispatch", value=True,
    help="ON: every agent starts at once, so the run costs the SLOWEST agent. "
         "OFF: one at a time, so it costs their SUM. Same report, longer wait.")
structured = st.sidebar.toggle(
    "Structured errors", value=True,
    help="ON: an agent that returns nothing is recorded as empty_result. "
         "OFF: silent failure -- the subtopic vanishes and the report never says so.")
broad = st.sidebar.toggle(
    "Broad decomposition", value=True,
    help="ON: 5+ distinct subtopics. OFF: one angle researched well, reported as "
         "full coverage.")

st.sidebar.divider()
st.sidebar.markdown("**Who can do what** — read straight out of `agents/`")
for name, tools in agents.tool_table().items():
    st.sidebar.markdown(f"- {name} → `{'`, `'.join(tools)}`")
st.sidebar.caption(
    "Synthesis and the report writer have no SEARCH tools. This is enforced per agent "
    "session, not requested in a prompt — after a run, check the Agent sessions table: "
    "their tools column comes back empty."
)

run_tab, learn_tab = st.tabs(["Run", "Learn (offline)"])

# ---------------------------------------------------------------- RUN tab
with run_tab:
    st.title("Multi-agent research")
    st.caption("One agent per subtopic, all at once. Every claim carries a source; every "
               "failure is typed; conflicting sources are kept, never averaged.")

    choice = st.selectbox("Query file", query_files() + ["(write my own)"])
    if choice == "(write my own)":
        source = st.text_area("Your research question", height=110)
    else:
        loaded = load_query(choice)
        source = choice
        left, right = st.columns([3, 1])
        left.markdown(f"**Topic:** {loaded['topic']}")
        left.markdown(f"**Timeframe:** {loaded['timeframe']}")
        right.metric("Subtopics to cover", len(loaded["subtopics"]))
        with st.expander("Required coverage — the run is graded against this"):
            for s in loaded["subtopics"]:
                st.markdown(f"- {s}")

    # Your own documents. This is what makes the document agent real: a paid analyst
    # extract, an internal deck, a trial paper -- evidence web search can never reach.
    docs = local_documents()
    with st.expander(f"Your documents ({len(docs)}) — private evidence for the "
                     f"document agent", expanded=not docs):
        # .txt only, on purpose. Extracting a 200-page PDF can cost more tokens than the
        # whole rest of the research; pasting the pages you need costs almost nothing.
        uploaded = st.file_uploader(
            "Add .txt files — paste in the section of a report you actually need. "
            "Saved to documents/ and read locally; nothing leaves your machine.",
            type=["txt"], accept_multiple_files=True)
        if uploaded:
            DOCS_DIR.mkdir(exist_ok=True)
            for file in uploaded:
                (DOCS_DIR / file.name).write_bytes(file.getbuffer())
            st.success(f"Saved {len(uploaded)} file(s). Press Run research to use them.")
            docs = local_documents()
        if docs:
            for name in docs:
                st.markdown(f"- `{name}`")
            st.caption("Text only. A PDF must be extracted before an agent can read it, "
                       "and that extraction is usually the most expensive part of a run.")
        else:
            st.caption("Empty — the document agent is not dispatched, and every subtopic "
                       "goes to web search. Deliberate: dispatching an agent to search an "
                       "empty folder costs money and returns an error.")

    st.caption("A full run takes 2–5 minutes and costs roughly $0.20–$1.00 — one agent "
               "session per subtopic, plus synthesis and the report writer.")

    if st.button("Run research", type="primary", disabled=not str(source).strip()):
        st.subheader("Live — what the agents are doing")
        st.caption("▶ started · · a tool call, as it happens · ✔ finished. With parallel "
                   "dispatch on, these lines interleave — that is several agents working "
                   "in the same moment.")
        log_box = st.empty()
        lines: list[str] = []

        def on_event(line: str) -> None:
            lines.append(line)
            # Show the tail while it runs; the whole log is kept for afterwards.
            log_box.code("\n".join(lines[-22:]))

        with st.spinner("Agents are working — this takes a few minutes..."):
            try:
                st.session_state.result = asyncio.run(run_research(
                    source, parallel=parallel, structured_errors=structured,
                    broad=broad, on_event=on_event))
                st.session_state.log = lines
            except Exception as exc:
                st.error(f"Run failed: {exc}")
                st.info("Needs ANTHROPIC_API_KEY in .env. The Learn tab works without one.")

    result = st.session_state.get("result")
    if result:
        st.divider()
        a, b, c, d, e = st.columns(5)
        a.metric("Coverage", f"{result.coverage:.0%}")
        b.metric("Findings", len(result.findings))
        c.metric("Conflicts", len(result.conflicts))
        d.metric("Time", f"{result.seconds:.0f}s")
        e.metric("Cost", f"${result.cost_usd:.3f}")

        # A dead session is not a research answer. Say so before anything else, or
        # "0 findings" reads as "there was nothing to find".
        dead = [e for e in result.errors
                if e.error_type in ("api_error", "rate_limit")]
        if dead:
            st.error(f"**{len(dead)} agent session(s) failed before doing any work — "
                     f"no research was performed.**\n\n`{dead[0].message}`\n\n"
                     "Coverage is 0% by design: a failed session is not an empty result. "
                     "Check your API limit in the Anthropic Console, then re-run.")

        # Parallelism, measured in seconds rather than claimed in prose.
        st.info(f"**Dispatch:** {result.parallel_saving}")

        # --- the report, first, because it is what you came for -------------------
        if result.report:
            st.subheader("Report")
            if result.output_dir:
                st.caption(f"Saved to `{result.output_dir.name}/research.md` — "
                           f"latest copy always at `output/research.md`")
            with st.container(border=True):
                st.markdown(result.report)
            r1, r2 = st.columns(2)
            r1.download_button("Download research.md", result.report, "research.md",
                               type="primary", width="stretch")
            if result.output_dir:
                r2.code(str(result.output_dir), language=None)

        # --- what the agents actually did ----------------------------------------
        if st.session_state.get("log"):
            with st.expander(f"Full delegation log ({len(st.session_state.log)} lines)"):
                st.code("\n".join(st.session_state.log))

        st.subheader("Agent sessions")
        st.caption("The tools column is the receipt for each agent's grant. Synthesis and "
                   "the report writer never show a search tool — they do not have one.")
        st.dataframe(
            [{"agent": r.role, "subtopic": r.subtopic[:44], "seconds": round(r.elapsed),
              "cost": f"${r.cost_usd:.3f}",
              "tools used": ", ".join(sorted(set(r.tools_used))) or "— none —",
              "failed": r.failure.error_type if r.failure else ""}
             for r in result.agent_runs],
            width="stretch", hide_index=True)

        if result.decomposition_problems:
            st.subheader("Decomposition problems")
            for problem in result.decomposition_problems:
                st.error(problem)

        if result.coverage_notes:
            st.subheader("Coverage notes")
            st.caption("Found in code, not by asking the model whether it did well.")
            for note in result.coverage_notes:
                subtopic = note.split(" -- ")[0]
                if needs_redelegation(subtopic, result.errors):
                    st.warning(f"{note}  — retryable, worth one more attempt")
                else:
                    st.info(note)

        if result.conflicts:
            st.subheader("Contested findings")
            st.caption("Both figures kept. Leading with one is presentation; deleting the "
                       "other, or averaging them, is a research error.")
            for conflict in result.conflicts:
                lead, why = lead_of(conflict)
                st.markdown(f"**{conflict[0].subtopic}**")
                st.dataframe(
                    [{"claim": f.claim, "source": f.source_title or f.source_url,
                      "url": f.source_url, "published": f.timestamp or "-",
                      "confidence": f.confidence,
                      "": f"lead ({why})" if f is lead else "contested"}
                     for f in conflict],
                    width="stretch", hide_index=True)

        established, _ = split_findings(result.findings)
        if established:
            with st.expander(f"Well-established findings ({len(established)})"):
                st.dataframe(
                    [{"subtopic": f.subtopic, "claim": f.claim,
                      "source": f.source_title or f.source_url, "url": f.source_url,
                      "published": f.timestamp or "-", "confidence": f.confidence,
                      "agent": f.worker_id} for f in established],
                    width="stretch", hide_index=True)

        if result.errors:
            with st.expander(f"Agent errors ({len(result.errors)})"):
                st.caption("access_failure = we never saw the data. "
                           "empty_result = we looked and there is nothing.")
                st.dataframe(
                    [{"error_type": e.error_type, "retryable": e.is_retryable,
                      "agent": e.worker_id, "subtopic": e.subtopic,
                      "message": e.message, "attempted_action": e.attempted_action}
                     for e in result.errors],
                    width="stretch", hide_index=True)

        if result.sources:
            with st.expander(f"Sources ({len(result.sources)})"):
                for i, s in enumerate(result.sources, 1):
                    st.markdown(f"{i}. {s}")

    # --- everything ever produced ------------------------------------------------
    saved = sorted(OUTPUT_DIR.glob("*/research.md"), reverse=True) if OUTPUT_DIR.exists() else []
    if saved:
        with st.expander(f"Past reports in output/ ({len(saved)})"):
            for path in saved[:20]:
                st.markdown(f"- `{path.parent.name}/research.md`")

# ---------------------------------------------------------------- LEARN tab
with learn_tab:
    st.title("The four checks")
    st.caption("Runs offline, no API key, no cost. Same content as "
               "`python coordinator.py --demo`.")

    st.subheader("1. Silent failure → visible failure")
    st.write("An agent times out and returns a well-formed empty success. It looks "
             "identical to one that searched properly and found nothing.")
    _, errors = parse_worker_output({"findings": [], "status": "ok"},
                                    "2026 projections", "web_search")
    st.code('agent sent  : {"findings": [], "status": "ok"}\n'
            f'recorded as : {errors[0].error_type} -- {errors[0].message}')
    st.info("Without this conversion the subtopic reads as covered, and a sixth of the "
            "topic disappears from the report without a word.")

    st.subheader("2. A claim with no source is refused")
    findings, errors = parse_worker_output(
        {"findings": [{"claim": "The market reached $91B.",
                       "source_url": "various sources", "confidence": 0.9}]},
        "market size", "web_search")
    st.code(f'kept as finding : {len(findings)}\n'
            f'recorded as     : {errors[0].error_type} -- {errors[0].message}')
    st.info("Provenance travels beside the claim, never inside the sentence — so the "
            "first agent that shortens the sentence cannot delete the citation.")

    st.subheader("3. Conflicting sources are both kept")
    st.write("A public tracker and your own paid document disagree. This is the exact "
             "case in `documents/sample_ev_battery_analyst_extract.txt`.")
    findings, _ = parse_worker_output({"findings": [
        {"claim": "Market at 91 billion in 2024.", "source_url": "https://marketsandmarkets",
         "source_title": "MarketsandMarkets (public)", "confidence": 0.9,
         "timestamp": "2026-04-01"},
        {"claim": "Market at 71 billion in 2024.",
         "source_url": "documents/sample_ev_battery_analyst_extract.txt",
         "source_title": "Meridian, paid — pack-level only", "confidence": 0.8,
         "timestamp": "2026-02-18"}]}, "market size", "web_search")
    conflict = find_conflicts(findings)[0]
    lead, why = lead_of(conflict)
    st.dataframe(
        [{"claim": f.claim, "source": f.source_title, "confidence": f.confidence,
          "": f"lead ({why})" if f is lead else "contested"} for f in conflict],
        width="stretch", hide_index=True)
    st.error("Averaging these gives 81 billion — a number neither source published. "
             "The document's own scope note is what explains the gap: pack-level only.")

    st.subheader("4. Coverage is checked in code")
    planned = ["market size", "supply chain", "cell pricing", "policy changes"]
    _, blocked = parse_worker_output({"errors": [{
        "error_type": "access_failure", "message": "analyst report paywalled",
        "attempted_action": "https://example.com/report", "is_retryable": True}]},
        "supply chain", "web_search")
    _, nothing = parse_worker_output({"errors": [{
        "error_type": "empty_result", "message": "no 2026 figures published",
        "attempted_action": "2026 cell price", "is_retryable": False}]},
        "cell pricing", "web_search")
    st.dataframe(
        [{"subtopic": "market size", "outcome": "2 findings", "counts as": "covered"},
         {"subtopic": "cell pricing", "outcome": "empty_result",
          "counts as": "covered — the answer is 'nothing published'"},
         {"subtopic": "supply chain", "outcome": "access_failure",
          "counts as": "GAP — retryable, we never saw the data"},
         {"subtopic": "policy changes", "outcome": "never dispatched",
          "counts as": "GAP — the worst kind, invisible without this check"}],
        width="stretch", hide_index=True)
    st.code("gaps: " + str(coverage_notes(planned, findings, blocked + nothing)) + "\n"
            f"re-dispatch supply chain? {needs_redelegation('supply chain', blocked)}"
            "   (retryable)\n"
            f"re-dispatch cell pricing? {needs_redelegation('cell pricing', nothing)}"
            "  (same query would buy the same silence)")
    st.info("Asking the model 'did you cover everything?' in the session that made the "
            "plan gets you 'yes' — it is reviewing its own reasoning.")
