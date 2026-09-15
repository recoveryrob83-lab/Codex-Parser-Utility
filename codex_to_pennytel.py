#!/usr/bin/env python3
"""Convert one Codex rollout turn into a PennyTel schema-v1 import dataset.

The utility intentionally exports only fields PennyTel schema v1 already accepts.
Raw prompts, reasoning text, tool inputs, and tool outputs are never copied into
PennyTel output.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PENNYTEL_ROLES = ("Orchestrator", "Context Steward", "Implementer", "Critic", "Repair")
PENNYTEL_THINKING = ("Low", "Medium", "High", "ExtraHigh", "Max")
MODEL_MAP = {
    "gpt-6-astra": ("GPT-6 Astra", "openai:gpt-6-astra"),
    "gpt-5.6-sol": ("GPT-5.6 Sol", "openai:gpt-5.6-sol"),
    "gpt-5.6-luna": ("GPT-5.6 Luna", "openai:gpt-5.6-luna"),
}
THINKING_MAP = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "ExtraHigh",
    "extra_high": "ExtraHigh",
    "extrahigh": "ExtraHigh",
    "extra-high": "ExtraHigh",
    "max": "Max",
}
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    index: int
    timestamp: str | None
    type: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class TurnBounds:
    turn_id: str
    start_index: int
    end_index: int
    start_record: Record
    end_record: Record


def load_records(path: Path) -> list[Record]:
    records: list[Record] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParseError(f"Invalid JSON on line {line_no}: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ParseError(f"Line {line_no} is not a JSON object.")
            payload = value.get("payload")
            records.append(
                Record(
                    index=len(records),
                    timestamp=value.get("timestamp") if isinstance(value.get("timestamp"), str) else None,
                    type=value.get("type") if isinstance(value.get("type"), str) else None,
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
    if not records:
        raise ParseError("Codex rollout is empty.")
    return records


def find_turns(records: Iterable[Record]) -> list[TurnBounds]:
    starts: dict[str, Record] = {}
    turns: list[TurnBounds] = []
    for record in records:
        if record.type != "event_msg":
            continue
        event_type = record.payload.get("type")
        turn_id = record.payload.get("turn_id")
        if not isinstance(turn_id, str):
            continue
        if event_type == "task_started":
            starts[turn_id] = record
        elif event_type == "task_complete" and turn_id in starts:
            start = starts.pop(turn_id)
            turns.append(
                TurnBounds(
                    turn_id=turn_id,
                    start_index=start.index,
                    end_index=record.index,
                    start_record=start,
                    end_record=record,
                )
            )
    return turns


def choose_turn(turns: list[TurnBounds], turn_id: str | None) -> TurnBounds:
    if turn_id:
        for turn in turns:
            if turn.turn_id == turn_id:
                return turn
        raise ParseError(f"No completed Codex task found for turn {turn_id}.")
    if not turns:
        raise ParseError("No completed Codex task_started/task_complete pair found.")
    if len(turns) > 1:
        ids = ", ".join(turn.turn_id for turn in turns)
        raise ParseError(f"Rollout contains multiple completed turns ({ids}). Pass --turn-id explicitly.")
    return turns[0]


def first_payload(records: Iterable[Record], record_type: str) -> dict[str, Any]:
    for record in records:
        if record.type == record_type:
            return record.payload
    return {}


def matching_turn_context(records: Iterable[Record], turn_id: str) -> dict[str, Any]:
    contexts = [
        record.payload
        for record in records
        if record.type == "turn_context" and record.payload.get("turn_id") == turn_id
    ]
    return contexts[-1] if contexts else {}


def token_total(record: Record) -> dict[str, int] | None:
    if record.type != "event_msg" or record.payload.get("type") != "token_count":
        return None
    info = record.payload.get("info")
    if not isinstance(info, dict):
        return None
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        return None
    parsed: dict[str, int] = {}
    for key in TOKEN_KEYS:
        value = total.get(key, 0)
        if not isinstance(value, int) or value < 0:
            return None
        parsed[key] = value
    return parsed


def delta_tokens(records: list[Record], turn: TurnBounds) -> dict[str, int]:
    baseline = {key: 0 for key in TOKEN_KEYS}
    end_total: dict[str, int] | None = None
    for record in records:
        total = token_total(record)
        if total is None:
            continue
        if record.index < turn.start_index:
            baseline = total
        elif turn.start_index <= record.index <= turn.end_index:
            end_total = total
    if end_total is None:
        raise ParseError("Selected turn has no token_count event.")
    delta = {key: end_total[key] - baseline[key] for key in TOKEN_KEYS}
    if any(value < 0 for value in delta.values()):
        raise ParseError("Token counters moved backwards within the selected turn.")
    if delta["cached_input_tokens"] > delta["input_tokens"]:
        raise ParseError("Cached input exceeds Codex input tokens; cannot derive fresh input safely.")
    return delta


def parse_iso(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParseError(f"Invalid {label} timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def canonical_utc(value: str, label: str) -> str:
    return parse_iso(value, label).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def task_times(turn: TurnBounds) -> tuple[str, str, float]:
    if not turn.start_record.timestamp or not turn.end_record.timestamp:
        raise ParseError("Selected turn is missing outer timestamps.")
    start = canonical_utc(turn.start_record.timestamp, "task start")
    end = canonical_utc(turn.end_record.timestamp, "task end")
    duration_ms = turn.end_record.payload.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        wall = float(duration_ms) / 60000.0
    else:
        wall = (parse_iso(end, "task end") - parse_iso(start, "task start")).total_seconds() / 60.0
    return start, end, wall


def normalize_model(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    key = raw.strip().lower()
    if key in MODEL_MAP:
        return MODEL_MAP[key]
    return raw, f"openai:{key}" if key.startswith("gpt-") else None


def normalize_thinking(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return THINKING_MAP.get(raw.strip().lower())


def quota_note(records: list[Record], turn: TurnBounds) -> str | None:
    snapshots: list[dict[str, Any]] = []
    for record in records[turn.start_index : turn.end_index + 1]:
        if record.type != "event_msg" or record.payload.get("type") != "token_count":
            continue
        limits = record.payload.get("rate_limits")
        if not isinstance(limits, dict):
            continue
        primary = limits.get("primary")
        if not isinstance(primary, dict) or not isinstance(primary.get("used_percent"), (int, float)):
            continue
        snapshots.append({
            "used": float(primary["used_percent"]),
            "window": primary.get("window_minutes"),
            "reset": primary.get("resets_at"),
            "plan": limits.get("plan_type"),
        })
    if not snapshots:
        return None
    first, last = snapshots[0], snapshots[-1]
    details = [f"quota used {first['used']:g}%→{last['used']:g}%"]
    if first["window"] is not None:
        details.append(f"window {first['window']}m")
    if first["reset"] is not None:
        details.append(f"reset_at {first['reset']}")
    if first["plan"]:
        details.append(f"plan {first['plan']}")
    details.append("coarse global meter; usageBefore/usageAfter not auto-attributed")
    return "; ".join(details)


def build_notes(path: Path, session: dict[str, Any], turn: TurnBounds, records: list[Record], user_notes: str | None) -> str:
    parts: list[str] = []
    if user_notes:
        parts.append(user_notes.strip())
    source_parts = [f"Codex source {path.name}", f"turn {turn.turn_id}"]
    session_id = session.get("session_id") or session.get("id")
    if session_id:
        source_parts.append(f"session {session_id}")
    if session.get("cli_version"):
        source_parts.append(f"cli {session['cli_version']}")
    git = session.get("git")
    if isinstance(git, dict):
        if git.get("repository_url"):
            source_parts.append(f"repo {git['repository_url']}")
        if git.get("branch"):
            source_parts.append(f"branch {git['branch']}")
        if git.get("commit_hash"):
            source_parts.append(f"baseline {git['commit_hash']}")
    parts.append("; ".join(source_parts) + ".")
    quota = quota_note(records, turn)
    if quota:
        parts.append(quota + ".")
    return " ".join(part for part in parts if part)


def build_run(records: list[Record], path: Path, args: argparse.Namespace) -> dict[str, Any]:
    turns = find_turns(records)
    turn = choose_turn(turns, args.turn_id)
    session = first_payload(records, "session_meta")
    context = matching_turn_context(records, turn.turn_id)
    tokens = delta_tokens(records, turn)
    start_at, end_at, wall_minutes = task_times(turn)

    raw_model = context.get("model") if isinstance(context.get("model"), str) else None
    model, model_id = normalize_model(args.model or raw_model)
    collaboration = context.get("collaboration_mode")
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    raw_thinking = settings.get("reasoning_effort") if isinstance(settings, dict) else None
    thinking = args.thinking or normalize_thinking(raw_thinking)

    timezone_name = context.get("timezone") if isinstance(context.get("timezone"), str) else None
    start_dt = parse_iso(start_at, "task start")
    local_dt = start_dt
    if timezone_name:
        try:
            local_dt = start_dt.astimezone(ZoneInfo(timezone_name))
        except Exception:
            pass

    run: dict[str, Any] = {
        "id": args.run_id,
        "sliceId": args.slice_id,
        "runType": args.run_type,
        "role": args.role,
        "startAt": start_at,
        "endAt": end_at,
        "localHour": local_dt.hour,
        "dayOfWeek": local_dt.strftime("%A"),
        "wallMinutes": wall_minutes,
        # Codex total input includes cached tokens. PennyTel stores fresh and cached separately.
        "inputTokens": tokens["input_tokens"] - tokens["cached_input_tokens"],
        "cachedInputTokens": tokens["cached_input_tokens"],
        # Codex output includes reasoning; reasoningTokens is a subset and is not double billed.
        "outputTokens": tokens["output_tokens"],
        "reasoningTokens": tokens["reasoning_output_tokens"],
        "notes": build_notes(path, session, turn, records, args.notes),
    }
    if args.candidate:
        run["candidate"] = args.candidate
    if model:
        run["model"] = model
    if model_id:
        run["modelId"] = model_id
    provider = session.get("model_provider")
    if isinstance(provider, str) and provider:
        run["provider"] = "OpenAI" if provider.lower() == "openai" else provider
        run["providerId"] = provider.lower()
    if thinking:
        run["thinking"] = thinking
    if args.session_mode:
        run["sessionMode"] = args.session_mode
    if args.context_mode:
        run["contextMode"] = args.context_mode
    if args.result:
        run["result"] = args.result
    return run


def dataset_for_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "revision": 0,
        "slices": [],
        "runs": [run],
        "findings": [],
        "discoveries": [],
        "pricing": [],
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert one completed Codex rollout turn into a PennyTel schema-v1 import JSON file."
    )
    p.add_argument("log", type=Path, help="Codex rollout .jsonl file")
    p.add_argument("--slice-id", required=True, help="Existing PennyTel slice ID")
    p.add_argument("--run-id", required=True, help="New unique PennyTel run ID")
    p.add_argument("--run-type", required=True, help="PennyTel runType value")
    p.add_argument("--role", required=True, choices=PENNYTEL_ROLES)
    p.add_argument("--turn-id", help="Required when a rollout contains more than one completed task")
    p.add_argument("--candidate")
    p.add_argument("--model", help="Override model parsed from Codex turn_context")
    p.add_argument("--thinking", choices=PENNYTEL_THINKING, help="Override thinking effort")
    p.add_argument("--session-mode", choices=("Fresh", "Resumed"))
    p.add_argument(
        "--context-mode",
        choices=("Full Repo", "Compact Packet", "Resumed Context", "Orchestrated Packet", "Other"),
    )
    p.add_argument(
        "--result",
        choices=("Completed", "Accepted", "Needs repair", "Rejected", "Blocked", "Aborted"),
    )
    p.add_argument("--notes", help="Optional operator notes prepended to parser provenance")
    p.add_argument("-o", "--output", type=Path, help="Output JSON path; stdout when omitted")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        records = load_records(args.log)
        run = build_run(records, args.log, args)
        dataset = dataset_for_run(run)
    except (OSError, ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(dataset, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
