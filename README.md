# Codex Telemetry Parser v0.2

Stdlib-first, privacy-reducing adapter:

`Codex rollout JSONL -> reviewed semantic labels -> PennyTel import JSON`

It supports explicit `v1` and `v2` PennyTel targets. Selecting v2 adds the current
PennyTel 0.2.1 `Run.executionEvidence` object; v1 never receives v2 fields.

## Safety boundaries

Codex evidence may supply model/thinking, time, tokens, runtime, repository,
invocation/tool counts, context occupancy, quota observations, and recorded
environment constraints. It never supplies `sliceId`, `runType`, `role`, `result`,
or `contextMode`. Exact operator-created category folder names may provide run type
and role presets; slice is always entered explicitly. Unlabeled and ambiguous rows
remain non-emittable.

Emitted JSON excludes prompts, messages, AGENTS/system instructions, reasoning
text, source excerpts, tool commands/output, and arbitrary payloads. The source
basename and SHA-256 digest are provenance only. Codex `used_percent` is retained
only as quota evidence with `Unknown` attribution by default and is never mapped to
PennyTel `usageBefore`/`usageAfter`.

Token accounting is:

- `inputTokens = Codex input_tokens - cached_input_tokens`
- `cachedInputTokens = cached_input_tokens`
- `outputTokens = output_tokens` (already includes reasoning)
- `reasoningTokens = reasoning_output_tokens` (subset; not billed again)

## UI

```bash
python3 apps/codex-telemetry-parser/codex_parser_ui.py
```

The UI retains file and recursive-folder input and exposes the output target. Its
steady-state actions are:

- **Find New Runs** scans `~/.codex/sessions` and, when present,
  `~/.codex/archived_sessions`. Each completed turn is shown independently;
  control/guardian-review sessions and previously harvested session+turn identities
  are skipped after checking persisted evidence integrity.
- **Review & Label** locates the first row still missing explicit semantics.
- **Save to Inbox** writes ordinary import JSON under
  `~/PennyTel-Inbox/YYYY-MM-DD/` in a recoverable transaction with normalized fingerprint/provenance state
  under the platform config directory (`~/.config/penny-codex-parser/` on Linux).
- **Curate Logs** copies (never moves/deletes) sources into a new private batch with
  model/thinking folders and a reduced `manifest.json`.

For ordinary folder output, relative source subfolders are preserved. A manually
selected multi-turn file is refused; Find New Runs is the explicit per-turn
selection workflow.

## CLI

Target selection is required:

```bash
python3 apps/codex-telemetry-parser/codex_to_pennytel.py rollout.jsonl \
  --target v2 --slice-id S6 --run-id my-run --run-type Implementation \
  --role Implementer --result Completed -o /tmp/my-run.json
```

Pass `--turn-id` for a multi-turn file. A folder input is searched recursively,
requires `--output` as a directory, preserves subfolders, and refuses ambiguous
files. Omit `--run-id` for folders; each tuple receives a persisted opaque UUID. `--quota-attribution Clean|Contaminated` is an explicit operator review;
the default is `Unknown`.

## v2 evidence mapping

- exact file bytes -> `sourceLog.contentHash` (`sha256`); basename -> `fileName`
- session metadata -> `sessionId`, `runtimeVersion`, `originator`,
  `workingDirectory`, and validated repository URL/branch/full commit SHA
- task completion -> `turnId`, `timeToFirstTokenMs`
- scoped token-usage responses -> `modelInvocationCount` and cache-inclusive
  `peakInvocation`; associated windows are included only when recorded together
- completed tool items -> `toolCallCount`
- task start -> `modelContextWindowTokens`
- first/last primary and secondary meter observations -> `quotaWindows`
- recorded sandbox/approval/reviewer/network enums -> `environment`

Missing or invalid optional evidence is omitted rather than guessed.

## Tests

```bash
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
```

Fixtures are reduced and synthetic. Raw local Codex logs are never stored in this
repository.

## Repair contract: evidence, identity, and source consistency

Token source precedence is deliberately conservative:

1. Within the selected `task_started`/`task_complete` span, take the last matching
   `token_usage_record.turn_token_usage` snapshot. This is recorded per-turn usage,
   independent of compacted/reset thread counters.
2. Preserve only fields present in that snapshot. Never backfill omitted fields
   from older snapshots or manufacture zeros. Fresh input requires both inclusive
   `input_tokens` and `cached_input_tokens`; output and reasoning remain independently
   optional. Explicit recorded zero remains zero.
3. There is currently **no cumulative-delta fallback**: the supported older
   `total_token_usage` shape does not prove uninterrupted counter continuity or
   complete turn coverage. Legacy/tokenless turns remain representable with Unknown
   token totals. Invocation usage is occupancy evidence, not a guessed turn sum.

Lifecycle boundaries prefer `task_started.started_at` (then the completion's
recorded `started_at`) and `task_complete.completed_at`, accepting recorded Unix
seconds or ISO timestamps. Envelope timestamps are fallback only when payload
boundaries are absent. Finite recorded `duration_ms` overrides elapsed minutes;
TTFT comes only from `time_to_first_token_ms`. OpenAI telemetry uses display provider
`OpenAI` and registry provider ID `openai-api`; `openai` is the registry maker ID,
not a provider ID and not an additional Run field.

