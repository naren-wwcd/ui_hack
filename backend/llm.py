"""Gemini live decisions and offline trace replay for AutoHeal."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

_VALID_ACTIONS = {
    "tool_call",
    "update_hypotheses",
    "propose_fix",
    "escalate",
    "conclude",
}
TRACE_DIR = Path(__file__).resolve().parents[1] / "traces"

_lock = threading.RLock()
_status: dict[str, str] = {"mode": "REPLAY", "reason": "initializing"}
_replay_positions: dict[int, int] = {}
_model: Any = None


def get_llm_status() -> dict[str, str]:
    """Return the current mode and the reason for that mode."""
    with _lock:
        return dict(_status)


def _switch_mode(mode: str, reason: str) -> None:
    """Update mode and publish the llm_mode event when it changes."""
    normalized = mode.upper()
    if normalized not in {"LIVE", "REPLAY"}:
        raise ValueError("mode must be LIVE or REPLAY")

    event_data = {"mode": normalized, "reason": reason}
    with _lock:
        changed = _status != event_data
        _status.clear()
        _status.update(event_data)

    if changed:
        try:
            from .events import publish

            publish("llm_mode", event_data)
        except Exception:
            # The LLM should remain usable even if event infrastructure is not
            # initialized yet, for example while running a focused unit test.
            pass


def _compact_messages(messages: list[dict[str, Any]]) -> str:
    """Serialize recent context while keeping individual tool results small."""
    compact: list[str] = []
    for message in messages[-14:]:
        if not isinstance(message, dict):
            continue
        value = dict(message)

        # Tools and the agent may use different names for their result field.
        for key in ("result", "tool_result", "content", "output"):
            field = value.get(key)
            if isinstance(field, str) and len(field) > 2000:
                value[key] = field[:2000] + "…[truncated]"
            elif isinstance(field, (dict, list)):
                rendered = json.dumps(field, ensure_ascii=False, separators=(",", ":"))
                if len(rendered) > 2000:
                    value[key] = rendered[:2000] + "…[truncated]"

        try:
            compact.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            compact.append(str(value)[:2000])

    text = "\n".join(compact)
    if len(text) > 12000:
        text = "[Earlier investigation context omitted]\n" + text[-12000:]
    return text


def _scenario_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """Infer a trace name from investigation evidence, not from alert title."""
    text = _compact_messages(messages).lower()
    clues = {
        "bad_config": ("connection refused db-prod-02", "db_host", "config diff"),
        "pool_leak": ("queuepool", "pool exhausted", "db_pool_used"),
        "disk_full": ("no space left on device", "disk_used_pct", "disk full"),
        "payment_down": ("upstream timeout payment-gw", "payment connectivity failed"),
        "db_hung": ("unresponsive", "connection latency", "db status"),
        "compound": ("compound", "no space left on device", "connection refused db-prod-02"),
    }

    # Explicit scenario labels in a user-visible test/demo message are usable
    # evidence for replay selection. They are never added to the LIVE prompt.
    for scenario in clues:
        if re.search(rf"\b{re.escape(scenario)}\b", text):
            return scenario

    scores: dict[str, int] = {}
    for scenario, phrases in clues.items():
        score = sum(text.count(phrase) for phrase in phrases)
        if score:
            scores[scenario] = score
    if not scores:
        return None

    # Compound evidence is a combination of independent fault signatures.
    if (
        scores.get("bad_config", 0)
        and scores.get("disk_full", 0)
    ):
        return "compound"

    return max(scores, key=scores.get)


def _scenario_for_incident(
    incident_id: int,
    messages: list[dict[str, Any]],
) -> str | None:
    inferred = _scenario_from_messages(messages)
    if inferred:
        return inferred

    # This lookup is used only to select an offline trace. The hidden hint is
    # never passed to Gemini or included in the agent's conversation context.
    try:
        from .db import query_one

        incident = query_one(
            "SELECT fault_hint_hidden FROM incidents WHERE id = ?",
            (incident_id,),
        )
        hint = incident.get("fault_hint_hidden") if incident else None
        if hint in {
            "bad_config",
            "pool_leak",
            "disk_full",
            "payment_down",
            "db_hung",
            "compound",
        }:
            return str(hint)
    except Exception:
        pass
    return None


def _validate_action(action: Any) -> bool:
    if not isinstance(action, dict):
        return False
    action_type = action.get("type")
    if action_type not in _VALID_ACTIONS:
        return False

    if action_type == "tool_call":
        return (
            isinstance(action.get("tool"), str)
            and isinstance(action.get("args", {}), dict)
        )

    if action_type == "update_hypotheses":
        hypotheses = action.get("hypotheses")
        return isinstance(hypotheses, list) and all(
            isinstance(item, dict)
            and isinstance(item.get("id"), (str, int))
            and isinstance(item.get("cause"), str)
            and isinstance(item.get("confidence"), (int, float))
            and isinstance(item.get("evidence_for", []), list)
            and isinstance(item.get("evidence_against", []), list)
            and isinstance(item.get("next_test", ""), str)
            for item in hypotheses
        )

    if action_type == "propose_fix":
        return (
            isinstance(action.get("fix_name"), str)
            and isinstance(action.get("confidence"), (int, float))
        )

    if action_type == "escalate":
        return isinstance(action.get("reason"), str)

    if action_type == "conclude":
        return isinstance(action.get("summary"), str)

    return False


def _validate_response(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    thought = value.get("thought")
    action = value.get("action")
    if not isinstance(thought, str) or not _validate_action(action):
        return None
    return {"thought": thought, "action": action}


def _parse_json(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # Some model versions wrap their JSON response in a Markdown fence.
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return _validate_response(parsed)


def _get_model() -> Any:
    global _model
    if _model is not None:
        return _model

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel(
        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        generation_config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    )
    return _model


def _is_replayable_provider_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in message
        for marker in (
            "quota",
            "resource exhausted",
            "429",
            "timeout",
            "timed out",
            "deadline",
            "unavailable",
            "503",
        )
    )


def _live_step(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Request one strict JSON action from Gemini, retrying invalid JSON once."""
    model = _get_model()
    context = _compact_messages(messages)
    system = (
        "You are AutoHeal's evidence-first agent operating in a local SANDBOX. "
        "Never assume a cause from the alert alone. Use only the read-only tools "
        "provided in the investigation context and only fixes in the listed "
        "remediation catalog. Do not blame these red herrings: cache miss ratio "
        "high, deprecated /v1/cart, slow /reports query, or occasional 404s. "
        "Prefer the least-risk supported fix. Escalate if confidence is below "
        "0.6 or no safe catalog fix fits. Quote relevant log evidence. "
        "Return exactly one JSON object with string key thought and object key "
        "action. Valid action types are tool_call, update_hypotheses, "
        "propose_fix, escalate, conclude."
    )
    prompt = f"{system}\n\nInvestigation context:\n{context}\n\nNext step JSON:"
    raw = ""

    for attempt in range(2):
        try:
            response = model.generate_content(
                prompt,
                request_options={"timeout": 20},
            )
            raw = str(getattr(response, "text", "") or "")
        except Exception as exc:
            if _is_replayable_provider_error(exc):
                return {
                    "error": "provider_unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "fallback_to_replay": True,
                }
            return {
                "error": "provider_error",
                "reason": f"{type(exc).__name__}: {exc}",
            }

        parsed = _parse_json(raw)
        if parsed:
            _switch_mode("LIVE", "Gemini returned a valid agent step")
            return parsed

        if attempt == 0:
            prompt += (
                '\n\nReturn valid JSON only. No Markdown or extra text. '
                'Required top-level shape: {"thought":"...","action":{"type":"..."}}'
            )

    return {"error": "invalid_json", "raw": raw[:4000]}


