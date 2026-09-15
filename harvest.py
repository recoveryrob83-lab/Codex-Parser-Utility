"""Tuple provenance, persistent opaque run IDs, and verified copy-only curation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import codex_to_pennytel as core

STATE_VERSION = 3
FINGERPRINT_VERSION = 1
# PennyTel uses JavaScript String.trim(), whose whitespace differs from Python's.
PENNYTEL_TRIM_CHARS = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def identity_key(session_id: str, turn_id: str) -> str:
    return json.dumps([session_id, turn_id], ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class Candidate:
    path: Path
    records: core.SourceRecords
    turn: core.TurnBounds
    session: dict[str, Any]
    context: dict[str, Any]
    source_hash: str
    _session_id: str | None = field(init=False, repr=False)

    def __post_init__(self):
        if self.path.absolute() != self.records.path or self.source_hash != self.records.digest:
            raise core.ParseError("Candidate differs from source snapshot.")
        snapshot = self.records.snapshot()
        core.snapshot_turn(snapshot, self.turn)
        session = core.first_payload(snapshot, "session_meta")
        object.__setattr__(self, "_session_id", core._metadata(session.get("session_id") or session.get("id"), 200, True))

    @property
    def stale(self) -> bool:
        return self.records.stale

    def snapshot(self, verify: bool = True):
        records = core.verified_snapshot(self.records, self.path) if verify else self.records.snapshot()
        turn = core.snapshot_turn(records, self.turn)
        return records, turn, core.first_payload(records, "session_meta"), core.matching_turn_context(records, turn.turn_id)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def identity(self) -> str | None:
        turn_id = core._metadata(self.turn.turn_id, 200, True)
        return identity_key(self.session_id, turn_id) if self.session_id and turn_id else None

    def verified_bytes(self) -> bytes:
        try:
            if self.stale or self.source_hash != self.records.digest:
                raise core.ParseError("Source is stale; refresh/reparse required.")
            return core.verified_source(self.records, self.path)
        except (OSError, core.ParseError):
            object.__setattr__(self.records, "stale", True)
            raise core.ParseError("Source is stale or outside selected root; refresh/reparse required.") from None


def config_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "penny-codex-parser"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "penny-codex-parser"


def state_file() -> Path:
    return config_dir() / "harvest-state.json"


def _clean_state(value: Any) -> dict[str, Any]:
    def invalid():
        raise core.ParseError("Harvest state is corrupt or unsupported; explicitly restore or recreate it before scanning.")
    if not isinstance(value, dict) or value.get("version", 1) not in (1, 2, 3): invalid()
    if not isinstance(value.get("harvested"), dict): invalid()
    cleaned = {"version": STATE_VERSION, "harvested": {}, "runIds": {}}
    for old_key, entry in value["harvested"].items():
        if not isinstance(entry, dict): invalid()
        session, turn = entry.get("sessionId"), entry.get("turnId")
        if not core._metadata(session, 200, True) or not core._metadata(turn, 200, True): invalid()
        key = identity_key(session, turn)
        if old_key != (f"{session}:{turn}" if value.get("version", 1) == 1 else key): invalid()
        if key in cleaned["harvested"]: invalid()
        if not isinstance(entry.get("sourceHash"), str) or not re.fullmatch("[a-f0-9]{64}", entry["sourceHash"]): invalid()
        name = core._metadata(entry.get("sourceFile"), 255)
        if not name or "/" in name or "\\" in name: invalid()
        cleaned["harvested"][key] = {k: entry[k] for k in ("sessionId", "turnId", "sourceFile", "sourceHash")}
        if "evidenceHash" in entry or "fingerprintVersion" in entry:
            if entry.get("fingerprintVersion") != FINGERPRINT_VERSION or not isinstance(entry.get("evidenceHash"), str) or not re.fullmatch("[a-f0-9]{64}", entry["evidenceHash"]): invalid()
            cleaned["harvested"][key].update({k: entry[k] for k in ("evidenceHash", "fingerprintVersion")})
    mappings = value.get("runIds", {})
    if not isinstance(mappings, dict): invalid()
    for key, run_id in mappings.items():
        try: pair = json.loads(key)
        except (TypeError, ValueError): invalid()
        if not isinstance(pair, list) or len(pair) != 2 or not all(core._metadata(v, 200, True) for v in pair): invalid()
        if identity_key(*pair) != key: invalid()
        try:
            parsed = uuid.UUID(run_id)
            if str(parsed) != run_id or parsed.version != 4: invalid()
        except (ValueError, TypeError, AttributeError): invalid()
        if run_id in cleaned["runIds"].values(): invalid()
        cleaned["runIds"][key] = run_id
    return cleaned


def _read_state(path: Path | None = None) -> dict[str, Any]:
    path = path or state_file()
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate state key")
            result[key] = value
        return result
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=unique_pairs)
    except FileNotFoundError:
        return {"version": STATE_VERSION, "harvested": {}, "runIds": {}}
    except (OSError, ValueError) as exc:
        raise core.ParseError("Harvest state unreadable/corrupt; explicitly restore or recreate it before scanning.") from exc
    if not isinstance(value, dict) or "version" not in value:
        raise core.ParseError("Harvest state missing version; explicitly restore or recreate it.")
    return _clean_state(value)


def load_state(path: Path | None = None) -> dict[str, Any]:
    # Readers participate in recovery so scans cannot silently skip a half commit.
    with state_transaction(path) as state:
        return state


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _ensure_directory(path: Path) -> None:
    """Persist new directory links as well as the files later placed inside them."""
    missing, current = [], path.absolute()
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        fsync_directory(directory.parent)
    # Also covers a retry after mkdir succeeded but its parent fsync failed.
    if not missing: fsync_directory(path.absolute().parent)


def _checkpoint(name: str) -> None:
    """Fault-injection seam; production commit logic does not depend on it."""


def _atomic_json(value: dict, path: Path) -> None:
    _ensure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".harvest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        _checkpoint("atomic_file_synced")
        os.replace(temporary, path)
        _checkpoint("atomic_replaced")
        fsync_directory(path.parent)
        _checkpoint("atomic_directory_synced")
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    _atomic_json(_clean_state(state), path or state_file())


@contextmanager
def state_transaction(path: Path | None = None):
    path = path or state_file(); _ensure_directory(path.parent)
    fd = os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_inbox(path)
        yield _read_state(path)


def ensure_run_id(records: core.SourceRecords, turn: core.TurnBounds, state_path: Path | None = None) -> str:
    records = core.verified_snapshot(records, records.path)
    turn = core.snapshot_turn(records, turn)
    session = core.first_payload(records, "session_meta")
    candidate = Candidate(records.path, records, turn, session, {}, records.digest)
    candidate.verified_bytes()
    if not candidate.identity: raise core.ParseError("Valid session/turn provenance is required for persistent run identity.")
    with state_transaction(state_path) as state:
        key = candidate.identity
        if key not in state["runIds"]:
            state["runIds"][key] = str(uuid.uuid4())
            save_state(state, state_path)
        return state["runIds"][key]


def mark_harvested(candidate: Candidate, state: dict[str, Any]) -> None:
    if not candidate.identity: raise core.ParseError("Invalid harvest identity.")
    state.setdefault("harvested", {})[candidate.identity] = {"sessionId": candidate.session_id, "turnId": candidate.turn.turn_id, "sourceFile": candidate.path.name, "sourceHash": candidate.source_hash, "fingerprintVersion": FINGERPRINT_VERSION, "evidenceHash": evidence_fingerprint(candidate)}
    state["version"] = STATE_VERSION


def evidence_fingerprint(candidate: Candidate) -> str:
    """Hash only normalized turn metrics/provenance, never messages/tool output.

    Exclude the whole-file digest/name so later unrelated appends/copies are safe.
    Include intermediate metric observations so changed non-peak readings conflict.
    The normalized material is ephemeral; state stores only its version and digest.
    """
    records, turn, session, context = candidate.snapshot()
    args = SimpleNamespace(turn_id=turn.turn_id, run_id="fingerprint", slice_id="fingerprint", run_type="Repair", role="Repair", target="v2")
    run = core._build_run(records, candidate.path, args, turn)
    run["executionEvidence"].pop("sourceLog", None)
    observations = []
    for r in records[turn.start_index:turn.end_index + 1]:
        p = r.payload
        if p.get("turn_id", turn.turn_id) != turn.turn_id: continue
        item = None
        if r.type == "token_usage_record":
            item = {"responseId": core._metadata(p.get("response_id"), 200, True),
                    "usage": core._numeric_usage(p.get("usage")), "turnUsage": core._numeric_usage(p.get("turn_token_usage")),
                    "window": core._numeric_usage({"input_tokens": p.get("model_context_window")})}
        elif r.type == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            item = {"usage": core._numeric_usage(info.get("last_token_usage")),
                    "window": core._numeric_usage({"input_tokens": info.get("model_context_window")}),
                    "quota": core._quota_windows(records, core.TurnBounds(turn.turn_id, r.index, r.index, r, r), "Unknown")}
            limits = p.get("rate_limits")
            if isinstance(limits, dict):
                item["quotaIdentity"] = {k: core._metadata(limits.get(k), 100) for k in ("limit_id", "limit_name", "meter_id", "plan_type", "window_id")}
                for name in ("primary", "secondary"):
                    reading = limits.get(name)
                    if isinstance(reading, dict): item[name] = {k: core._metadata(reading.get(k), 100) for k in ("limit_id", "meter_id", "window_id")}
        elif r.type == "event_msg" and p.get("type") == "item_completed":
            tool = p.get("item")
            if isinstance(tool, dict):
                item = {"tool": tool.get("type") if tool.get("type") in core.TOOL_ITEM_TYPES else "non-tool",
                        "id": core._metadata(tool.get("id") or tool.get("call_id") or p.get("item_id"), 200, True)}
        if item is not None:
            observations.append([r.type, core._reset_timestamp(r.timestamp), item])
    material = json.dumps([FINGERPRINT_VERSION, run, observations], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(material.encode()).hexdigest()


def _check_harvested(candidate: Candidate, entry: dict) -> None:
    if entry.get("evidenceHash"):
        matches = entry["evidenceHash"] == evidence_fingerprint(candidate)
    else:
        # Legacy entries cannot prove append-only growth. Never silently rebaseline.
        matches = entry["sourceHash"] == candidate.source_hash
    if not matches:
        raise core.ParseError("Harvested evidence conflict; scan refused (legacy state may require explicit rebaseline).")


def _turn_signature(candidate: Candidate) -> list:
    return [(r.timestamp, r.type, r.payload) for r in candidate.records[candidate.turn.start_index:candidate.turn.end_index + 1]]


def find_new_runs(roots: Iterable[Path] | None = None, state: dict[str, Any] | None = None) -> list[Candidate]:
    roots = list(roots if roots is not None else (Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions"))
    state = load_state() if state is None else _clean_state(state)
    candidates = {}
    for root in roots:
        if not root.exists(): continue
        for path in core.discover_logs(root):
            persisted = [entry for entry in state["harvested"].values() if entry["sourceFile"] == path.name]
            try: records = core.load_records(path, root)
            except (OSError, core.ParseError):
                if persisted: raise core.ParseError("Harvested evidence conflict: source is no longer readable/parseable.") from None
                continue
            session = core.first_payload(records, "session_meta")
            turns = core.find_turns(records)
            session_id = core._metadata(session.get("session_id") or session.get("id"), 200, True)
            for entry in persisted:
                if session_id != entry["sessionId"] or not any(t.turn_id == entry["turnId"] for t in turns):
                    raise core.ParseError("Harvested evidence conflict: completed tuple disappeared from source.")
            for turn in turns:
                context = core.matching_turn_context(records, turn.turn_id)
                candidate = Candidate(path, records, turn, session, context, records.digest)
                key = candidate.identity
                if not key: continue
                if key in state["harvested"]:
                    _check_harvested(candidate, state["harvested"][key])
                    continue
                if core.is_auto_review(session, context): continue
                if key in candidates:
                    previous = candidates[key]
                    if previous.source_hash != candidate.source_hash or _turn_signature(previous) != _turn_signature(candidate):
                        raise core.ParseError("Conflicting duplicate session/turn evidence; scan refused.")
                    continue
                candidates[key] = candidate
    return list(candidates.values())


def safe_component(value: str | None, fallback: str = "Unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", value or "").strip(" .-")
    return cleaned[:64] or fallback


def inbox_root(now: datetime | None = None) -> Path:
    return Path.home() / "PennyTel-Inbox" / (now or datetime.now()).date().isoformat()


def output_name(candidate: Candidate) -> str:
    if not candidate.identity: raise core.ParseError("Invalid harvest identity.")
    return "codex-" + hashlib.sha256(candidate.identity.encode()).hexdigest() + ".pennytel.json"


def save_to_inbox(candidate: Candidate, dataset: dict[str, Any], destination: Path | None = None, state_path: Path | None = None) -> Path:
    candidate.verified_bytes()
    # Detach before validation/serialization: caller mutations cannot race commit.
    try: dataset = json.loads(json.dumps(dataset, allow_nan=False))
    except (TypeError, ValueError) as exc: raise core.ParseError("Invalid inbox dataset.") from exc
    # Reject invalid slice IDs before even locking/recovering a state transaction.
    _validate_inbox_slice_id(dataset)
    state_path = state_path or state_file()
    with state_transaction(state_path) as state:
        if candidate.identity in state["harvested"]:
            _check_harvested(candidate, state["harvested"][candidate.identity])
            raise core.ParseError("Identity already harvested; output refused.")
        _validate_inbox_dataset(candidate, dataset, state)
        mark_harvested(candidate, state)
        destination = (destination or inbox_root()).absolute(); _ensure_directory(destination)
        output = destination / output_name(candidate)
        if output.exists() or output.is_symlink(): raise FileExistsError(output)
        raw = (json.dumps(dataset, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        stage = destination / (".inbox-" + uuid.uuid4().hex + ".tmp")
        journal = {"version": 1, "identity": candidate.identity, "entry": state["harvested"][candidate.identity],
                   "output": str(output), "stage": str(stage), "outputHash": hashlib.sha256(raw).hexdigest()}
        try:
            # The durable intent precedes creation/publication of either artifact.
            _atomic_json(journal, _journal_path(state_path))
            _checkpoint("inbox_prepared")
            _private_write(stage, raw)
            fsync_directory(destination)
            _checkpoint("inbox_staged")
            candidate.verified_bytes()
            # Hard-link publication is atomic and exclusive, including symlinks.
            os.link(stage, output)
            _checkpoint("inbox_linked")
            fsync_directory(destination)
            _checkpoint("inbox_output_synced")
            save_state(state, state_path)
            _checkpoint("inbox_state_committed")
        except Exception:
            # On a crash the same recovery runs on the next locked transaction.
            _recover_inbox(state_path)
            raise
        _recover_inbox(state_path)
    return output


def _validate_inbox_slice_id(dataset: Any) -> None:
    runs = dataset.get("runs") if isinstance(dataset, dict) else None
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        return  # Malformed envelopes are refused by _validate_inbox_dataset.
    slice_id = runs[0].get("sliceId")
    if (not isinstance(slice_id, str) or not slice_id or slice_id != slice_id.strip(PENNYTEL_TRIM_CHARS)
            # JavaScript String.length counts UTF-16 code units.
            or len(slice_id.encode("utf-16-le", errors="surrogatepass")) > 400):
        raise core.ParseError("Inbox sliceId must be nonempty text, exactly trimmed, and at most 200 characters.")


def _validate_inbox_dataset(candidate: Candidate, dataset: Any, state: dict) -> None:
    if not isinstance(dataset, dict) or type(dataset.get("schemaVersion")) is not int or dataset["schemaVersion"] not in (1, 2):
        raise core.ParseError("Inbox requires a v1/v2 dataset.")
    runs = dataset.get("runs")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise core.ParseError("Inbox requires exactly one intended harvested run.")
    run = runs[0]
    target = "v2" if dataset["schemaVersion"] == 2 else "v1"
    if json.dumps(dataset, sort_keys=True) != json.dumps(core.dataset_for_run(run, target), sort_keys=True):
        raise core.ParseError("Inbox requires the single-run parser dataset envelope.")
    if not state["runIds"].get(candidate.identity) or run.get("id") != state["runIds"][candidate.identity]:
        raise core.ParseError("Dataset run identity differs from persisted mapping.")
    for key in ("runType", "role"):
        if not isinstance(run.get(key), str) or not run[key].strip() or len(run[key]) > 100_000:
            raise core.ParseError("Inbox run requires valid semantic labels.")
    options = {"role": core.PENNYTEL_ROLES, "sessionMode": ("Fresh", "Resumed"),
               "contextMode": ("Full Repo", "Compact Packet", "Resumed Context", "Orchestrated Packet", "Other"),
               "result": ("Completed", "Accepted", "Needs repair", "Rejected", "Blocked", "Aborted")}
    for key, allowed in options.items():
        if key in run and run[key] not in allowed: raise core.ParseError("Invalid inbox semantic label.")
    for key in ("notes", "candidate"):
        if key in run and (not isinstance(run[key], str) or not run[key].strip() or len(run[key]) > 100_000):
            raise core.ParseError("Invalid inbox text label.")
    for key in ("model", "modelId", "provider", "providerId", "thinking"):
        if key in run and (not isinstance(run[key], str) or not run[key].strip() or len(run[key]) > 100_000):
            raise core.ParseError("Invalid inbox model/provider metadata.")
    for key in ("modelId", "providerId"):
        if key in run and (len(run[key]) > 200 or run[key] != run[key].strip()):
            raise core.ParseError("Invalid inbox registry identity.")
    evidence = run.get("executionEvidence")
    attribution = "Unknown"
    if isinstance(evidence, dict) and isinstance(evidence.get("quotaWindows"), list) and evidence["quotaWindows"]:
        first = evidence["quotaWindows"][0]
        if isinstance(first, dict): attribution = first.get("attribution")
    args = SimpleNamespace(turn_id=candidate.turn.turn_id, run_id=run["id"], slice_id=run["sliceId"],
                           run_type=run["runType"], role=run["role"], target=target, quota_attribution=attribution,
                           candidate=run.get("candidate"), session_mode=run.get("sessionMode"),
                           context_mode=run.get("contextMode"), result=run.get("result"))
    expected = core.build_run(candidate.records, candidate.path, args)
    if "notes" in run: expected["notes"] = run["notes"]
    if json.dumps(run, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise core.ParseError("Dataset metrics/provenance differ from the intended source run.")


def _journal_path(state_path: Path) -> Path:
    return state_path.with_suffix(".inbox-transaction.json")


def _private_write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())


def _recover_inbox(state_path: Path) -> None:
    """Complete cleanup for a committed state, otherwise roll back owned output.

    The staging inode proves output ownership; a colliding third-party file is
    never removed. The journal survives failures in recovery for another retry.
    """
    path = _journal_path(state_path)
    if not path.exists(): return
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal.get("version") != 1: raise ValueError()
        key, entry = journal["identity"], journal["entry"]
        cleaned = _clean_state({"version": STATE_VERSION, "harvested": {key: entry}, "runIds": {}})
        if cleaned["harvested"][key] != entry: raise ValueError()
        output, stage = Path(journal["output"]), Path(journal["stage"])
        if not output.is_absolute() or stage.parent != output.parent or not re.fullmatch(r"\.inbox-[a-f0-9]{32}\.tmp", stage.name): raise ValueError()
        if output.name != "codex-" + hashlib.sha256(key.encode()).hexdigest() + ".pennytel.json": raise ValueError()
        if not re.fullmatch("[a-f0-9]{64}", journal["outputHash"]): raise ValueError()
        if stage.is_symlink(): raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise core.ParseError("Inbox recovery journal is invalid; recovery refused.") from exc
    state = _read_state(state_path)
    committed = state["harvested"].get(key)
    if committed:
        if committed != entry or output.is_symlink() or not output.is_file() or hashlib.sha256(output.read_bytes()).hexdigest() != journal["outputHash"]:
            raise core.ParseError("Committed inbox output/state conflict; recovery refused.")
        # A previous save_state may have failed after replace but before dir fsync.
        fsync_directory(state_path.parent)
    elif output.exists() or output.is_symlink():
        if stage.exists() and not output.is_symlink() and os.path.samefile(stage, output):
            output.unlink()
            _checkpoint("inbox_rollback_unlinked")
        # Anything without the staging inode is an unrelated collision. This
        # remains true on a retry after staging cleanup was interrupted.
    fsync_directory(output.parent)
    if stage.exists(): stage.unlink()
    fsync_directory(stage.parent)
    _checkpoint("inbox_stage_removed")
    path.unlink()
    fsync_directory(path.parent)
    _checkpoint("inbox_journal_removed")


def curate_candidates(candidates: Iterable[Candidate], destination: Path) -> Path:
    # Preflight all candidates; use exactly these checked bytes for the copies.
    checked, identities = [], {}
    for candidate in candidates:
        raw = candidate.verified_bytes()
        key = candidate.identity
        if not key: raise core.ParseError("Invalid curation identity.")
        if key in identities:
            if identities[key] != candidate.source_hash: raise core.ParseError("Conflicting duplicate curation identity.")
            continue
        identities[key] = candidate.source_hash
        checked.append((candidate, raw))
    if not checked: raise core.ParseError("No completed runs selected for curation.")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink(): raise FileExistsError("Choose a new curated batch directory.")
    _ensure_directory(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=".curation-", dir=destination.parent))
    published = False
    try:
        manifest = []
        for candidate, raw in checked:
            records, turn, session, context = candidate.snapshot()
            model, _ = core.normalize_model(context.get("model") if isinstance(context.get("model"), str) else None)
            collaboration = context.get("collaboration_mode")
            settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
            thinking = core.normalize_thinking(settings.get("reasoning_effort") if isinstance(settings, dict) else context.get("effort"))
            folder = stage / safe_component(model)
            if model: folder /= safe_component(thinking)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            copied = folder / (candidate.source_hash + ".jsonl")
            if not copied.exists(): _private_write(copied, raw)
            elif copied.read_bytes() != raw: raise core.ParseError("Curated output collision.")
            _checkpoint("curation_copy")
            start, _, wall = core.task_times(turn)
            evidence = core._execution_evidence(records, candidate.path, turn, session, context)
            manifest.append({"sourceFile": candidate.path.name, "curatedFile": str(copied.relative_to(stage)),
                             "sourceHash": candidate.source_hash, "model": model, "thinking": thinking,
                             "startTime": start, "wallMinutes": wall, "evidence": evidence, "status": "completed"})
        _private_write(stage / "manifest.json", (json.dumps({"version": 1, "runs": manifest}, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
        _checkpoint("curation_manifest")
        # Detect drift at any earlier copy/manifest boundary before publication.
        for candidate, _ in checked: candidate.verified_bytes()
        for folder in sorted((p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True): fsync_directory(folder)
        fsync_directory(stage)
        if destination.exists() or destination.is_symlink(): raise FileExistsError(destination)
        os.rename(stage, destination)
        published = True
        _checkpoint("curation_published")
        fsync_directory(destination.parent)
        return destination / "manifest.json"
    except Exception:
        if published:
            # Unpublish atomically before cleanup, so cleanup failure/crash cannot
            # expose a partially deleted batch. Failed rename leaves a full batch.
            os.rename(destination, stage)
            fsync_directory(destination.parent)
        raise
    finally:
        if stage.exists(): shutil.rmtree(stage)
