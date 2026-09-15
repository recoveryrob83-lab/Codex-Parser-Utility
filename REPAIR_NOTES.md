# Codex Telemetry Parser v0.2 repair handoff

## Authority and scope

- Issue: https://github.com/recoveryrob83-lab/Penny-Utilities/issues/1
- Parser branch: `eng/codex-telemetry-parser-v0.2`
- Unchanged parser HEAD: `a2274031c14db7cd86a1b18c9b41268782873be3`
- Current PennyTel authority: `c1b615154b11b3f5bb71df0e92d65ea2b8460181`, package 0.2.1.
- Accepted product candidate: `8589e444486389c1fe44d6691fd4467f62cb2ee7`.
- The current authority's `docs/data-contract.md`, `src/shared/execution-evidence.ts`,
  and `src/shared/data.ts` were read directly. These three files have no difference
  between the accepted product candidate and accepted main.
- Changes remain in Penny-Utilities. Existing candidate edits were preserved and
  extended. PennyTel remains clean. No commit or push was performed.

## Finding dispositions

| Finding | Disposition |
| --- | --- |
| Mutable parsed snapshot/provenance divergence | Repaired: frozen byte authority; fresh parsing at emission and curation boundaries. |
| Empty inbox dataset can mark harvested | Repaired: exactly one intended run, persisted UUID, source metrics/provenance, envelope and semantic labels are required. |
| Invalid stable `sliceId` can publish and mark harvested | Repaired: validate the detached single run before entering the state transaction; no output/state/journal side effects on rejection, and corrected retry emits once. |
| Inbox output/state is not failure-atomic | Repaired: durable intent journal, private staging, exclusive hard-link publication, locked rollback/recovery. |
| Reset-boundary quota evidence is over-filtered | Repaired: reset timestamps are endpoint values; meter, plan and actual window identity remain compatibility gates. |
| Curated raw logs can widen permissions | Repaired: raw copies and manifests use exclusive `0600` creation under private staging. |
| Late curation failure leaves partial output | Repaired: complete batch staging, final source verification, atomic directory publication and atomic unpublication before cleanup. |
| Persisted harvested conflicts are skipped | Repaired: versioned normalized turn fingerprint checked before every harvested skip. |
| State replacement lacks directory fsync | Repaired: temporary file fsync, replace, containing-directory fsync; new directory links are also synced. |
| Above-window paired peaks should be retained | Rejected as stale authority, per Chief Engineering: invalid optional peak evidence remains omitted, never clamped. Current 0.2.1 rejection is tested directly. |

## Designs

### Immutable snapshots

`SourceRecords` retains immutable exact bytes and frozen path/root/digest fields.
Its parsed view remains convenient for inspection, but is disposable. Public run
and evidence emission verify source byte equality/containment and reparse the
retained bytes. Candidates freeze the selected turn and original session identity.
Curation copies those same bytes and builds its manifest from a fresh parsing of
them. Mutating nested payloads, lifecycle records, session/context dictionaries,
the parsed list, or previously returned output cannot change subsequent emission.

Thus bytes parsed = bytes hashed = bytes used for metrics = bytes curated. Detected
on-disk drift still marks the original snapshot stale until refresh/reparse.

### Inbox transaction

Before entering the state lock, `save_to_inbox()` detaches the dataset and calls
`_validate_inbox_slice_id()`. The ID must be a nonempty string, exactly trimmed,
and at most 200 characters under PennyTel's JavaScript `trim()` and UTF-16 length
rules. This rejects invalid IDs before lock/recovery, journal publication,
staging, output publication, or even in-memory harvested-state mutation.
Referenced slice existence remains the receiving PennyTel import's responsibility.

Under the existing exclusive state lock:

1. Recover any prior transaction and validate the remaining single-run dataset contract.
2. Persist a private journal containing normalized provenance/fingerprint, output
   and staging paths, and the rendered JSON digest. No raw log content is journaled.
3. Exclusively create and fsync the private staging file and destination directory.
4. Recheck the source; publish a complete JSON file using an exclusive hard link.
5. Fsync the destination directory, then atomically save and fsync harvested state.
6. Remove staging and journal, syncing their directories.

