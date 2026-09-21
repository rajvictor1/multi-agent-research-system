"""agents/ -- the four subagents (the "spokes"). One file each, so you can read them alone.

    agents/web_search.py          finds sources on the web
    agents/document_analysis.py   reads documents already on disk
    agents/synthesis.py           reconciles findings -- HAS NO TOOLS
    agents/report_generation.py   writes the final cited report

Each file defines exactly four things:

    NAME          the id the coordinator spawns it by
    DESCRIPTION   what the coordinator reads when choosing who to send work to
    TOOLS         what it is allowed to do
    PROMPT        its instructions

TOOLS is the part that matters. It is the architecture, written down:

    coordinator        ["Task"]                          delegates, cannot research
    web_search         ["WebSearch", "WebFetch", "Read"] searches
    document_analysis  ["Read", "Grep", "Glob"]          reads files, CANNOT search
    synthesis          ["Read"]                          NO search tools
    report_generation  ["Read"]                          NO search tools

The two "Read only" grants are the important ones. Give synthesis WebSearch and it will
search: it spots a gap in the findings, fills it itself, and now one agent does two jobs
and nobody can tell collected evidence from improvised evidence. That is the "super-agent"
anti-pattern, and the fix is the tool list -- not a prompt asking it nicely not to.
"""

from claude_agent_sdk import AgentDefinition

from . import document_analysis, report_generation, synthesis, web_search

# Every worker returns this same JSON shape. One contract for all four means the
# coordinator has one parser instead of four, and no worker can invent its own way of
# saying "I failed".
# Kept short on purpose: this string is prepended to every agent's system prompt, on every
# session, so each line is paid for once per agent per run. Everything here earns its place.
CONTRACT = """
Return ONLY this JSON. No prose, no code fence:

{"findings":[{"claim":"one sentence, NO source inside it",
  "evidence":"the exact quote or figure (keep it short)",
  "source_url":"URL or file path","source_title":"publisher / document name",
  "timestamp":"YYYY-MM-DD the SOURCE was published, or null",
  "confidence":0.0-1.0,"subtopic":"your subtopic, word for word"}],
 "errors":[{"error_type":"access_failure|empty_result|timeout|rate_limit|parse_failure",
  "message":"what happened","attempted_action":"the exact query or path you tried",
  "is_retryable":true,"partial_results":[]}]}

RULES
- Never "source_url":"" or "various sources". Cannot name it? Emit an error instead.
- findings:[] together with errors:[] is INVALID. Found nothing -> empty_result.
- Paywall or 403 is access_failure (you never saw the data), NOT empty_result.
- timestamp is the source's own publication date, never today's.
- Failed halfway? Put what you had in partial_results.
"""

# The four modules, in the order the coordinator normally uses them.
MODULES = [web_search, document_analysis, synthesis, report_generation]


def build(roles: list[str] | None = None) -> dict[str, AgentDefinition]:
    """Turn the four modules into the AgentDefinition dict the SDK expects.

    Pass `roles` to leave some out. A one-fact question does not need a synthesis agent,
    and an agent that was never defined cannot be spawned out of habit -- deciding what
    EXISTS is a cheaper control than telling the model what not to use.
    """
    wanted = roles or [m.NAME for m in MODULES]
    return {
        module.NAME: AgentDefinition(
            description=module.DESCRIPTION,
            prompt=module.PROMPT + CONTRACT,   # its own instructions + the shared contract
            tools=module.TOOLS,
            model="inherit",                   # same model as the coordinator
        )
        for module in MODULES
        if module.NAME in wanted
    }


def tool_table() -> dict[str, list[str]]:
    """{agent name: its tools}. The UI prints this; it is the architecture in one glance."""
    return {module.NAME: module.TOOLS for module in MODULES}
