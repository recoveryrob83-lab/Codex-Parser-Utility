import json, tempfile, unittest
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harvest
import codex_to_pennytel as core
from test_codex_to_pennytel import fixture
import test_codex_to_pennytel as parser_tests

class HarvestTests(unittest.TestCase):
    def test_find_new_runs_and_identity_dedupe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sessions"; source = root / "2026" / "rollout-a.jsonl"; source.parent.mkdir(parents=True); source.write_text(fixture(), encoding="utf-8")
            state = {"version": 1, "harvested": {}}
            candidates = harvest.find_new_runs([root], state)
            self.assertEqual(len(candidates), 1); self.assertEqual(candidates[0].identity, harvest.identity_key("session-1", "turn-1"))
            harvest.mark_harvested(candidates[0], state)
            self.assertEqual(harvest.find_new_runs([root], state), [])
            self.assertEqual(state["harvested"][harvest.identity_key("session-1", "turn-1")]["sourceHash"], candidates[0].source_hash)

    def test_multi_turn_file_yields_independent_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "rollout-many.jsonl").write_text(fixture(two_turns=True), encoding="utf-8")
            candidates = harvest.find_new_runs([root], {"harvested": {}})
            self.assertEqual([item.turn.turn_id for item in candidates], ["turn-1", "turn-2"])

    def test_state_round_trip_contains_only_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"; state = {"version": 1, "harvested": {"s:t": {"sessionId": "s", "turnId": "t", "sourceFile": "x", "sourceHash": "a" * 64, "unexpected": "DROP-ME"}}}
            harvest.save_state(state, path); loaded = harvest.load_state(path)
            self.assertNotIn("unexpected", loaded["harvested"][harvest.identity_key("s", "t")]); self.assertNotIn("DROP-ME", json.dumps(loaded))
            self.assertNotIn("DROP-ME", path.read_text())

    def test_inbox_name_and_date_are_stable(self):
        self.assertTrue(str(harvest.inbox_root(datetime(2026, 9, 15))).endswith("PennyTel-Inbox/2026-09-15"))

    def test_save_to_inbox_writes_import_and_dedupe_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sessions"; root.mkdir(); (root / "rollout-a.jsonl").write_text(fixture(), encoding="utf-8")
            candidate = harvest.find_new_runs([root], {"harvested": {}})[0]
            state_path = Path(temp) / "config" / "state.json"
            run_id = harvest.ensure_run_id(candidate.records, candidate.turn, state_path)
            run = core.build_run(candidate.records, candidate.path, parser_tests.ParserTests.make_args(self, run_id=run_id))
            dataset = core.dataset_for_run(run, 'v2')
            output = harvest.save_to_inbox(candidate, dataset, Path(temp) / "Inbox" / "2026-09-15", state_path)
            self.assertEqual(json.loads(output.read_text()), dataset)
            self.assertIn(candidate.identity, harvest.load_state(state_path)["harvested"])

    def test_curated_copy_does_not_mutate_source_and_manifest_is_reduced(self):
        with tempfile.TemporaryDirectory() as temp:
            root, curated = Path(temp) / "sessions", Path(temp) / "Curated"; root.mkdir(); source = root / "rollout-a.jsonl"; source.write_text(fixture(), encoding="utf-8"); before = source.read_bytes()
            candidate = harvest.find_new_runs([root], {"harvested": {}})[0]
            manifest_path = harvest.curate_candidates([candidate], curated)
            self.assertEqual(source.read_bytes(), before)
            manifest = json.loads(manifest_path.read_text()); rendered = json.dumps(manifest)
            self.assertIn("GPT-5.6 Sol/Medium", rendered)
            for forbidden in ("PRIVATE", "command", "message", "raw_content"): self.assertNotIn(forbidden, rendered)
            copied = curated / manifest["runs"][0]["curatedFile"]; self.assertEqual(copied.read_bytes(), before)

    def test_auto_review_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "rollout-auto.jsonl").write_text(fixture(auto=True), encoding="utf-8")
            self.assertEqual(harvest.find_new_runs([root], {"harvested": {}}), [])

if __name__ == "__main__": unittest.main()
