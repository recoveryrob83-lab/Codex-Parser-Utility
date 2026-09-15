import argparse, contextlib, hashlib, io, json, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_to_pennytel as parser

def line(timestamp, ordinal, type_, payload):
    return json.dumps({"timestamp": timestamp, "ordinal": ordinal, "type": type_, "payload": payload})

def fixture(two_turns=False, model="gpt-5.6-sol", thinking="medium", auto=False, optional=True):
    session = {"session_id": "session-1", "cli_version": "0.154.0", "originator": "codex-tui", "cwd": "/work/repo", "model_provider": "openai", "git": {"repository_url": "https://github.com/example/repo.git", "branch": "eng/test", "commit_hash": "a" * 40}}
    if auto: session["thread_source"] = "guardian_review"
    rows = [line("2026-09-15T02:00:00.000Z", 0, "session_meta", session),
        line("2026-09-15T02:00:01.000Z", 1, "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100, "reasoning_output_tokens": 25}}}),
        line("2026-09-15T02:01:00.000Z", 2, "event_msg", {"type": "task_started", "turn_id": "turn-1", **({"model_context_window": 258400} if optional else {})}),
        line("2026-09-15T02:01:00.100Z", 3, "turn_context", {"turn_id": "turn-1", "timezone": "America/Chicago", "model": model, "collaboration_mode": {"settings": {"reasoning_effort": thinking}}, **({"sandbox_policy": {"type": "workspace-write", "network_access": False}, "approval_policy": "on-request", "approvals_reviewer": "auto_review", "permission_profile": {"network": "restricted"}} if optional else {})})]
    if optional:
        for n, usage in enumerate(((2000, 1500, 180, 40), (3000, 2400, 220, 60)), 4):
            rows.append(line(f"2026-09-15T02:01:0{n}.000Z", n, "token_usage_record", {"turn_id": "turn-1", "response_id": f"response-{n}", "usage": dict(zip(parser.TOKEN_KEYS, usage)), "turn_token_usage": dict(zip(parser.TOKEN_KEYS, (4000, 3200, 400, 100)))}))
        rows.append(line("2026-09-15T02:01:10.000Z", 6, "event_msg", {"type": "item_completed", "turn_id": "turn-1", "item": {"type": "CommandExecution", "id": "tool-1"}}))
    rows.extend([
        line("2026-09-15T02:01:20.000Z", 7, "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 3000, "cached_input_tokens": 2300, "output_tokens": 300, "reasoning_output_tokens": 75}}, "rate_limits": {"primary": {"used_percent": 29.0, "window_minutes": 10080, "resets_at": 1800000000}, "plan_type": "prolite"}}),
        line("2026-09-15T02:02:00.000Z", 8, "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 500, "reasoning_output_tokens": 125}}, "rate_limits": {"primary": {"used_percent": 31.5, "window_minutes": 10080, "resets_at": 1800000000}, "plan_type": "prolite"}}),
        line("2026-09-15T02:03:00.000Z", 9, "event_msg", {"type": "task_complete", "turn_id": "turn-1", "duration_ms": 120000, **({"time_to_first_token_ms": 345.5} if optional else {})})])
    if two_turns:
        rows.extend([line("2026-09-15T02:04:00.000Z", 10, "event_msg", {"type": "task_started", "turn_id": "turn-2"}), line("2026-09-15T02:04:10.000Z", 11, "turn_context", {"turn_id": "turn-2", "model": "gpt-5.6-luna", "effort": "max"}), line("2026-09-15T02:04:20.000Z", 12, "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 6000, "cached_input_tokens": 4500, "output_tokens": 600, "reasoning_output_tokens": 150}}}), line("2026-09-15T02:05:00.000Z", 13, "event_msg", {"type": "task_complete", "turn_id": "turn-2", "duration_ms": 60000})])
    return "\n".join(rows) + "\n"

class ParserTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = dict(turn_id=None, run_id="run-1", slice_id="slice-1", run_type="Implementation", role="Implementer", candidate=None, model=None, thinking=None, session_mode="Fresh", context_mode="Compact Packet", result="Completed", notes=None, target="v2", quota_attribution="Unknown")
        values.update(overrides); return argparse.Namespace(**values)

    def build(self, content=None, **args):
        temp = tempfile.TemporaryDirectory(); path = Path(temp.name) / "rollout-safe.jsonl"; path.write_text(content or fixture(), encoding="utf-8")
        return temp, path, parser.build_run(parser.load_records(path), path, self.make_args(**args))

    def test_v2_tokens_models_and_privacy(self):
        temp, path, run = self.build(); self.addCleanup(temp.cleanup)
        self.assertEqual((run["inputTokens"], run["cachedInputTokens"], run["outputTokens"], run["reasoningTokens"]), (800, 3200, 400, 100))
        self.assertEqual((run["model"], run["thinking"]), ("GPT-5.6 Sol", "Medium"))
        rendered = json.dumps(run)
        for private in ("command", "base_instructions", "raw_content", "message"): self.assertNotIn(private, rendered)
        self.assertNotIn("usageBefore", run); self.assertNotIn("usageAfter", run)

    def test_exact_v2_evidence_mapping(self):
        temp, path, run = self.build(); self.addCleanup(temp.cleanup); evidence = run["executionEvidence"]
        self.assertEqual(evidence["sourceLog"]["contentHash"]["value"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((evidence["sessionId"], evidence["turnId"], evidence["runtimeVersion"], evidence["originator"]), ("session-1", "turn-1", "0.154.0", "codex-tui"))
        self.assertEqual(evidence["repository"]["baselineCommitSha"], "a" * 40)
        self.assertEqual((evidence["timeToFirstTokenMs"], evidence["modelInvocationCount"], evidence["toolCallCount"], evidence["modelContextWindowTokens"]), (345.5, 2, 1, 258400))
        self.assertEqual(evidence["peakInvocation"], {"inputTokens": 3000, "cachedInputTokens": 2400})
        quota = evidence["quotaWindows"][0]
        self.assertEqual((quota["first"]["usedPercent"], quota["last"]["usedPercent"], quota["windowMinutes"], quota["planType"], quota["attribution"]), (29.0, 31.5, 10080, "prolite", "Unknown"))
        self.assertEqual(evidence["environment"], {"sandboxMode": "workspace-write", "approvalPolicy": "on-request", "approvalReviewer": "auto_review", "networkAccess": "restricted"})

    def test_explicit_clean_attribution_only(self):
        temp, _, run = self.build(quota_attribution="Clean"); self.addCleanup(temp.cleanup)
        self.assertEqual(run["executionEvidence"]["quotaWindows"][0]["attribution"], "Clean")

    def test_missing_optional_evidence_is_omitted(self):
        temp, _, run = self.build(fixture(optional=False)); self.addCleanup(temp.cleanup); evidence = run["executionEvidence"]
        for key in ("timeToFirstTokenMs", "modelInvocationCount", "toolCallCount", "modelContextWindowTokens", "peakInvocation", "environment"): self.assertNotIn(key, evidence)

    def test_v1_and_v2_are_explicit_and_separate(self):
        temp, _, v1 = self.build(target="v1"); self.addCleanup(temp.cleanup)
        self.assertNotIn("executionEvidence", v1)
        self.assertIn("coarse global meter", v1["notes"])
        self.assertEqual(parser.dataset_for_run(v1, "v1")["schemaVersion"], 1)
        self.assertEqual(parser.dataset_for_run({"executionEvidence": {}}, "v2")["schemaVersion"], 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parser().parse_args(["x", "--slice-id", "s", "--run-id", "r", "--run-type", "x", "--role", "Critic"])

    def test_astra_sol_luna_and_thinking_normalization(self):
        expected = (("gpt-6-astra", "xhigh", "GPT-6 Astra", "ExtraHigh"), ("gpt-5.6-sol", "high", "GPT-5.6 Sol", "High"), ("gpt-5.6-luna", "max", "GPT-5.6 Luna", "Max"))
        for raw, effort, model, thinking in expected:
            temp, _, run = self.build(fixture(model=raw, thinking=effort)); self.addCleanup(temp.cleanup); self.assertEqual((run["model"], run["thinking"]), (model, thinking))

    def test_multiple_turns_require_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"; path.write_text(fixture(two_turns=True), encoding="utf-8"); records = parser.load_records(path)
            with self.assertRaisesRegex(parser.ParseError, "multiple completed turns"): parser.build_run(records, path, self.make_args())
            run = parser.build_run(records, path, self.make_args(turn_id="turn-2"))
            self.assertEqual((run["model"], run["thinking"]), ("GPT-5.6 Luna", "Max"))

    def test_auto_review_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"; path.write_text(fixture(auto=True), encoding="utf-8")
            with self.assertRaisesRegex(parser.ParseError, "control sessions"):
                parser.build_run(parser.load_records(path), path, self.make_args())

    def test_recursive_batch_preserves_structure(self):
        with tempfile.TemporaryDirectory() as temp:
            root, output = Path(temp) / "input", Path(temp) / "output"; source = root / "implementation" / "rollout-a.jsonl"; source.parent.mkdir(parents=True); source.write_text(fixture(), encoding="utf-8")
            rc = parser.main([str(root), "--target", "v2", "--slice-id", "s", "--run-type", "Implementation", "--role", "Implementer", "-o", str(output), "--state-file", str(Path(temp) / "state.json")])
            self.assertEqual(rc, 0); self.assertTrue((output / "implementation" / parser.output_filename(source)).exists())

    def test_bad_json_reports_line(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"; path.write_text("{}\nnot-json\n", encoding="utf-8")
            with self.assertRaisesRegex(parser.ParseError, "line 2"): parser.load_records(path)

if __name__ == "__main__": unittest.main()
