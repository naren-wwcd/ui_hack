"""Record a live Gemini decision sequence into traces/<scenario>.json.

Usage:
    python -m scripts.record_trace bad_config

This script creates a local incident with the scenario stored only as hidden
metadata for trace selection. It does not expose that metadata to Gemini. Tool
calls run through the project's read-only tool dispatcher; proposed fixes are
recorded but never executed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

SCENARIOS = (
    "bad_config",
    "pool_leak",
    "disk_full",
    "payment_down",
    "db_hung",
    "compound",
)


def record(scenario: str, max_steps: int) -> Path:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise RuntimeError("Set GEMINI_API_KEY to record a live trace.")
    os.environ["LLM_MODE"] = "auto"

    from backend.db import execute, init_db, now_iso
    from backend.llm import next_step, reset_replay, set_mode  # type: ignore[attr-defined]
    from backend.tools import run_tool

    init_db()
    incident_id = execute(
        """
        INSERT INTO incidents (title, severity, status, fault_hint_hidden, opened_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "SANDBOX trace recording",
            "high",
            "investigating",
            scenario,
            now_iso(),
        ),
    )
    reset_replay(incident_id)

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "Investigate the current SANDBOX incident using available evidence.",
        }
    ]
    recorded: list[dict[str, Any]] = []

    for _ in range(max_steps):
        result = next_step(incident_id, messages)
        if result.get("error"):
            raise RuntimeError(f"Live trace recording stopped: {result}")

        recorded.append(result)
        action = result["action"]
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

        if action["type"] == "tool_call":
            tool_result = run_tool(action["tool"], action.get("args", {}))
            compact = json.dumps(tool_result, ensure_ascii=False, separators=(",", ":"))
            if len(compact) > 2000:
                compact = compact[:2000] + "…[truncated]"
            messages.append(
                {
                    "role": "tool",
                    "name": action["tool"],
                    "content": compact,
                }
            )
        elif action["type"] in {"conclude", "escalate"}:
            break
        elif action["type"] == "propose_fix":
            # Keep trace recording read-only; do not execute proposed actions.
            break

    trace_path = Path(__file__).resolve().parents[1] / "traces" / f"{scenario}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps({"scenario": scenario, "steps": recorded}, indent=2),
        encoding="utf-8",
    )
    return trace_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--max-steps", type=int, default=24)
    args = parser.parse_args()
    if args.max_steps < 1:
        parser.error("--max-steps must be at least 1")
    print(record(args.scenario, args.max_steps))


if __name__ == "__main__":
    main()