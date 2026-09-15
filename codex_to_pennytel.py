#!/usr/bin/env python3
"""Privacy-reducing Codex rollout -> PennyTel schema-v1/schema-v2 adapter."""
from __future__ import annotations

import argparse, hashlib, json, math, os, re, sys, unicodedata
from dataclasses import dataclass, field
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

PENNYTEL_ROLES = ("Orchestrator", "Context Steward", "Implementer", "Critic", "Repair")
PENNYTEL_THINKING = ("Low", "Medium", "High", "ExtraHigh", "Max")
OUTPUT_TARGETS = ("v1", "v2")
QUOTA_ATTRIBUTIONS = ("Unknown", "Clean", "Contaminated")
MODEL_MAP = {"gpt-6-astra": ("GPT-6 Astra", "openai:gpt-6-astra"), "gpt-5.6-sol": ("GPT-5.6 Sol", "openai:gpt-5.6-sol"), "gpt-5.6-luna": ("GPT-5.6 Luna", "openai:gpt-5.6-luna")}
THINKING_MAP = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "ExtraHigh", "extra_high": "ExtraHigh", "extrahigh": "ExtraHigh", "extra-high": "ExtraHigh", "max": "Max"}
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
TOOL_ITEM_TYPES = {"CommandExecution", "FileChange", "McpToolCall", "CollabAgentToolCall", "ImageView", "Extension"}
SAFE_INTEGER = 9_007_199_254_740_991

class ParseError(ValueError): pass

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

