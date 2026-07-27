"""Entry point: seed the data, build the graph, and let the agents loop.

    python main.py

What happens, in order:
    1. shop.db is (re)created with 4 intentional defects.
    2. The Kuzu graph mirror is built from shop.db.
    3. The LoopAgent runs detect -> plan -> fix -> validate, up to 5 times,
       stopping early as soon as the validator escalates.
    4. A final SQL verification proves the database is clean.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from config import KUZU_PATH, SQLITE_PATH, describe_model, missing_credentials
from db.seed_sqlite import seed
from graph.build_kuzu import sync_from_sqlite
from tools.quality_tools import detect_issues

APP_NAME = "data_quality_guardian"
USER_ID = "learner"
SESSION_ID = "session-1"

# Agents that mark the start / end of one loop iteration, used only for logging.
FIRST_AGENT = "detector_agent"


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def verify_sqlite() -> int:
    """Run the ground-truth SQL checks and print them. Returns defect count."""
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    checks = {
        "orphan orders": "SELECT COUNT(*) AS n FROM orders o "
        "LEFT JOIN customers c ON c.id = o.customer_id WHERE c.id IS NULL",
        "orphan order_items": "SELECT COUNT(*) AS n FROM order_items oi "
        "LEFT JOIN products p ON p.id = oi.product_id "
        "LEFT JOIN orders o ON o.id = oi.order_id WHERE p.id IS NULL OR o.id IS NULL",
        "negative stock": "SELECT COUNT(*) AS n FROM products WHERE stock < 0",
        "price mismatches": "SELECT COUNT(*) AS n FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id "
        "WHERE ABS(oi.unit_price - p.price) > 0.001",
    }
    total = 0
    for label, sql in checks.items():
        n = conn.execute(sql).fetchone()["n"]
        total += n
        print(f"  {label:<20} {n}")
    conn.close()
    return total


def issue_key(issue: dict) -> str:
    return f"{issue['issue_type']}:{issue['primary_key']}"


def print_state(label: str, issues: list[dict]) -> None:
    """Print the outstanding issues at a point in time."""
    print(f"\n  --- {label}: {len(issues)} issue(s) ---")
    for issue in issues:
        print(f"      {issue_key(issue):<28} {issue['description']}")
    if not issues:
        print("      (none)")


def print_diff(before: list[dict], after: list[dict]) -> None:
    """Show which issues this iteration removed (and any new ones it exposed)."""
    before_keys = {issue_key(i) for i in before}
    after_keys = {issue_key(i) for i in after}
    resolved = sorted(before_keys - after_keys)
    appeared = sorted(after_keys - before_keys)
    print(f"  --- iteration delta: {len(resolved)} resolved, {len(appeared)} newly exposed ---")
    for key in resolved:
        print(f"      RESOLVED  {key}")
    for key in appeared:
        print(f"      NEW       {key}")


def print_tool_result(author: str, name: str, response) -> None:
    """Pretty-print a tool response; apply_fix gets a before/after row dump."""
    if name == "apply_fix" and isinstance(response, dict) and "before" in response:
        pk = f"{response['issue_type']}:{response['primary_key']}"
        print(f"  [{author}] <- apply_fix {pk}  ({response['rows_changed']} row(s) changed)")
        for sql in response["sql"]:
            print(f"                 sql   : {sql}")
        print(f"                 before: {response['before']}")
        print(f"                 after : {response['after']}")
        print(
            f"                 issues: {response['issues_before']} -> {response['issues_after']}"
        )
    else:
        print(f"  [{author}] <- {name} returned {response}")


async def run_loop() -> None:
    # Imported here (not at module import time) so the credential check in main()
    # runs before we try to build a model client.
    from agents.agent import root_agent

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

    message = types.Content(
        role="user",
        parts=[types.Part(text="Clean the shop database. Run the full detect-fix-validate cycle.")],
    )

    iteration = 0
    escalated = False
    issues_at_start: list[dict] = []

    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        # A new iteration starts every time the detector speaks up again.
        if event.author == FIRST_AGENT and not iteration:
            iteration = 1
            banner(f"ITERATION {iteration}")
            issues_at_start = detect_issues()["issues"]
            print_state("BEFORE this iteration", issues_at_start)

        # Tool calls / results - the interesting part of the trace.
        for call in event.get_function_calls() or []:
            print(f"  [{event.author}] -> tool {call.name}({dict(call.args or {})})")
        for resp in event.get_function_responses() or []:
            print_tool_result(event.author, resp.name, resp.response)

        # Final natural-language output of each agent.
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts).strip()
            if text:
                print(f"  [{event.author}] {text}")

        if event.actions and event.actions.escalate:
            escalated = True
            print(f"\n  *** escalate raised by {event.author} - breaking the loop ***")

        # End of a pass: show what this iteration actually changed.
        if event.author == "validator_agent" and event.is_final_response():
            issues_now = detect_issues()["issues"]
            print_state("AFTER this iteration", issues_now)
            print_diff(issues_at_start, issues_now)
            if not escalated:
                iteration += 1
                banner(f"ITERATION {iteration}")
                issues_at_start = issues_now
                print_state("BEFORE this iteration", issues_at_start)

    banner("LOOP FINISHED")
    print(f"  iterations run : {min(iteration, root_agent.max_iterations)}")
    print(f"  stopped because: {'validator escalated (data is clean)' if escalated else 'max_iterations reached'}")


def main() -> None:
    problem = missing_credentials()
    if problem:
        sys.exit(f"{problem}  See README.md -> Setup.")
    print(f"model: {describe_model()}")

    # Fail fast on a model id the endpoint does not serve: the provider answers
    # such a request with a bare '404 page not found', which looks like a broken
    # URL and sends you hunting in the wrong place.
    try:
        available = config.list_remote_models()
    except Exception:  # offline / endpoint without a /models route - skip the check
        available = []
    if available and config.MODEL_NAME not in available:
        hint = [m for m in available if config.MODEL_NAME.split("/")[0] in m][:5]
        sys.exit(
            f"Model '{config.MODEL_NAME}' is not served by {config.MODEL_PROVIDER}.\n"
            f"Similar ids: {hint or 'run python check_model.py for the full list'}"
        )
    # Same check for the failover list - an invalid fallback surfaces as a
    # confusing '404 page not found' only *after* the primary model hiccups.
    bad = [m for m in config.FALLBACK_MODELS if available and m not in available]
    if bad:
        sys.exit(f"ADK_FALLBACK_MODELS contains ids this endpoint does not serve: {bad}")
    if config.FALLBACK_MODELS:
        print(f"fallbacks: {', '.join(config.FALLBACK_MODELS)}")

    banner("STEP 1  seed SQLite (with 4 intentional defects)")
    seed(SQLITE_PATH)
    print(f"  wrote {SQLITE_PATH}")

    banner("STEP 2  build the Kuzu graph mirror")
    sync_from_sqlite(SQLITE_PATH, KUZU_PATH)
    print(f"  wrote {KUZU_PATH}")

    banner("STEP 3  issues before the agents run")
    for issue in detect_issues()["issues"]:
        print(f"  - {issue['issue_type']:<18} pk={issue['primary_key']:<6} {issue['description']}")

    banner("STEP 4  run the autonomous agent loop")
    asyncio.run(run_loop())

    banner("STEP 5  final SQLite verification")
    total = verify_sqlite()
    print(f"\n  TOTAL REMAINING DEFECTS: {total}")
    print("  RESULT: DATABASE IS CLEAN" if total == 0 else "  RESULT: DEFECTS REMAIN")


if __name__ == "__main__":
    main()