def _load_trace(scenario: str) -> list[dict[str, Any]]:
    path = TRACE_DIR / f"{scenario}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    steps = trace.get("steps") if isinstance(trace, dict) else trace
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"trace {path.name} has no steps")

    valid: list[dict[str, Any]] = []
    for step in steps:
        parsed = _validate_response(step)
        if parsed is None:
            raise ValueError(f"trace {path.name} contains an invalid step")
        valid.append(parsed)
    return valid


def _replay_step(incident_id: int, messages: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    scenario = _scenario_for_incident(incident_id, messages)
    if not scenario:
        return {
            "error": "trace_scenario_unknown",
            "reason": "Could not infer a replay scenario from incident evidence.",
        }

    try:
        steps = _load_trace(scenario)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "error": "trace_unavailable",
            "reason": f"No usable replay trace for {scenario}: {exc}",
        }

    with _lock:
        position = _replay_positions.get(incident_id, 0)
        _replay_positions[incident_id] = position + 1

    _switch_mode("REPLAY", reason)
    # Once a trace is exhausted, keep returning its final decision. The real
    # caller still executes any tool_call against the live sandbox.
    return dict(steps[min(position, len(steps) - 1)])


def next_step(incident_id: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return one validated agent decision or an explicit error marker."""
    if not isinstance(messages, list):
        return {"error": "invalid_messages", "reason": "messages must be a list"}

    mode = os.getenv("LLM_MODE", "auto").strip().lower()
    if mode not in {"auto", "replay"}:
        mode = "auto"

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if mode == "replay":
        return _replay_step(incident_id, messages, "LLM_MODE=replay")
    if not api_key:
        return _replay_step(incident_id, messages, "GEMINI_API_KEY is not configured")

    live = _live_step(messages)
    if live.get("fallback_to_replay"):
        return _replay_step(
            incident_id,
            messages,
            str(live.get("reason", "Gemini quota or timeout; using replay")),
        )
    return live


def reset_replay(incident_id: int | None = None) -> None:
    """Reset replay cursors for one incident or for all incidents."""
    with _lock:
        if incident_id is None:
            _replay_positions.clear()
        else:
            _replay_positions.pop(incident_id, None)