@dataclass(frozen=True)
class SourceRecords(Sequence):
    """Immutable byte authority with a disposable, caller-accessible parsed view.

    Nested dictionaries in the view may be edited by UI/callers. All emission
    boundaries reparse _raw; neither those edits nor replacement views are trusted.
    """
    _raw: bytes = field(repr=False)
    path: Path
    root: Path
    _records: list[Record] = field(init=False, repr=False, compare=False)
    digest: str = field(init=False)
    stale: bool = field(default=False, init=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_raw", bytes(self._raw))
        object.__setattr__(self, "digest", hashlib.sha256(self._raw).hexdigest())
        object.__setattr__(self, "_records", _parse_records(self._raw))

    def __len__(self): return len(self._records)
    def __getitem__(self, key): return self._records[key]

    def snapshot(self) -> SourceRecords:
        return SourceRecords(self._raw, self.path, self.root)


def read_source(path: Path, root: Path) -> bytes:
    root = root.absolute()
    if root.resolve(strict=True) != root: raise ParseError("Selected root changed; refresh/reparse required.")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ParseError("Source escapes selected root.")
    # Open each resolved path component relative to a pinned directory descriptor.
    # O_NOFOLLOW closes symlink replacement races after resolve/containment checks.
    relative = resolved.relative_to(root)
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        source_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(source_fd, "rb") as handle:
            return handle.read()
    finally:
        os.close(fd)


def load_records(path: Path, root: Path | None = None) -> SourceRecords:
    path = path.absolute(); root = (root or path.parent).resolve(strict=True)
    return SourceRecords(read_source(path, root), path, root)


def _parse_records(raw_bytes: bytes) -> list[Record]:
    records = []
    try: lines = raw_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc: raise ParseError("Source is not UTF-8.") from exc
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip(): continue
        try: value = json.loads(raw)
        except json.JSONDecodeError as exc: raise ParseError(f"Invalid JSON on line {line_no}: {exc.msg}") from exc
        if not isinstance(value, dict): raise ParseError(f"Line {line_no} is not a JSON object.")
        payload = value.get("payload")
        records.append(Record(len(records), value.get("timestamp") if isinstance(value.get("timestamp"), str) else None, value.get("type") if isinstance(value.get("type"), str) else None, payload if isinstance(payload, dict) else {}))
    if not records: raise ParseError("Codex rollout is empty.")
    return records


def verified_source(records: SourceRecords, path: Path) -> bytes:
    if not isinstance(records, SourceRecords) or path.absolute() != records.path:
        raise ParseError("Source snapshot unavailable; refresh/reparse required.")
    try:
        if records.stale: raise ParseError("Source is stale; refresh/reparse required.")
        raw = read_source(path, records.root)
        if raw != records._raw:
            raise ParseError("Source is stale; refresh/reparse required.")
        return records._raw
    except (OSError, ParseError):
        object.__setattr__(records, "stale", True)
        raise ParseError("Source is stale or unavailable; refresh/reparse required.") from None


def verified_snapshot(records: SourceRecords, path: Path) -> SourceRecords:
    verified_source(records, path)
    return records.snapshot()


def find_turns(records: Iterable[Record]) -> list[TurnBounds]:
    starts, turns = {}, []
    for record in records:
        if record.type != "event_msg": continue
        event_type, turn_id = record.payload.get("type"), record.payload.get("turn_id")
        if not isinstance(turn_id, str): continue
        if event_type == "task_started": starts[turn_id] = record
        elif event_type == "task_complete" and turn_id in starts:
            start = starts.pop(turn_id); turns.append(TurnBounds(turn_id, start.index, record.index, start, record))
    return turns

def choose_turn(turns: list[TurnBounds], turn_id: str | None) -> TurnBounds:
    if turn_id:
        for turn in turns:
            if turn.turn_id == turn_id: return turn
        raise ParseError("No completed Codex task found for selected turn.")
    if not turns: raise ParseError("No completed Codex task_started/task_complete pair found.")
    if len(turns) > 1: raise ParseError("Rollout contains multiple completed turns. Pass --turn-id explicitly.")
    return turns[0]

def first_payload(records: Iterable[Record], record_type: str) -> dict[str, Any]:
    return next((r.payload for r in records if r.type == record_type), {})

def matching_turn_context(records: Iterable[Record], turn_id: str) -> dict[str, Any]:
    values = [r.payload for r in records if r.type == "turn_context" and r.payload.get("turn_id") == turn_id]
    return values[-1] if values else {}

def delta_tokens(records: list[Record], turn: TurnBounds) -> dict[str, int]:
    """Exact turn snapshots only: thread counters do not prove lifetime continuity."""
    snapshots = [r.payload["turn_token_usage"] for r in records[turn.start_index:turn.end_index + 1]
                 if r.type == "token_usage_record" and r.payload.get("turn_id") == turn.turn_id
                 and isinstance(r.payload.get("turn_token_usage"), dict)]
    # A later partial snapshot must not acquire stale fields from earlier snapshots.
    tokens = _numeric_usage(snapshots[-1]) if snapshots else {}
    tokens = tokens or {}
    if "input_tokens" in tokens and tokens.get("cached_input_tokens", 0) > tokens["input_tokens"]:
        raise ParseError("Cached input exceeds Codex input tokens.")
    if "output_tokens" in tokens and tokens.get("reasoning_output_tokens", 0) > tokens["output_tokens"]:
        raise ParseError("Reasoning tokens exceed output tokens.")
    return tokens


def parse_iso(value: str, label: str) -> datetime:
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ParseError(f"Invalid {label} timestamp.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

def canonical_utc(value: str, label: str) -> str:
    return parse_iso(value, label).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def task_times(turn: TurnBounds) -> tuple[str, str, float]:
    start_raw = next((value for value in (turn.start_record.payload.get("started_at"), turn.end_record.payload.get("started_at"), turn.start_record.timestamp) if value is not None), None)
    end_raw = turn.end_record.payload.get("completed_at")
    if end_raw is None: end_raw = turn.end_record.timestamp
    start, end = _reset_timestamp(start_raw), _reset_timestamp(end_raw)
    if start is None or end is None: raise ParseError("Selected turn is missing valid lifecycle timestamps.")
    if parse_iso(end, "task end") < parse_iso(start, "task start"): raise ParseError("Task end precedes start.")
    duration = turn.end_record.payload.get("duration_ms")
    wall = float(duration) / 60000 if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and 0 <= duration <= SAFE_INTEGER else (parse_iso(end, "task end") - parse_iso(start, "task start")).total_seconds() / 60
    return start, end, wall

def normalize_model(raw: str | None) -> tuple[str | None, str | None]:
    if not raw: return None, None
    key = raw.strip().lower(); return MODEL_MAP.get(key, (raw, f"openai:{key}" if key.startswith("gpt-") else None))

def normalize_thinking(raw: Any) -> str | None:
    return THINKING_MAP.get(raw.strip().lower()) if isinstance(raw, str) else None

def is_auto_review(session: dict[str, Any], context: dict[str, Any]) -> bool:
    return context.get("model") == "codex-auto-review" or session.get("thread_source") == "guardian_review"

def _metadata(value: Any, maximum: int, no_space: bool = False) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum: return None
    if any(unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in value) or (no_space and any(c.isspace() for c in value)): return None
    return value

def _safe_repository_url(value: Any) -> str | None:
    value = _metadata(value, 2048)
    if not value: return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http", "ssh", "git"} or not parts.hostname: return None
        # Reject userinfo entirely; strip query/fragment without ever copying them.
        if parts.username is not None or parts.password is not None: return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return None


def _numeric_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict): return None
    result = {}
    for key in TOKEN_KEYS:
        if key not in value: continue
        number = value[key]
        if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= SAFE_INTEGER: return None
        result[key] = number
    return result

def _invocation_evidence(records: list[Record], turn: TurnBounds) -> tuple[int | None, dict[str, int] | None]:
    observations, identified, conflict = [], {}, False
    scoped = records[turn.start_index:turn.end_index + 1]
    for record in scoped:
        if record.type != "token_usage_record" or record.payload.get("turn_id") != turn.turn_id: continue
        p = record.payload
        usage = _numeric_usage(p.get("usage"))
        response_id = p.get("response_id")
        window = p.get("model_context_window")
        observation = (usage, window)
        if isinstance(response_id, str) and response_id:
            if response_id in identified and identified[response_id] != observation: conflict = True
            identified[response_id] = observation
        else:
            conflict = True
        if usage: observations.append(observation)
    count = len(identified) if identified and not conflict else None
    if not observations:
        # Snapshot equality does not establish distinct invocations. Keep count Unknown.
        for record in scoped:
            if record.type != "event_msg" or record.payload.get("type") != "token_count": continue
            info = record.payload.get("info")
            if not isinstance(info, dict): continue
            usage = _numeric_usage(info.get("last_token_usage"))
            if usage: observations.append((usage, info.get("model_context_window")))
    peaks = [entry for entry in observations if entry[0] and "input_tokens" in entry[0]]
    if not peaks or conflict: return count, None
    usage, window = max(peaks, key=lambda entry: entry[0]["input_tokens"])
    value = {"inputTokens": usage["input_tokens"]}
    if "cached_input_tokens" in usage:
        if usage["cached_input_tokens"] > usage["input_tokens"]: return count, None
        value["cachedInputTokens"] = usage["cached_input_tokens"]
    if isinstance(window, int) and not isinstance(window, bool) and 0 < window <= SAFE_INTEGER:
        if usage["input_tokens"] > window: return count, None
        value["contextWindowTokens"] = window
    return count, value


def _tool_call_count(records: list[Record], turn: TurnBounds) -> int | None:
    identities, observed = set(), False
    for record in records[turn.start_index:turn.end_index + 1]:
        if record.type != "event_msg" or record.payload.get("type") != "item_completed": continue
        if record.payload.get("turn_id", turn.turn_id) != turn.turn_id: continue
        item = record.payload.get("item")
        if not isinstance(item, dict): continue
        observed = True
        if item.get("type") in TOOL_ITEM_TYPES:
            identity = item.get("id") or item.get("call_id") or record.payload.get("item_id")
            if not isinstance(identity, str) or not identity: return None
            identities.add(identity)
    return len(identities) if observed else None


def _reset_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try: return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError): return None
    if isinstance(value, str):
        try: return canonical_utc(value, "quota reset")
        except ParseError: return None
    return None

