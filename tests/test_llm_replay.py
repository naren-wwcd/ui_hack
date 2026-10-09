"""Offline replay tests; no API key or network access is needed."""

from __future__ import annotations

import json
from pathlib import Path

from backend import llm


def test_replay_returns_trace_steps_in_order(tmp_path: Path, monkeypatch) -> None:
    trace = {
        "scenario": "bad_config",
        "steps": [
            {
                "thought": "Inspect the alert.",
                "action": {"type": "tool_call", "tool": "get_alert", "args": {}},
            },
            {
                "thought": "Check database connectivity.",
                "action": {
                    "type": "tool_call",
                    "tool": "check_connectivity",
                    "args": {"target": "db"},
                },
            },
        ],
    }
    (tmp_path / "bad_config.json").write_text(json.dumps(trace), encoding="utf-8")
    monkeypatch.setattr(llm, "TRACE_DIR", tmp_path)
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm.reset_replay()

    messages = [{"role": "user", "content": "Scenario bad_config; investigate."}]
    first = llm.next_step(101, messages)
    # A real tool result in context does not alter or get replaced by replay.
    messages.append({
        "role": "tool",
        "name": "get_alert",
        "content": '{"title":"5xx on /checkout","error_rate":0.25}',
    })
    second = llm.next_step(101, messages)

    assert first["action"]["tool"] == "get_alert"
    assert second["action"]["tool"] == "check_connectivity"
    assert llm.get_llm_status()["mode"] == "REPLAY"


def test_replay_infers_scenario_from_hidden_incident_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pool_leak.json").write_text(
        json.dumps({
            "scenario": "pool_leak",
            "steps": [{
                "thought": "Check pool pressure.",
                "action": {
                    "type": "tool_call",
                    "tool": "get_metrics",
                    "args": {"window_s": 60},
                },
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm, "TRACE_DIR", tmp_path)
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        llm,
        "_scenario_for_incident",
        lambda incident_id, messages: "pool_leak",
    )

    result = llm.next_step(202, [{"role": "user", "content": "Investigate."}])
    assert result["action"]["tool"] == "get_metrics"


def test_invalid_trace_shape_returns_error(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "bad_config.json").write_text(
        json.dumps({
            "scenario": "bad_config",
            "steps": [{"thought": "missing action"}],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm, "TRACE_DIR", tmp_path)
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = llm.next_step(
        303,
        [{"role": "user", "content": "Scenario bad_config"}],
    )
    assert result["error"] == "trace_unavailable"