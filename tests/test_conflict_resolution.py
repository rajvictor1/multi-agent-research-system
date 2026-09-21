"""Test 3 -- conflict resolution.

Two sources disagreeing must both reach the report. Picking a lead is fine; deleting the
loser is not, and averaging is never right.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import (  # noqa: E402
    Finding,
    find_conflicts,
    lead_of,
    split_findings,
)


def finding(claim, url, conf=0.8, when="2026-01-01", subtopic="market size"):
    return Finding(claim=claim, evidence=claim, source_url=url, confidence=conf,
                   worker_id="web_search", subtopic=subtopic, timestamp=when)


def test_two_sources_disagreeing_are_detected():
    conflicts = find_conflicts([
        finding("Market at 91 billion in 2024.", "https://mam"),
        finding("Market at 12 billion in 2024.", "https://outlier"),
    ])
    assert len(conflicts) == 1
    assert len(conflicts[0]) == 2


def test_the_same_figure_from_two_sources_is_corroboration_not_conflict():
    assert find_conflicts([
        finding("Market at 91 billion in 2024.", "https://mam"),
        finding("Market at 91 billion in 2024.", "https://iea"),
    ]) == []


def test_the_losing_figure_is_kept():
    """The whole point. The report needs both numbers, not just the winner."""
    strong = finding("Market at 91 billion in 2024.", "https://mam", conf=0.9)
    weak = finding("Market at 12 billion in 2024.", "https://outlier", conf=0.4)
    conflict = find_conflicts([strong, weak])[0]

    lead, why = lead_of(conflict)
    assert lead is strong
    assert why == "higher confidence"
    assert weak in conflict              # still there -- goes in the contested section


def test_confidence_beats_recency():
    """A fresh blog post does not outrank an audited filing."""
    old_strong = finding("Market at 91 billion.", "https://filing", conf=0.95, when="2025-06-01")
    new_weak = finding("Market at 12 billion.", "https://blog", conf=0.4, when="2026-06-01")
    lead, why = lead_of([old_strong, new_weak])
    assert lead is old_strong
    assert why == "higher confidence"


def test_recency_breaks_a_confidence_tie():
    older = finding("Market at 91 billion.", "https://a", conf=0.8, when="2025-01-01")
    newer = finding("Market at 97 billion.", "https://b", conf=0.8, when="2026-01-01")
    lead, why = lead_of([older, newer])
    assert lead is newer
    assert why == "more recent source"


def test_nothing_is_averaged():
    """91 and 12 must never become 51.5 -- no source published 51.5."""
    conflict = find_conflicts([
        finding("Market at 91 billion in 2024.", "https://mam", conf=0.9),
        finding("Market at 12 billion in 2024.", "https://outlier", conf=0.4),
    ])[0]
    reported = {f.claim for f in conflict}
    assert len(reported) == 2
    assert not any("51" in claim for claim in reported)


def test_split_gives_the_reports_two_sections():
    agreed = finding("Pack prices fell below $100/kWh.", "https://iea", subtopic="pricing")
    a = finding("Market at 91 billion in 2024.", "https://mam", subtopic="size")
    b = finding("Market at 12 billion in 2024.", "https://outlier", subtopic="size")

    well_established, contested = split_findings([agreed, a, b])
    assert well_established == [agreed]
    assert len(contested) == 1
    assert {f.source_url for f in contested[0]} == {"https://mam", "https://outlier"}


def test_different_subtopics_with_the_same_number_do_not_collide():
    assert find_conflicts([
        finding("Growth was 30 percent.", "https://a", subtopic="growth"),
        finding("Share was 30 percent.", "https://b", subtopic="share"),
    ]) == []


def test_every_finding_that_survives_is_citable():
    f = finding("Market at 91 billion in 2024.", "https://mam")
    assert f.source_url
    assert "https://mam" in f.cite()
