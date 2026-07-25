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
import os
import sqlite3
import sys
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.agent import root_agent
from config import KUZU_PATH, SQLITE_PATH
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


async def run_loop() -> None:
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
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        # A new iteration starts every time the detector speaks up again.
        if event.author == FIRST_AGENT and iteration == 0:
            iteration = 1
            banner(f"ITERATION {iteration}")

        # Tool calls / results - the interesting part of the trace.
        for call in event.get_function_calls() or []:
            print(f"  [{event.author}] -> tool {call.name}({dict(call.args or {})})")
        for resp in event.get_function_responses() or []:
            print(f"  [{event.author}] <- {resp.name} returned {resp.response}")

        # Final natural-language output of each agent.
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts).strip()
            if text:
                print(f"  [{event.author}] {text}")
            if event.author == "validator_agent":
                iteration += 1
                if not (event.actions and event.actions.escalate):
                    banner(f"ITERATION {iteration}")

        if event.actions and event.actions.escalate:
            escalated = True
            print(f"\n  *** escalate raised by {event.author} - breaking the loop ***")

    banner("LOOP FINISHED")
    print(f"  iterations run : {min(iteration, root_agent.max_iterations)}")
    print(f"  stopped because: {'validator escalated (data is clean)' if escalated else 'max_iterations reached'}")


def main() -> None:
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set. See README.md -> Setup.")

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
