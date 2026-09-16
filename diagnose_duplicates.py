#!/usr/bin/env python3
"""Read-only duplicate diagnostic for Codex rollout session/turn evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import codex_to_pennytel as core
import harvest


def signature_hash(candidate: harvest.Candidate) -> str:
    material = json.dumps(
        harvest._turn_signature(candidate),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def scan_root(root: Path, source_kind: str, groups: dict[str, list[dict]], stats: dict[str, int]) -> None:
    if not root.exists():
        return
    for path in core.discover_logs(root):
        stats["files"] += 1
        try:
            records = core.load_records(path, root)
            session = core.first_payload(records, "session_meta")
            for turn in core.find_turns(records):
                context = core.matching_turn_context(records, turn.turn_id)
                if core.is_auto_review(session, context):
                    stats["auto_review_skipped"] += 1
                    continue
                candidate = harvest.Candidate(path, records, turn, session, context, records.digest)
                if not candidate.identity:
                    stats["invalid_identity_skipped"] += 1
                    continue
                try:
                    evidence_hash = harvest.evidence_fingerprint(candidate)
                    turn_hash = signature_hash(candidate)
                except Exception as exc:
                    stats["fingerprint_errors"] += 1
                    print(f"FINGERPRINT ERROR: {path}: {exc}")
                    continue
                groups[candidate.identity].append(
                    {
                        "source_kind": source_kind,
                        "path": str(path),
                        "source_hash": candidate.source_hash,
                        "turn_hash": turn_hash,
                        "evidence_hash": evidence_hash,
                        "session_id": candidate.session_id,
                        "turn_id": turn.turn_id,
                    }
                )
                stats["turns"] += 1
        except Exception as exc:
            stats["file_errors"] += 1
            print(f"FILE ERROR: {path}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostic for duplicate Codex session/turn tuples."
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
        help="Active Codex sessions root.",
    )
    parser.add_argument(
        "--archived-root",
        type=Path,
        default=Path.home() / ".codex" / "archived_sessions",
        help="Archived Codex sessions root.",
    )
    parser.add_argument(
        "--non-archived-only",
        action="store_true",
        help="Scan active sessions only.",
    )
    args = parser.parse_args()

    groups: dict[str, list[dict]] = defaultdict(list)
    stats = defaultdict(int)

    scan_root(args.sessions_root, "sessions", groups, stats)
    if not args.non_archived_only:
        scan_root(args.archived_root, "archived_sessions", groups, stats)

    duplicate_groups = [rows for rows in groups.values() if len(rows) > 1]
    safe_duplicates = []
    true_conflicts = []

    for rows in duplicate_groups:
        if len({row["evidence_hash"] for row in rows}) == 1:
            safe_duplicates.append(rows)
        else:
            true_conflicts.append(rows)

    print("\n=== Duplicate diagnostic summary ===")
    print(f"Files scanned: {stats['files']}")
    print(f"Completed non-control turns: {stats['turns']}")
    print(f"Unique session/turn identities: {len(groups)}")
    print(f"Duplicate identity groups: {len(duplicate_groups)}")
    print(f"Same normalized evidence: {len(safe_duplicates)}")
    print(f"Different normalized evidence: {len(true_conflicts)}")
    print(f"Auto-review turns skipped: {stats['auto_review_skipped']}")
    print(f"File errors: {stats['file_errors']}")
    print(f"Fingerprint errors: {stats['fingerprint_errors']}")

    def show(label: str, collection: list[list[dict]]) -> None:
        if not collection:
            return
        print(f"\n=== {label} ===")
        for index, rows in enumerate(collection, 1):
            first = rows[0]
            print(f"\n[{index}] session={first['session_id']} turn={first['turn_id']}")
            for row in rows:
                print(f"  {row['source_kind']}: {row['path']}")
                print(f"    source_sha256={row['source_hash']}")
                print(f"    turn_sha256={row['turn_hash']}")
                print(f"    normalized_evidence_sha256={row['evidence_hash']}")

    show("Duplicates with identical normalized evidence", safe_duplicates)
    show("TRUE EVIDENCE CONFLICTS", true_conflicts)

    return 2 if true_conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