def _quota_windows(records: list[Record], turn: TurnBounds, attribution: str) -> list[dict[str, Any]]:
    by_name: dict[str, list[tuple[Record, dict[str, Any], dict[str, Any]]]] = {"primary": [], "secondary": []}
    for record in records[turn.start_index:turn.end_index + 1]:
        if record.type != "event_msg" or record.payload.get("type") != "token_count": continue
        limits = record.payload.get("rate_limits")
        if not isinstance(limits, dict): continue
        for name in by_name:
            reading = limits.get(name); used = reading.get("used_percent") if isinstance(reading, dict) else None
            if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100: by_name[name].append((record, reading, limits))
    result = []
    for name, entries in by_name.items():
        if not entries: continue
        # Reset boundaries belong to endpoints, not the identity of the meter.
        identities = {json.dumps([e[2].get("limit_id"), e[2].get("limit_name"), e[2].get("meter_id"), e[2].get("plan_type"), e[2].get("window_id"), e[1].get("window_minutes"), e[1].get("limit_id"), e[1].get("window_id"), e[1].get("meter_id")], sort_keys=True) for e in entries}
        if len(identities) != 1: continue
        first, last = entries[0], entries[-1]; window: dict[str, Any] = {"windowName": name, "attribution": attribution}
        minutes = first[1].get("window_minutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool) and 0 < minutes <= SAFE_INTEGER: window["windowMinutes"] = minutes
        plan = _metadata(first[2].get("plan_type"), 100)
        if plan: window["planType"] = plan
        for key, entry in (("first", first), ("last", last)):
            reading = {"usedPercent": entry[1]["used_percent"]}
            if entry[0].timestamp:
                try: reading["recordedAt"] = canonical_utc(entry[0].timestamp, "quota reading")
                except ParseError: pass
            reset = _reset_timestamp(entry[1].get("resets_at"))
            if reset: reading["resetsAt"] = reset
            window[key] = reading
        result.append(window)
    return result

def _environment(context: dict[str, Any]) -> dict[str, str]:
    result = {}; sandbox = context.get("sandbox_policy"); sandbox_type = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    if sandbox_type in {"read-only", "workspace-write", "danger-full-access", "external-sandbox", "unknown"}: result["sandboxMode"] = sandbox_type
    if context.get("approval_policy") in {"untrusted", "on-failure", "on-request", "never", "unknown"}: result["approvalPolicy"] = context["approval_policy"]
    if context.get("approvals_reviewer") in {"user", "auto_review", "unknown"}: result["approvalReviewer"] = context["approvals_reviewer"]
    permission = context.get("permission_profile"); network = permission.get("network") if isinstance(permission, dict) else None
    if network in {"enabled", "restricted", "disabled", "unknown"}: result["networkAccess"] = network
    elif isinstance(sandbox, dict) and isinstance(sandbox.get("network_access"), bool): result["networkAccess"] = "enabled" if sandbox["network_access"] else "disabled"
    return result

def build_execution_evidence(records: list[Record], path: Path, turn: TurnBounds, session: dict[str, Any], context: dict[str, Any], attribution: str = "Unknown") -> dict[str, Any]:
    records = verified_snapshot(records, path)
    turn = snapshot_turn(records, turn)
    return _execution_evidence(records, path, turn, first_payload(records, "session_meta"), matching_turn_context(records, turn.turn_id), attribution)


def snapshot_turn(records: SourceRecords, turn: TurnBounds) -> TurnBounds:
    # Compare immutable selectors, never caller-owned lifecycle payloads.
    for original in find_turns(records):
        if (original.turn_id, original.start_index, original.end_index) == (turn.turn_id, turn.start_index, turn.end_index):
            return original
    raise ParseError("Turn does not belong to source snapshot; refresh/reparse required.")


def _execution_evidence(records, path, turn, session, context, attribution="Unknown"):
    if attribution not in QUOTA_ATTRIBUTIONS: raise ParseError(f"Invalid quota attribution: {attribution}")
    file_name = _metadata(path.name, 255)
    if not file_name or file_name in {".", ".."} or "/" in file_name or "\\" in file_name: raise ParseError("Source filename cannot be represented by PennyTel v2.")
    evidence: dict[str, Any] = {"kind": "codex-rollout", "formatVersion": 1, "sourceLog": {"fileName": file_name, "contentHash": {"algorithm": "sha256", "value": records.digest}}}
    candidates = ((session.get("session_id") or session.get("id"), "sessionId", 200, True), (turn.turn_id, "turnId", 200, True), (session.get("cli_version"), "runtimeVersion", 100, False), (session.get("originator"), "originator", 200, False), (session.get("cwd") or context.get("cwd"), "workingDirectory", 4096, False))
    for raw, target, maximum, no_space in candidates:
        value = _metadata(raw, maximum, no_space)
        if value: evidence[target] = value
    git = session.get("git")
    if isinstance(git, dict):
        repository = {}; url, branch, sha = _safe_repository_url(git.get("repository_url")), _metadata(git.get("branch"), 255), _metadata(git.get("commit_hash"), 64)
        if url: repository["url"] = url
        if branch: repository["branch"] = branch
        if sha and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", sha): repository["baselineCommitSha"] = sha
        if repository: evidence["repository"] = repository
    ttft = turn.end_record.payload.get("time_to_first_token_ms")
    if isinstance(ttft, (int, float)) and not isinstance(ttft, bool) and math.isfinite(ttft) and 0 <= ttft <= SAFE_INTEGER: evidence["timeToFirstTokenMs"] = ttft
    count, peak = _invocation_evidence(records, turn)
    if count is not None: evidence["modelInvocationCount"] = count
    if peak is not None: evidence["peakInvocation"] = peak
    tool_count = _tool_call_count(records, turn)
    if tool_count is not None: evidence["toolCallCount"] = tool_count
    window = turn.start_record.payload.get("model_context_window")
    if isinstance(window, int) and not isinstance(window, bool) and 0 < window <= SAFE_INTEGER: evidence["modelContextWindowTokens"] = window
    quota = _quota_windows(records, turn, attribution)
    if quota: evidence["quotaWindows"] = quota
    environment = _environment(context)
    if environment: evidence["environment"] = environment
    return evidence

def build_notes(path: Path, session: dict[str, Any], turn: TurnBounds, records: list[Record], user_notes: str | None, target: str) -> str:
    parts = [user_notes.strip()] if user_notes and user_notes.strip() else []
    # Structured evidence is the only source identifier channel. Rejected metadata
    # must never leak into the less restrictive notes field (including v1).
    parts.append("Imported from Codex rollout telemetry.")
    if target == "v1":
        windows = _quota_windows(records, turn, "Unknown")
        if windows:
            window = windows[0]; first, last = window["first"]["usedPercent"], window["last"]["usedPercent"]
            parts.append(f"quota used {first:g}%→{last:g}%; coarse global meter; usageBefore/usageAfter not auto-attributed.")
    return " ".join(parts)

def build_run(records: list[Record], path: Path, args: argparse.Namespace) -> dict[str, Any]:
    return _build_run(verified_snapshot(records, path), path, args)


def _build_run(records: SourceRecords, path: Path, args: argparse.Namespace, turn: TurnBounds | None = None) -> dict[str, Any]:
    turn = turn or choose_turn(find_turns(records), getattr(args, "turn_id", None)); session, context = first_payload(records, "session_meta"), matching_turn_context(records, turn.turn_id)
    if is_auto_review(session, context): raise ParseError("codex-auto-review/control sessions are not emittable.")
    tokens = delta_tokens(records, turn); start_at, end_at, wall = task_times(turn)
    model, model_id = normalize_model(getattr(args, "model", None) or (context.get("model") if isinstance(context.get("model"), str) else None))
    collaboration = context.get("collaboration_mode"); settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    thinking = getattr(args, "thinking", None) or normalize_thinking(settings.get("reasoning_effort") if isinstance(settings, dict) else context.get("effort"))
    local = parse_iso(start_at, "task start"); tz = context.get("timezone")
    if isinstance(tz, str):
        try: local = local.astimezone(ZoneInfo(tz))
        except Exception: pass
    target = getattr(args, "target", "v1")
    run: dict[str, Any] = {"id": args.run_id, "sliceId": args.slice_id, "runType": args.run_type, "role": args.role, "startAt": start_at, "endAt": end_at, "localHour": local.hour, "dayOfWeek": local.strftime("%A"), "wallMinutes": wall, "notes": build_notes(path, session, turn, records, getattr(args, "notes", None), target)}
    for key, value in (("candidate", getattr(args, "candidate", None)), ("model", model), ("modelId", model_id), ("thinking", thinking), ("sessionMode", getattr(args, "session_mode", None)), ("contextMode", getattr(args, "context_mode", None)), ("result", getattr(args, "result", None))):
        if value: run[key] = value
    if "input_tokens" in tokens and "cached_input_tokens" in tokens: run["inputTokens"] = tokens["input_tokens"] - tokens["cached_input_tokens"]
    for source, dest in (("cached_input_tokens", "cachedInputTokens"), ("output_tokens", "outputTokens"), ("reasoning_output_tokens", "reasoningTokens")):
        if source in tokens: run[dest] = tokens[source]
    provider = session.get("model_provider")
    if isinstance(provider, str) and provider.lower() in {"openai", "openai-api"}:
        run["provider"], run["providerId"] = "OpenAI", "openai-api"
    if target == "v2": run["executionEvidence"] = _execution_evidence(records, path, turn, session, context, getattr(args, "quota_attribution", "Unknown"))
    return run

def dataset_for_run(run: dict[str, Any], target: str = "v1") -> dict[str, Any]:
    if target not in OUTPUT_TARGETS: raise ParseError(f"Unknown output target: {target}")
    run = dict(run)
    if target == "v1": run.pop("executionEvidence", None)
    return {"schemaVersion": 1 if target == "v1" else 2, "revision": 0, "slices": [], "runs": [run], "findings": [], "discoveries": [], "pricing": []}

def discover_logs(path: Path) -> list[Path]:
    if path.is_file(): return [path] if path.suffix == ".jsonl" else []
    return sorted(p for p in path.rglob("rollout-*.jsonl") if p.resolve().is_relative_to(path.resolve())) if path.is_dir() else []

def output_filename(path: Path, turn_id: str | None = None) -> str:
    key = json.dumps([str(path.absolute()), turn_id], ensure_ascii=True)
    return "codex-" + hashlib.sha256(key.encode()).hexdigest() + ".pennytel.json"


def write_output(path: Path, rendered: str) -> None:
    # Exclusive creation also protects against filename collisions and symlinks.
    with path.open("x", encoding="utf-8") as handle: handle.write(rendered)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Convert completed Codex rollout turns into PennyTel import JSON.")
    p.add_argument("log", type=Path, help="Rollout JSONL file or recursively searched folder"); p.add_argument("--target", required=True, choices=OUTPUT_TARGETS)
    p.add_argument("--slice-id", required=True); p.add_argument("--run-id", help="Explicit single-file run ID override; otherwise persist a UUID"); p.add_argument("--run-type", required=True); p.add_argument("--role", required=True, choices=PENNYTEL_ROLES)
    p.add_argument("--state-file", type=Path, help="Persistent harvest/run-ID state location"); p.add_argument("--turn-id"); p.add_argument("--candidate"); p.add_argument("--model"); p.add_argument("--thinking", choices=PENNYTEL_THINKING); p.add_argument("--session-mode", choices=("Fresh", "Resumed")); p.add_argument("--context-mode", choices=("Full Repo", "Compact Packet", "Resumed Context", "Orchestrated Packet", "Other")); p.add_argument("--result", choices=("Completed", "Accepted", "Needs repair", "Rejected", "Blocked", "Aborted")); p.add_argument("--notes"); p.add_argument("--quota-attribution", choices=QUOTA_ATTRIBUTIONS, default="Unknown"); p.add_argument("-o", "--output", type=Path)
    return p

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv); paths = discover_logs(args.log)
    if not paths: print("error: no rollout JSONL files found", file=sys.stderr); return 2
    if args.log.is_dir() and (args.turn_id or not args.output): print("error: folder input requires --output directory and does not accept --turn-id", file=sys.stderr); return 2
    if args.log.is_dir() and args.run_id:
        print("error: --run-id overrides one file only; folder runs use persistent UUIDs", file=sys.stderr); return 2
    failures = []
    for path in paths:
        try:
            records = load_records(path, args.log if args.log.is_dir() else args.log.parent); run_args = argparse.Namespace(**vars(args))
            if not args.run_id:
                turn = choose_turn(find_turns(records), args.turn_id); run_args.turn_id = turn.turn_id
                import harvest
                run_args.run_id = harvest.ensure_run_id(records, turn, args.state_file)
            run = build_run(records, path, run_args); rendered = json.dumps(dataset_for_run(run, args.target), indent=2, ensure_ascii=False) + "\n"
            if args.output:
                destination = args.output if args.log.is_file() else args.output / path.parent.relative_to(args.log) / output_filename(path)
                destination.parent.mkdir(parents=True, exist_ok=True); write_output(destination, rendered); print(destination)
            else: sys.stdout.write(rendered)
        except (OSError, ParseError) as exc: failures.append(f"{path}: {exc}")
    for failure in failures: print(f"error: {failure}", file=sys.stderr)
    return 2 if failures else 0

if __name__ == "__main__": raise SystemExit(main())
