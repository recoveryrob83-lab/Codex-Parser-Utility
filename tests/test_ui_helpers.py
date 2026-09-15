import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_parser_ui as ui
from test_codex_to_pennytel import fixture


class UIHelperTests(unittest.TestCase):
    def test_exact_folder_presets_are_conservative(self):
        run_type, role = ui.infer_folder_labels(Path('/tmp/batch/implementation/rollout-a.jsonl'))
        self.assertEqual((run_type, role), ('Implementation', 'Implementer'))

        run_type, role = ui.infer_folder_labels(Path('/tmp/batch/critic/rollout-b.jsonl'))
        self.assertIsNone(run_type)
        self.assertEqual(role, 'Critic')

        run_type, role = ui.infer_folder_labels(Path('/tmp/batch/re-critic/rollout-c.jsonl'))
        self.assertEqual((run_type, role), ('Re-Critic', 'Critic'))

    def test_slice_inference_uses_explicit_slice_number_only(self):
        self.assertEqual(ui.infer_slice_id('/home/rob/dev/PennyTel-AX-slice-5'), 'S5')
        self.assertEqual(ui.infer_slice_id('eng/pennytel-slice-4-accepted-outcome-economics'), 'S4')
        self.assertIsNone(ui.infer_slice_id('eng/pennytel-comparison-plan-runner'))

    def test_inspection_never_turns_path_into_slice_semantics(self):
        with tempfile.TemporaryDirectory(prefix="slice-99-") as temp:
            path = Path(temp) / "implementation" / "rollout-a.jsonl"
            path.parent.mkdir(); path.write_text(fixture(), encoding="utf-8")
            item = ui.inspect_log(path)
            self.assertIsNone(item.slice_id)
            self.assertEqual((item.inferred_run_type, item.inferred_role), ("Implementation", "Implementer"))
            self.assertEqual(item.status, "Needs labels")

    def test_output_preserves_input_subfolders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'input'
            source = root / 'implementation' / 'rollout-a.jsonl'
            source.parent.mkdir(parents=True)
            source.touch()
            item = ui.InspectedLog(source, [], None, {}, {}, None, None, None, 'S5', 'Implementation', 'Implementer', 'Ready')
            destination = ui.output_path_for(item, root, Path(temp) / 'output')
            self.assertEqual(destination.relative_to(Path(temp) / 'output'), Path('implementation') / ui.core.output_filename(source))


if __name__ == '__main__':
    unittest.main()