Harvest identity uses canonical JSON encoding of the **tuple** `[sessionId, turnId]`.
Identical duplicate observations yield one candidate. Conflicting duplicate bytes
or turn observations stop the scan visibly. Previously harvested tuples are checked
for evidence conflicts before they can be skipped.
A random UUIDv4 is reserved when a candidate first needs a PennyTel Run ID, under a
locked state transaction. This separate mapping survives review/reparse/restarts.
State v3 contains allowlisted provenance, a versioned normalized turn evidence
SHA-256, and `runIds`, never source bytes. The fingerprint covers emitted telemetry
and intermediate metric/identity observations, excluding source filename/full-file
hash, operator labels, prompts, messages, and tool output. Identical evidence and
unrelated appended turns skip normally; changed harvested evidence fails closed.
Raw-content-only changes are intentionally outside this normalized fingerprint.
Valid v1/v2 state migrates without fabricating old fingerprints: unchanged full-file
hashes can skip, but changed legacy files require explicit operator rebaseline.
Corrupt, unsupported,
or unreadable state blocks harvesting until explicitly restored/recreated. Writes
use a private temporary file, file fsync, atomic replacement, and directory fsync.
State reads and updates retain the existing exclusive lock and perform inbox recovery.

CLI `--run-id` is an explicit **single-file** override; without it, the same persisted
UUID mapping is used. Folder runs always use individual UUIDs. `--state-file PATH`
selects a state location for CLI use/testing. Ordinary folder output retains relative
subfolders with bounded SHA-256 basenames. Inbox names hash the full tuple. All JSON
outputs use exclusive creation: an existing destination is refused, never overwritten.
Use a new destination for a revised export.

Parsing and hashing use one byte read. Inspection keeps parsed records and their
immutable original bytes/digest/root in memory. Emission/curation rechecks containment
and byte equality, then reparses those exact retained bytes. Caller-accessible
dictionary/list edits cannot change metrics, identity, provenance, or curated content.
A changed source
becomes stale/non-emittable and requires refresh/reparse. Curation writes the verified
bytes themselves, with digest-only source basenames; it never reopens a newer source
for copying. No raw source bytes enter harvest state or import JSON.

Inbox publication accepts exactly one run matching the persisted UUID and freshly
derived source metrics/provenance. Empty, extra, wrong-tuple, modified-metric, invalid
label, and unsupported-envelope imports are refused. The inbox supports the parser's
single-run v1/v2 envelope; referenced slices must already exist in PennyTel.
Before entering the state transaction, the detached run's `sliceId` must be a
nonempty, exactly trimmed string of at most 200 characters under PennyTel's
JavaScript trim/UTF-16 length rules. Invalid IDs produce no inbox, journal, or
harvested-state change; correcting the label permits a normal retry.
A private durable journal precedes private staging. The complete JSON is published
with an exclusive hard link, then the destination directory is fsynced before
harvested state is committed. Recovery under the state lock keeps both committed
artifacts or removes only output owned by the staging inode and permits a normal
retry. Unrelated collisions are preserved. Hidden `.tmp` files are never imports.

Curation takes a new batch-directory destination (the UI creates a unique batch
inside the selected folder). All raw copies and the reduced manifest are staged
under a private sibling directory, created exclusively with mode `0600`, and fsynced.
Sources are checked again after copying and manifest creation. A directory rename
publishes the complete batch. Failures unpublish atomically before cleanup; originals
are never written. A hard process exit can leave a private hidden staging directory
or atomic-write temporary file, neither of which is treated as a published result.

Recursive discovery excludes resolved targets outside the selected root. Source
reads open path components through directory descriptors with `O_NOFOLLOW`, also
rejecting root/path symlink replacement. Repository URLs reject userinfo and strip
queries/fragments; unsupported/malformed URL forms are omitted. Source identifiers
are validated before inclusion, and are never copied to notes as a fallback.

Invocation counts use distinct recorded response IDs. Repeated fallback token
snapshots may supply occupancy but leave invocation count Unknown. Completed tools
are counted by stable item/call ID; unidentified tool completions leave count Unknown.
A peak and its optional context window come from the same observation; an observed
peak exceeding its associated window is omitted, never clamped. Changed quota
meter/plan/window identities cause that window's aggregate to be omitted. Reset
timestamps are endpoint observations: a compatible meter can retain different
first/last `resetsAt` values and a decrease to zero across replenishment. No burn
or clean attribution is inferred. PennyTel 0.2.1 accepts paired peak equality and
known zero, and rejects above-window pairs; invalid optional peaks remain omitted.
`dataset_for_run(..., 'v1')` strips v2 evidence without mutating its input.

### Verification and remaining limits

Run the parser suite and current-PennyTel/read-only real-log smoke:

```bash
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
python3 apps/codex-telemetry-parser/tests/smoke_pennytel.py \
  /path/to/PennyTel --sessions "$HOME/.codex/sessions" --sample 50
```

The smoke requires PennyTel package version **0.2.1** and uses its installed TypeScript compiler in memory, the actual
`validateExecutionEvidence`, `normalizeDataset`, `mergeImport`, and canonical seed
registry. It writes no PennyTel files, copies no real logs, and prints aggregate
results only. Synthetic parent slices/semantic labels exist solely for validation.

Unknown/new telemetry shapes, incomplete JSONL, unpaired lifecycle records, unknown
invocation identities, and ambiguous quota changes are not reconstructed. Legacy
cumulative-only logs retain Unknown run token totals. Multi-turn manual input still
requires explicit turn selection. A source that is actively appending must be
refreshed before emission. The hardened descriptor/locking implementation currently
requires POSIX (`O_NOFOLLOW`, directory descriptors, `flock`); Windows support is
not verified/supported by this repair. Full visual Tk smoke requires a working display.
