"""models.py -- the data the system passes around, plus the checks that keep it honest.

Field names follow the Scenario 3 build reference exactly, so what you read here is what
you will see in an exam question:

    Finding      claim, evidence, source_url, source_title, timestamp, retrieved_at,
                 confidence, worker_id, subtopic
    WorkerError  error_type, message, attempted_action, is_retryable, partial_results,
                 worker_id, subtopic

Plain dataclasses, no pydantic. Everything here runs without an API key.

THE FOUR CHECKS (these are the exam points)
  1. parse_worker_output()   a worker that returns nothing gets an explicit empty_result.
                             Silence must never read as "covered".
  2. make_finding()          a claim with no source_url is refused. Provenance cannot go
                             missing quietly; it becomes a visible parse_failure.
  3. find_conflicts()        two sources disagreeing are BOTH kept. Never averaged.
  4. coverage_notes()        subtopics that were planned but never answered, found in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone

# The coordinator must break a broad topic into at least this many distinct areas.
# Five is the build reference's number: it is enough to force real decomposition instead
# of restating the topic once and researching that.
MIN_SUBTOPICS = 5


# ---------------------------------------------------------------------------
# 1. The two things a subagent can return
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One traceable claim.

    Provenance travels BESIDE the claim, never inside it. If the source were part of the
    sentence ("according to BNEF, prices fell..."), the first agent that shortens the
    sentence would delete the citation and nobody would know.

    Two dates, on purpose:
      timestamp    when the SOURCE was published  -- tells you if the figure is stale
      retrieved_at when WE fetched it             -- tells you how fresh the run is
    Conflating them is how a 2019 number passes for current.
    """

    claim: str
    evidence: str                    # the exact quote or figure from the source
    source_url: str                  # URL or file path -- never blank, never "various"
    confidence: float                # 0.0 - 1.0
    worker_id: str                   # which subagent found it
    subtopic: str                    # which slice of the topic it answers
    source_title: str = ""           # "MarketsandMarkets, EV Battery Market 2026"
    timestamp: str | None = None     # source publication date, YYYY-MM-DD
    retrieved_at: str = ""           # when this run fetched it

    def cite(self) -> str:
        label = self.source_title or self.source_url
        when = f", {self.timestamp}" if self.timestamp else ""
        return f"[{label}{when}]"


@dataclass
class WorkerError:
    """A failure the coordinator can actually act on.

    error_type says WHAT went wrong, is_retryable says whether to try again, and
    attempted_action says what to change. "Something failed" gives it none of those.
    """

    error_type: str                  # see ERROR_TYPES below
    message: str
    attempted_action: str            # the exact query/path tried, so a retry can differ
    is_retryable: bool
    worker_id: str
    subtopic: str
    partial_results: list = field(default_factory=list)


ERROR_TYPES = [
    "access_failure",   # paywall / 403 / missing file -- WE NEVER SAW THE DATA
    "empty_result",     # we looked and there is nothing -- a real research answer
    "timeout",
    "rate_limit",       # incl. an account spend cap: the agent never got to think
    "api_error",        # the session itself failed -- no research happened at all
    "parse_failure",    # got something back, could not turn it into a Finding
    "tool_denied",
]

# access_failure vs empty_result is the distinction the exam asks about. The first leaves
# the question unasked; the second answers it with "nothing published".
#
# api_error is in here for a reason learned the hard way: a run once hit the account's API
# spend cap, every agent returned in 4 seconds at $0.000, and the system recorded seven
# `empty_result`s and announced 100% coverage. A dead session is not a research answer.
BLOCKS_COVERAGE = {"access_failure", "timeout", "rate_limit", "api_error",
                   "parse_failure", "tool_denied"}
RETRY_BY_DEFAULT = {"access_failure", "timeout", "rate_limit"}

BAD_SOURCES = {"", "-", "n/a", "unknown", "various", "various sources",
               "multiple sources", "industry reports"}


# ---------------------------------------------------------------------------
# 2. Turning a worker's raw JSON into the above (this is where lies get caught)
# ---------------------------------------------------------------------------


def make_finding(raw: dict, subtopic: str, worker_id: str) -> Finding:
    """Build one Finding. Raises ValueError if it is not citable."""
    url = str(raw.get("source_url") or raw.get("source") or "").strip()
    if url.lower() in BAD_SOURCES:
        raise ValueError(f"no usable source_url (got {url!r})")
    if not raw.get("claim"):
        raise ValueError("no claim")

    return Finding(
        claim=raw["claim"],
        evidence=raw.get("evidence", ""),
        source_url=url,
        confidence=float(raw.get("confidence", 0.5)),
        worker_id=raw.get("worker_id") or worker_id,
        subtopic=raw.get("subtopic") or subtopic,
        source_title=raw.get("source_title", ""),
        timestamp=raw.get("timestamp") or raw.get("date") or None,
        retrieved_at=raw.get("retrieved_at") or datetime.now(timezone.utc).date().isoformat(),
    )