Recovery keeps a matching committed output/state pair. Otherwise it removes only
an output sharing the transaction's staging inode, cleans staging, and permits a
normal retry. Existing unrelated files/symlinks are preserved. State reads recover
before scans can skip a tuple. All UI harvest paths use this transaction.

### Persisted evidence

State v3 stores only allowlisted tuple/source provenance, the UUID mapping, and
`fingerprintVersion` / `evidenceHash`. The fingerprint hashes normalized turn
metrics/provenance plus intermediate invocation, token, tool-identity and quota
observations. It excludes whole-file name/hash, operator labels, prompts, messages,
tool commands and tool output. The normalized material itself is not persisted.

Identical harvested evidence skips; unrelated later turns do not change the old
fingerprint; changed harvested telemetry fails closed. Disappeared or malformed
previously harvested turns in a recognized source also fail visibly. Raw-content-only
changes are outside this intentionally normalized fingerprint.

Legacy v1/v2 entries retain their original full-file hashes. Without a historical
turn fingerprint, changed legacy files fail closed and require explicit operator
rebaseline; the parser does not silently assert that growth was unrelated.

### Curation and permissions

The curation API requires a new batch directory; the UI creates a unique batch
inside the operator's selected folder. Copies and manifest are staged under a
private sibling directory with exclusive `0600` file creation. All sources are
verified again after copying and manifest creation. Files/directories are synced,
then one directory rename publishes the complete batch. If publication cleanup
must roll back, a rename removes the complete batch from view before recursive
cleanup begins. Source logs are never written by curation.

### Quota resets and paired occupancy

Different `resetsAt` values and decreases to known zero are retained when meter,
plan, window length and explicit window identity remain compatible. Genuine
meter/window/plan changes omit the aggregate. Attribution is never inferred.
Paired occupancy accepts valid values, equality and zero. Above-window optional
peaks are omitted; current PennyTel validation also rejects a deliberately injected
above-window pair through evidence validation, normalization and import.

## Earlier repair verification

- Full parser suite: **70 tests passed**.
- Inbox fault injection: 13 commit/cleanup boundaries, each with ordinary errors
  and simulated crashes; nine additional subprocess `os._exit` recovery cases.
- Injected write, hard-link, replace, fsync and interrupted-rollback failures pass.
  Existing file/symlink collision preservation and detached-dataset mutation pass.
- Curation failures: source drift after manifest creation, manifest write,
  first copy, second-file copy, and post-publication failure all pass.
- Permissions under umask `0002`: raw curated JSONL, manifest, inbox JSON and state
  are `0600`; protected source permissions/content remain unchanged.
- Python compile/import checks and Node harness syntax check pass.
- `git diff --check` passes.
- Privacy tests cover reduced imports/manifests/state; repository scanning found
  no credential/key patterns or raw JSONL/JSON artifacts in the parser directory.
- Read-only real-log smoke sampled **50 files**: **37 completed production turns**,
  38 control turns skipped, one file without completed turns, one malformed file
  refused. All 37 emitted turns had exact token snapshots. No real log was copied.
- Synthetic smoke: **24 turns**, covering Astra/Sol/Luna, complete/partial/tokenless
  cases, valid/equal/zero/above-window peaks, and quota reset transitions.
- Current PennyTel 0.2.1: **122 imports** (61 v1, 61 v2) passed
  `normalizeDataset` / `mergeImport`; all 61 evidence objects passed
  `validateExecutionEvidence`; all 122 provider/model identities resolved and
  received registry price snapshots. Historical snapshots survive normalization
  and reimport without alteration.

Commands:

```bash
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
python3 -m compileall -q apps/codex-telemetry-parser
PYTHONPATH=apps/codex-telemetry-parser python3 -c 'import codex_to_pennytel, codex_parser_ui, harvest'
node --check apps/codex-telemetry-parser/tests/validate_pennytel.cjs
python3 apps/codex-telemetry-parser/tests/smoke_pennytel.py \
  /home/rob/dev/PennyTel-AX --sessions /home/rob/.codex/sessions --sample 50
git diff --check
```

