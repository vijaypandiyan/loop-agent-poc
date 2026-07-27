"""The multi-agent system: four specialists inside one autonomous loop.

    root_agent = LoopAgent(max_iterations=5)
                    └── fix_cycle = SequentialAgent
                            ├── detector_agent   (tool: detect_issues)  -> state["issues"]
                            ├── planner_agent                            -> state["plan"]
                            ├── fixer_agent      (tool: apply_fix)       -> state["fix_report"]
                            └── validator_agent  (tool: count_issues)    -> state["validation"]
                                                  ^ sets escalate -> loop stops

Two ADK ideas make this work:

1. **Shared session state.** ``output_key="issues"`` stores an agent's final
   text under that key; the next agent reads it by writing ``{issues}`` inside
   its instruction.  That is the entire "hand-off" mechanism - no custom glue.

2. **Escalation.** ``tool_context.actions.escalate = True`` (set inside
   ``count_issues``) bubbles up as ``EventActions.escalate`` and tells the
   enclosing ``LoopAgent`` to stop *now*.  Without it the loop would simply run
   ``max_iterations`` times.
"""

import sys
from pathlib import Path

from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent
from google.adk.tools import FunctionTool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import build_model  # noqa: E402
from tools.quality_tools import apply_fix, count_issues, detect_issues  # noqa: E402

# One model object shared by all four agents: a Gemini model name, or a LiteLlm
# wrapper around DeepSeek / Qwen / any OpenAI-compatible endpoint (see config.py).
MODEL = build_model()

# ADK reads the signature + docstring of each function to build the tool schema.
detect_issues_tool = FunctionTool(func=detect_issues)
apply_fix_tool = FunctionTool(func=apply_fix)
count_issues_tool = FunctionTool(func=count_issues)

# --------------------------------------------------------------------------
# 1. Detector - the only agent allowed to look at the raw databases.
# --------------------------------------------------------------------------
detector_agent = LlmAgent(
    name="detector_agent",
    model=MODEL,
    description="Finds data-quality and referential-integrity issues.",
    instruction=(
        "You are the DETECTOR in a data-quality pipeline.\n"
        "Call the `detect_issues` tool exactly once.\n"
        "Then output the issues it returned as a JSON array, one object per issue "
        "with the keys issue_type, table, primary_key, description.\n"
        "If the tool reports zero issues, output exactly: []\n"
        "Output JSON only - no prose, no markdown fences."
    ),
    tools=[detect_issues_tool],
    output_key="issues",  # -> session.state["issues"]
)

# --------------------------------------------------------------------------
# 2. Planner - pure reasoning, no tools.  It only orders the work.
# --------------------------------------------------------------------------
planner_agent = LlmAgent(
    name="planner_agent",
    model=MODEL,
    description="Turns detected issues into an ordered repair plan.",
    instruction=(
        "You are the PLANNER. Here are the issues the detector found:\n\n"
        "{issues?}\n\n"
        "Produce an ordered repair plan as a JSON array. Each element must be "
        '{{"step": <n>, "issue_type": <type>, "primary_key": <pk>, "reason": <short why>}}.\n'
        "Ordering rules:\n"
        "  1. orphan_order first - deleting an order also removes its child rows, "
        "which can make later issues disappear.\n"
        "  2. orphan_order_item next.\n"
        "  3. negative_stock, then price_mismatch.\n"
        "If the issue list is empty output exactly: []\n"
        "Output JSON only - no prose, no markdown fences."
    ),
    output_key="plan",  # -> session.state["plan"]
)

# --------------------------------------------------------------------------
# 3. Fixer - the only agent allowed to write.
# --------------------------------------------------------------------------
fixer_agent = LlmAgent(
    name="fixer_agent",
    model=MODEL,
    description="Applies the planned fixes to SQLite and re-syncs the graph.",
    instruction=(
        "You are the FIXER. Execute this repair plan:\n\n"
        "{plan?}\n\n"
        "For every step call the `apply_fix` tool with that step's `issue_type` and "
        "`primary_key`. Call it once per step, in the given order. Never invent a "
        "primary key that is not in the plan.\n"
        "If a call fails, continue with the remaining steps.\n"
        "When finished, summarise in one line per step: "
        "'<issue_type> <primary_key> -> <status>'.\n"
        "If the plan is empty, reply exactly: NO FIXES NEEDED"
    ),
    tools=[apply_fix_tool],
    output_key="fix_report",
)

# --------------------------------------------------------------------------
# 4. Validator - re-measures reality and decides whether to keep looping.
# --------------------------------------------------------------------------
validator_agent = LlmAgent(
    name="validator_agent",
    model=MODEL,
    description="Re-counts issues and stops the loop when the data is clean.",
    instruction=(
        "You are the VALIDATOR. Call the `count_issues` tool exactly once.\n"
        "The tool itself raises the escalate signal that stops the loop when the "
        "count is zero - you do not need to do anything else.\n"
        "Report the result in one line: 'CLEAN - 0 issues remain' if the tool "
        "returned clean=true, otherwise 'DIRTY - <remaining> issues remain'."
    ),
    tools=[count_issues_tool],
    output_key="validation",
)

# --------------------------------------------------------------------------
# Composition: one pass = detect -> plan -> fix -> validate.
# The LoopAgent repeats that pass until the validator escalates (data is clean)
# or until max_iterations passes have run (safety valve against infinite loops).
# --------------------------------------------------------------------------
fix_cycle = SequentialAgent(
    name="fix_cycle",
    description="One detect -> plan -> fix -> validate pass.",
    sub_agents=[detector_agent, planner_agent, fixer_agent, validator_agent],
)

root_agent = LoopAgent(
    name="data_quality_guardian",
    description="Autonomously repairs the shop database until it is clean.",
    sub_agents=[fix_cycle],
    max_iterations=5,
)