def parse_worker_output(payload: dict, subtopic: str, worker_id: str
                        ) -> tuple[list[Finding], list[WorkerError]]:
    """Read one worker's JSON. Returns (findings, errors) -- always both.

    THE IMPORTANT LINE is at the bottom: findings=[] AND errors=[] becomes an explicit
    empty_result. A subagent that times out and returns {"findings": []} looks exactly like
    one that searched properly and found nothing. Without this conversion the coordinator
    marks the subtopic covered and the report quietly omits it.
    """
    findings: list[Finding] = []
    errors: list[WorkerError] = []

    for raw in payload.get("findings") or []:
        try:
            findings.append(make_finding(raw, subtopic, worker_id))
        except (ValueError, TypeError, KeyError) as exc:
            # Not discarded -- demoted to a visible failure, with the payload kept so the
            # coordinator can retry it rather than lose it.
            errors.append(WorkerError(
                error_type="parse_failure", message=f"unusable finding: {exc}",
                attempted_action=str(raw)[:150], is_retryable=False,
                worker_id=worker_id, subtopic=subtopic, partial_results=[raw],
            ))

    for raw in payload.get("errors") or []:
        error_type = raw.get("error_type") or raw.get("category") or "parse_failure"
        if error_type not in ERROR_TYPES:
            error_type = "parse_failure"
        errors.append(WorkerError(
            error_type=error_type,
            message=raw.get("message", "no message"),
            attempted_action=raw.get("attempted_action") or raw.get("attempted", "<not recorded>"),
            is_retryable=bool(raw.get("is_retryable", error_type in RETRY_BY_DEFAULT)),
            worker_id=worker_id,
            subtopic=raw.get("subtopic") or subtopic,
            partial_results=list(raw.get("partial_results") or []),
        ))

    if not findings and not errors:
        errors.append(WorkerError(
            error_type="empty_result",
            message="worker returned no findings and no errors",
            attempted_action=f"subtopic: {subtopic}", is_retryable=False,
            worker_id=worker_id, subtopic=subtopic,
        ))

    return findings, errors


# ---------------------------------------------------------------------------
# 3. Conflicts, coverage -- computed in code, never asked of the model
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,.]*")

# Words that say nothing about WHAT is being claimed. Stripped before grouping, so
# "the market reached $91B" and "market size was $71B" land together.
_STOPWORDS = frozenset("""
a an the of in on at to for by from with and or is are was were be been will would could
about approximately roughly over under more than most reached totaled representing
according reports states estimates projects accounting had has have its their
""".split())


def _numbers(text: str) -> str:
    """The figures a claim asserts, as a comparable signature."""
    return "|".join(n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text))


def _question_key(finding: Finding) -> str:
    """The question a claim answers, reduced to something comparable.

    Numbers are stripped: they are the ANSWER, not the question. Leaving them in would
    make every disagreeing pair look like two unrelated claims, which is exactly how
    conflicts go undetected.

    Deliberately blunt -- a shared subtopic plus the same handful of content words. A live
    run showed why the subtopic alone is not enough: five findings about China's LFP share,
    global installs, and US stationary storage all sat in one subtopic and were flagged as
    contradicting each other. They were answering different questions.
    """
    text = _NUMBER.sub(" ", finding.claim.lower())
    words = [w for w in re.findall(r"[a-z]+", text) if w not in _STOPWORDS and len(w) > 2]
    return f"{finding.subtopic.lower().strip()}::{' '.join(sorted(set(words))[:5])}"


