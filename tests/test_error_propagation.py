"""Test 2 -- error propagation.

The scenario's signature failure: a subagent times out and returns {"findings": []}. It is
well-formed, it reads as success, and it removes a subtopic from the report without ever
saying so. If only one test file survives, keep this one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import (  # noqa: E402
    coverage_notes,
    needs_redelegation,
    parse_worker_output,
)


def test_a_timeout_returning_empty_success_becomes_a_recorded_error():
    findings, errors = parse_worker_output(
        {"findings": [], "status": "ok"}, "2026 projections", "web_search")
    assert findings == []
    assert len(errors) == 1
    assert errors[0].error_type == "empty_result"


def test_access_failure_is_not_the_same_as_empty_result():
    """The distinction the exam asks about: never looked vs looked and found nothing."""
    _, blocked = parse_worker_output({"errors": [{
        "error_type": "access_failure", "message": "403 from the analyst portal",
        "attempted_action": "https://example.com/report", "is_retryable": True}]},
        "pricing", "web_search")
    _, empty = parse_worker_output({"errors": [{
        "error_type": "empty_result", "message": "no 2026 figures published",
        "attempted_action": "2026 pack price", "is_retryable": False}]},
        "pricing", "web_search")

    planned = ["pricing"]
    # access_failure leaves the question unasked -> it is a coverage gap.
    assert coverage_notes(planned, [], blocked) == ["pricing -- access_failure"]
    # empty_result ANSWERS the question with "nothing published" -> not a gap.
    assert coverage_notes(planned, [], empty) == []


def test_partial_results_survive_a_failure():
    """A worker that found two sources before dying gives up two sources, not zero."""
    _, errors = parse_worker_output({"errors": [{
        "error_type": "timeout", "message": "exceeded wall clock",
        "attempted_action": "lithium supply 2026", "is_retryable": True,
        "partial_results": [{"url": "https://a"}, {"url": "https://b"}]}]},
        "supply chain", "web_search")
    assert len(errors[0].partial_results) == 2


def test_only_retryable_failures_get_re_delegated():
    _, timed_out = parse_worker_output({"errors": [{
        "error_type": "timeout", "message": "slow", "attempted_action": "q",
        "is_retryable": True}]}, "supply chain", "web_search")
    _, nothing = parse_worker_output({"errors": [{
        "error_type": "empty_result", "message": "none", "attempted_action": "q",
        "is_retryable": False}]}, "supply chain", "web_search")

    assert needs_redelegation("supply chain", timed_out) is True
    # Re-asking the same corpus buys the same silence at full price.
    assert needs_redelegation("supply chain", nothing) is False


def test_a_claim_with_no_source_becomes_a_visible_parse_failure():
    """Lost provenance surfaces as an error, not as an uncitable line in the report."""
    findings, errors = parse_worker_output({"findings": [
        {"claim": "The market reached $91B in 2024.", "source_url": "", "confidence": 0.9}]},
        "market size", "web_search")
    assert findings == []
    assert errors[0].error_type == "parse_failure"
    assert errors[0].partial_results          # the payload is kept, not thrown away


def test_placeholder_sources_are_refused():
    _, errors = parse_worker_output({"findings": [
        {"claim": "The market reached $91B.", "source_url": "various sources",
         "confidence": 0.9}]}, "market size", "web_search")
    assert errors[0].error_type == "parse_failure"


def test_an_unknown_error_type_does_not_crash_the_run():
    _, errors = parse_worker_output(
        {"errors": [{"error_type": "vibes", "message": "hmm"}]}, "x", "web_search")
    assert errors[0].error_type == "parse_failure"


def test_a_subtopic_nobody_was_sent_shows_up_as_a_gap():
    """The worst gap: planned, forgotten, and invisible without this check."""
    findings, errors = parse_worker_output({"findings": [
        {"claim": "Market at $91B in 2024.", "evidence": "$91B",
         "source_url": "https://mam", "confidence": 0.9}]}, "market size", "web_search")
    notes = coverage_notes(["market size", "supply chain"], findings, errors)
    assert notes == ["supply chain -- never dispatched"]
