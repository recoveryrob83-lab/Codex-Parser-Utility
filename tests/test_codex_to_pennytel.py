import argparse
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_to_pennytel as parser


def line(timestamp, ordinal, type_, payload):
    return json.dumps({"timestamp": timestamp, "ordinal": ordinal, "type": type_, "payload": payload})


def fixture(two_turns=False):
    rows = [
        line("2026-09-15T02:00:00.000Z", 0, "session_meta", {
            "session_id": "session-1",
            "cli_version": "0.154.0",
            "model_provider": "openai",
            "git": {
                "repository_url": "https://github.com/example/repo.git",
                "branch": "eng/test",
                "commit_hash": "abc123",
            },
        }),
        line("2026-09-15T02:00:01.000Z", 1, "event_msg", {
            "type": "token_count",
            "info": {"total_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 800,
                "output_tokens": 100,
                "reasoning_output_tokens": 25,
            }},
            "rate_limits": {"primary": {"used_percent": 29.0, "window_minutes": 10080, "resets_at": 999}, "plan_type": "prolite"},
        }),
        line("2026-09-15T02:01:00.000Z", 2, "event_msg", {"type": "task_started", "turn_id": "turn-1", "model_context_window": 258400}),
        line("2026-09-15T02:01:00.100Z", 3, "turn_context", {
            "turn_id": "turn-1",
            "timezone": "America/Chicago",
            "model": "gpt-5.6-sol",
            "collaboration_mode": {"settings": {"reasoning_effort": "medium"}},
        }),
        line("2026-09-15T02:02:00.000Z", 4, "event_msg", {
            "type": "token_count",
            "info": {"total_token_usage": {
                "input_tokens": 5000,
                "cached_input_tokens": 4000,
                "output_tokens": 500,
                "reasoning_output_tokens": 125,
            }},
            "rate_limits": {"primary": {"used_percent": 29.0, "window_minutes": 10080, "resets_at": 999}, "plan_type": "prolite"},
        }),
        line("2026-09-15T02:03:00.000Z", 5, "event_msg", {"type": "task_complete", "turn_id": "turn-1", "duration_ms": 120000}),
    ]
    if two_turns:
        rows.extend([
            line("2026-09-15T02:04:00.000Z", 6, "event_msg", {"type": "task_started", "turn_id": "turn-2"}),
            line("2026-09-15T02:04:10.000Z", 7, "turn_context", {"turn_id": "turn-2", "model": "gpt-5.6-luna"}),
            line("2026-09-15T02:04:20.000Z", 8, "event_msg", {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 6000, "cached_input_tokens": 4500, "output_tokens": 600, "reasoning_output_tokens": 150
            }}}),
            line("2026-09-15T02:05:00.000Z", 9, "event_msg", {"type": "task_complete", "turn_id": "turn-2", "duration_ms": 60000}),
        ])
    return "\n".join(rows) + "\n"


class ParserTests(unittest.TestCase):
    def make_args(self, **overrides):
        base = dict(
            turn_id=None,
            run_id="run-1",
            slice_id="slice-1",
            run_type="Implementation",
            role="Implementer",
            candidate=None,
            model=None,
            thinking=None,
            session_mode="Fresh",
            context_mode="Compact Packet",
            result="Completed",
            notes=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_derives_current_pennytel_fields_without_copying_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text(fixture(), encoding="utf-8")
            records = parser.load_records(path)
            run = parser.build_run(records, path, self.make_args())

        self.assertEqual(run["model"], "GPT-5.6 Sol")
        self.assertEqual(run["modelId"], "openai:gpt-5.6-sol")
        self.assertEqual(run["provider"], "OpenAI")
        self.assertEqual(run["providerId"], "openai")
        self.assertEqual(run["thinking"], "Medium")
        self.assertEqual(run["inputTokens"], 800)  # (5000-1000) - (4000-800)
        self.assertEqual(run["cachedInputTokens"], 3200)
        self.assertEqual(run["outputTokens"], 400)
        self.assertEqual(run["reasoningTokens"], 100)
        self.assertEqual(run["wallMinutes"], 2.0)
        self.assertEqual(run["localHour"], 21)
        self.assertEqual(run["dayOfWeek"], "Monday")
        self.assertNotIn("usageBefore", run)
        self.assertNotIn("usageAfter", run)
        self.assertIn("coarse global meter", run["notes"])
        self.assertNotIn("base_instructions", json.dumps(run))

    def test_output_is_schema_v1_additive_dataset_shape(self):
        dataset = parser.dataset_for_run({"id": "r", "sliceId": "s", "runType": "x", "role": "Critic"})
        self.assertEqual(dataset["schemaVersion"], 1)
        self.assertEqual(dataset["revision"], 0)
        self.assertEqual(dataset["slices"], [])
        self.assertEqual(dataset["runs"][0]["id"], "r")
        self.assertEqual(dataset["findings"], [])
        self.assertEqual(dataset["discoveries"], [])
        self.assertEqual(dataset["pricing"], [])

    def test_multiple_turns_require_explicit_turn_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text(fixture(two_turns=True), encoding="utf-8")
            records = parser.load_records(path)
            with self.assertRaisesRegex(parser.ParseError, "multiple completed turns"):
                parser.build_run(records, path, self.make_args())
            run = parser.build_run(records, path, self.make_args(turn_id="turn-2"))
        self.assertEqual(run["model"], "GPT-5.6 Luna")
        self.assertEqual(run["inputTokens"], 500)
        self.assertEqual(run["cachedInputTokens"], 500)

    def test_bad_json_reports_line(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text("{}\nnot-json\n", encoding="utf-8")
            with self.assertRaisesRegex(parser.ParseError, "line 2"):
                parser.load_records(path)


if __name__ == "__main__":
    unittest.main()
