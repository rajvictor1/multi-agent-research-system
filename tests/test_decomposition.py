"""Test 1 -- decomposition.

The failure being guarded against: a coordinator that splits "EV battery market" into one
subtopic, researches it thoroughly, and reports success.

    uv run pytest -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordinator import load_query                      # noqa: E402
from models import MIN_SUBTOPICS, check_decomposition   # noqa: E402


def test_broad_topic_needs_at_least_five_subtopics():
    assert MIN_SUBTOPICS == 5
    narrow = check_decomposition(["lithium-ion cell pricing"])
    assert any("narrow decomposition" in p for p in narrow)


def test_a_real_decomposition_passes():
    assert check_decomposition([
        "global market size estimates by year",
        "regional demand split",
        "cell and pack price trends",
        "raw material supply constraints",
        "policy and subsidy changes",
    ]) == []


def test_duplicate_subtopics_are_caught():
    problems = check_decomposition(["cell pricing", "cell pricing", "supply chain",
                                    "regional demand", "policy changes"])
    assert any("duplicate" in p for p in problems)


def test_one_word_subtopics_are_too_vague_to_dispatch():
    problems = check_decomposition(["pricing", "supply", "policy", "demand", "regulation"])
    assert len([p for p in problems if "too vague" in p]) == 5


def test_every_sample_query_file_lists_five_or_more_subtopics():
    """The query files are the yardstick a run is graded against, so they have to be
    broad themselves."""
    for path in sorted((Path(__file__).resolve().parent.parent / "sample_queries").glob("*.txt")):
        query = load_query(str(path))
        assert query["topic"], f"{path.name} has no TOPIC"
        assert query["timeframe"], f"{path.name} has no TIMEFRAME"
        assert len(query["subtopics"]) >= MIN_SUBTOPICS, f"{path.name} is too narrow"
        assert check_decomposition(query["subtopics"]) == [], path.name


def test_a_plain_question_still_loads():
    query = load_query("What year was the first lithium-ion battery sold?")
    assert query["subtopics"] == []
    assert query["topic"].startswith("What year")