## Final stable slice ID repair verification

- Focused harvest/hardening suites: **31 tests passed**.
- Full parser suite: **72 tests passed**.
- Two added full-inbox tests cover both v1/v2: 201-character rejection, leading
  and trailing whitespace, empty/blank/non-string/null values, BOM whitespace,
  and the UTF-16 length boundary. Exactly 200-character trimmed IDs and internal
  spaces are accepted with an empty parser `slices` array.
- Every invalid case proves no transaction/write/publication/mark call, unchanged
  state bytes and filesystem contents, absent inbox/journal, and a candidate still
  available to scan. Correcting the same dataset publishes exactly the intended
  run, retains its UUID mapping, marks it harvested, and refuses a duplicate save.
- Current PennyTel 0.2.1 smoke at
  `c1b615154b11b3f5bb71df0e92d65ea2b8460181`: **54 imports** (27 v1, 27 v2),
  including **six corrected inbox retries** with boundary-length/internal-space
  IDs. All 27 evidence objects validate; all 54 runs resolve registry identity and
  receive pricing; historical snapshots survive normalization and reimport.
- The smoke uses temporary synthetic fixtures and synthetic parent slices only.
  No live PennyTel dataset or real rollout log was inspected in this final pass.
- `git diff --check` and Node harness syntax check pass. The pre-existing candidate
  edits remain; no files were staged, committed, or pushed, and PennyTel is clean.

Final-pass commands:

```bash
PYTHONPATH=apps/codex-telemetry-parser:apps/codex-telemetry-parser/tests \
  python3 -m unittest -v test_harvest test_hardening
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
python3 apps/codex-telemetry-parser/tests/smoke_pennytel.py /home/rob/dev/PennyTel-AX
node --check apps/codex-telemetry-parser/tests/validate_pennytel.cjs
git diff --check
```

No remaining uncertainty was found for the repaired stable-ID inbox boundary.
Referenced slice existence and the platform/UI limits below remain unchanged.

## Remaining limits

- Hardened filesystem behavior requires POSIX directory descriptors, `O_NOFOLLOW`,
  `flock`, hard links and directory fsync. Windows is unsupported.
- Unknown telemetry, incomplete/malformed JSONL, unpaired lifecycle events and
  ambiguous manual multi-turn selection remain unsupported. Legacy cumulative-only
  telemetry does not produce guessed exact turn totals.
- Actively changing source files require refresh/reparse before emission.
- Legacy harvested entries cannot distinguish append-only growth until explicitly
  rebaselined; raw-content-only mutation is outside normalized evidence detection.
- A hard process exit can leave an unpublished private atomic-write temporary file
  or hidden curation staging directory. Neither is a successful/importable output.
- Referenced slices must exist in the receiving PennyTel dataset. The inbox accepts
  the parser's single-run envelope, not arbitrary multi-table imports.
- UI helpers and emission paths were exercised; no visual Tk session was run.

## Final worktree state

HEAD and branch remain unchanged; no staging, commit or push was performed.
Paths below are relative to `apps/codex-telemetry-parser/`:

```text
 M README.md
 M codex_parser_ui.py
 M codex_to_pennytel.py
 M tests/test_codex_to_pennytel.py
 M tests/test_ui_helpers.py
?? REPAIR_NOTES.md
?? harvest.py
?? tests/smoke_pennytel.py
?? tests/test_hardening.py
?? tests/test_harvest.py
?? tests/test_repairs.py
?? tests/validate_pennytel.cjs
```

In the earlier repair pass, the pre-existing dirty candidate accounted for all of
these paths except the new `REPAIR_NOTES.md` and `tests/test_hardening.py`.
The final stable-ID pass updated only `harvest.py`, `tests/test_hardening.py`,
`tests/smoke_pennytel.py`, `README.md`, and this handoff. All listed paths were
already dirty at the start of that final pass; it introduced no new status entries.