def find_conflicts(findings: list[Finding]) -> list[list[Finding]]:
    """Group findings that answer the SAME QUESTION with DIFFERENT numbers.

    Same question + same numbers = corroboration, the strongest thing a report has.
    Same question + different numbers = a conflict; both figures go in the report with
    their own sources.

    NEVER average them. $91B and $71B do not become $81B -- no source published $81B, so
    the report would be citing two sources for a number neither of them wrote.
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(_question_key(f), []).append(f)

    conflicts = []
    for group in groups.values():
        numeric = [f for f in group if _numbers(f.claim)]
        if len(numeric) > 1 and len({_numbers(f.claim) for f in numeric}) > 1:
            conflicts.append(numeric)
    return conflicts


def lead_of(conflict: list[Finding]) -> tuple[Finding, str]:
    """Which figure to lead with: highest confidence, then most recent source.

    Choosing a lead is a PRESENTATION decision. The other figures stay in the report's
    contested section -- picking a winner is fine, deleting the loser is not.
    """
    best = max(conflict, key=lambda f: (f.confidence, f.timestamp or ""))
    top = [f for f in conflict if f.confidence == best.confidence]
    return best, ("higher confidence" if len(top) == 1 else "more recent source")


def split_findings(findings: list[Finding]) -> tuple[list[Finding], list[list[Finding]]]:
    """(well_established, contested). The report's two required sections, decided in code.

    Well-established = every finding not caught up in a conflict.
    Contested = the conflict groups, whole.
    """
    conflicts = find_conflicts(findings)
    in_conflict = {id(f) for group in conflicts for f in group}
    return [f for f in findings if id(f) not in in_conflict], conflicts


def check_decomposition(subtopics: list[str]) -> list[str]:
    """Complaints about the coordinator's plan, before judging its findings.

    The failure this catches: "EV battery market" split into one subtopic, researched
    thoroughly, and reported as full coverage.
    """
    problems = []
    if len(subtopics) < MIN_SUBTOPICS:
        problems.append(f"narrow decomposition: {len(subtopics)} subtopics, "
                        f"expected at least {MIN_SUBTOPICS}")
    lowered = [s.lower().strip() for s in subtopics]
    if len(set(lowered)) != len(lowered):
        problems.append("duplicate subtopics")
    for s in subtopics:
        if len(s.split()) < 2:
            problems.append(f"subtopic too vague to dispatch: {s!r}")
    return problems


def coverage_notes(subtopics: list[str], findings: list[Finding],
                   errors: list[WorkerError]) -> list[str]:
    """Subtopics that were planned but never actually answered.

    Done in code on purpose. Asking the model "did you cover everything?" in the same
    session that produced the plan gets you "yes" -- it is reviewing its own reasoning.
    """
    answered = {f.subtopic.lower().strip() for f in findings}
    answered |= {e.subtopic.lower().strip() for e in errors
                 if e.error_type not in BLOCKS_COVERAGE}
    blocked = {e.subtopic.lower().strip(): e for e in errors
               if e.error_type in BLOCKS_COVERAGE}

    notes = []
    for s in subtopics:
        key = s.lower().strip()
        if key in answered:
            continue
        reason = blocked[key].error_type if key in blocked else "never dispatched"
        notes.append(f"{s} -- {reason}")
    return notes


def needs_redelegation(subtopic: str, errors: list[WorkerError]) -> bool:
    """Is this subtopic worth one more attempt?

    Only if something retryable failed. Re-asking after an empty_result puts the same
    question to the same corpus and buys the same nothing at full price.
    """
    mine = [e for e in errors if e.subtopic.lower().strip() == subtopic.lower().strip()]
    return any(e.is_retryable for e in mine)


# ---------------------------------------------------------------------------
# 4. What one full run produces
# ---------------------------------------------------------------------------


@dataclass
class AgentRun:
    """One agent session: what it was asked, what it did, what it cost.

    `tools_used` is the receipt for the tool grant. The synthesis agent's list must come
    back empty of search tools -- not because it was told not to search, but because it
    had nothing to search with.
    """

    role: str
    subtopic: str
    text: str = ""                          # its raw reply, before parsing
    tools_used: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    cost_usd: float = 0.0
    terminal_reason: str = ""
    failure: "WorkerError | None" = None     # set when the session itself died


@dataclass
class ResearchResult:
    query: str
    topic: str = ""
    timeframe: str = ""
    subtopics: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[WorkerError] = field(default_factory=list)
    conflicts: list[list[Finding]] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    decomposition_problems: list[str] = field(default_factory=list)
    report: str = ""
    agent_runs: list[AgentRun] = field(default_factory=list)
    output_dir: "Path | None" = None
    seconds: float = 0.0
    cost_usd: float = 0.0

    @property
    def parallel_saving(self) -> str:
        """Proof of parallelism, in wall-clock seconds rather than in prose.

        Agents that ran at the same time cost you the SLOWEST one. Agents that ran one
        after another cost you the SUM. Printing both is the measurement -- a coordinator
        that says "running these in parallel" and then waits for each in turn is not
        contradicted by its own transcript, only by this number.
        """
        collectors = [r for r in self.agent_runs if r.role in
                      ("web_search", "document_analysis")]
        if len(collectors) < 2:
            return "single agent -- nothing to parallelize"
        total = sum(r.elapsed for r in collectors)
        slowest = max(r.elapsed for r in collectors)
        return (f"{len(collectors)} agents: {total:.0f}s of work in "
                f"{slowest:.0f}s of waiting (saved {total - slowest:.0f}s)")

    @property
    def well_established(self) -> list[Finding]:
        return split_findings(self.findings)[0]

    @property
    def coverage(self) -> float:
        if not self.subtopics:
            return 0.0
        return 1 - len(self.coverage_notes) / len(self.subtopics)

    @property
    def sources(self) -> list[str]:
        """Deduplicated source list for the report's Sources section."""
        seen: list[str] = []
        for f in self.findings:
            label = f"{f.source_title} -- {f.source_url}" if f.source_title else f.source_url
            if label not in seen:
                seen.append(label)
        return seen

    def summary(self) -> str:
        return (f"{len(self.subtopics)} subtopics | {len(self.findings)} findings | "
                f"{len(self.errors)} errors | {len(self.conflicts)} conflicts | "
                f"{len(self.coverage_notes)} gaps | coverage {self.coverage:.0%}")
