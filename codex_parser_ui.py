#!/usr/bin/env python3
"""Tiny Tkinter wrapper around codex_to_pennytel.py.

The UI is intentionally boring: choose a rollout file or folder, inspect what Codex
actually recorded, supply/confirm PennyTel semantic labels, choose an output folder,
and emit one privacy-reduced PennyTel schema-v1 JSON file per rollout.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import codex_to_pennytel as core


# Folder names are operator intent, not Codex evidence. Only exact, deliberately
# supported names receive automatic semantic labels.
FOLDER_PRESETS: dict[str, tuple[str | None, str | None]] = {
    "implementation": ("Implementation", "Implementer"),
    "implementer": ("Implementation", "Implementer"),
    "independent-critic": ("Independent Critic", "Critic"),
    "initial-critic": ("Independent Critic", "Critic"),
    "re-critic": ("Re-Critic", "Critic"),
    "recritic": ("Re-Critic", "Critic"),
    "critic": (None, "Critic"),  # role is safe; exact run type still needs confirmation
    "repair": ("Repair", "Repair"),
    "context-scout": ("Context Scout", "Context Steward"),
    "scout": ("Context Scout", "Context Steward"),
    "scouting": ("Context Scout", "Context Steward"),
    "master-index-mapper": ("MASTER_INDEX Mapper", "Context Steward"),
    "master-index": ("MASTER_INDEX Mapper", "Context Steward"),
    "mapper": ("MASTER_INDEX Mapper", "Context Steward"),
}


@dataclass
class InspectedLog:
    path: Path
    records: list[core.Record]
    turn: core.TurnBounds | None
    session: dict
    context: dict
    model: str | None
    thinking: str | None
    wall_minutes: float | None
    slice_id: str | None
    inferred_run_type: str | None
    inferred_role: str | None
    status: str

    @property
    def session_id(self) -> str | None:
        value = self.session.get("session_id") or self.session.get("id")
        return value if isinstance(value, str) and value else None


def normalize_folder_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def infer_folder_labels(path: Path) -> tuple[str | None, str | None]:
    # Nearest parent wins so a specific subfolder can override a broad parent.
    for parent in path.parents:
        preset = FOLDER_PRESETS.get(normalize_folder_name(parent.name))
        if preset:
            return preset
    return None, None


def infer_slice_id(*values: object) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        match = re.search(r"(?i)slice[-_ ]?(\d+)", value)
        if match:
            return f"S{int(match.group(1))}"
    return None


def inspect_log(path: Path) -> InspectedLog:
    records = core.load_records(path)
    turns = core.find_turns(records)
    session = core.first_payload(records, "session_meta")
    run_type, role = infer_folder_labels(path)

    if len(turns) != 1:
        status = "No completed turn" if not turns else f"Needs turn selection ({len(turns)} turns)"
        return InspectedLog(
            path, records, None, session, {}, None, None, None,
            infer_slice_id(session.get("cwd"), (session.get("git") or {}).get("branch") if isinstance(session.get("git"), dict) else None),
            run_type, role, status,
        )

    turn = turns[0]
    context = core.matching_turn_context(records, turn.turn_id)
    raw_model = context.get("model") if isinstance(context.get("model"), str) else None
    model, _ = core.normalize_model(raw_model)
    collaboration = context.get("collaboration_mode")
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    raw_thinking = settings.get("reasoning_effort") if isinstance(settings, dict) else None
    thinking = core.normalize_thinking(raw_thinking)
    _, _, wall = core.task_times(turn)

    git = session.get("git") if isinstance(session.get("git"), dict) else {}
    slice_id = infer_slice_id(
        session.get("cwd"),
        context.get("cwd"),
        git.get("branch"),
    )

    if raw_model == "codex-auto-review":
        status = "SKIP auto-review"
    else:
        status = "Ready" if slice_id and run_type and role else "Needs labels"

    return InspectedLog(
        path=path,
        records=records,
        turn=turn,
        session=session,
        context=context,
        model=model,
        thinking=thinking,
        wall_minutes=wall,
        slice_id=slice_id,
        inferred_run_type=run_type,
        inferred_role=role,
        status=status,
    )


def discover_logs(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix == ".jsonl" else []
    if path.is_dir():
        return sorted(path.rglob("rollout-*.jsonl"))
    return []


def stable_run_id(item: InspectedLog) -> str:
    if item.turn:
        return f"codex-{item.turn.turn_id}"
    if item.session_id:
        return f"codex-{item.session_id}"
    return f"codex-{item.path.stem}"


def output_path_for(item: InspectedLog, input_root: Path, output_root: Path) -> Path:
    if input_root.is_dir():
        try:
            relative_parent = item.path.parent.relative_to(input_root)
        except ValueError:
            relative_parent = Path()
    else:
        relative_parent = Path()
    return output_root / relative_parent / f"{item.path.stem}.pennytel.json"


class ParserUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Penny Codex Telemetry Parser v0.1")
        self.minsize(1050, 620)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.slice_var = tk.StringVar()
        self.run_type_var = tk.StringVar()
        self.role_var = tk.StringVar()
        self.session_mode_var = tk.StringVar()
        self.context_mode_var = tk.StringVar()
        self.result_var = tk.StringVar()
        self.use_folder_labels_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Choose a Codex rollout file or folder.")

        self.input_root: Path | None = None
        self.items: list[InspectedLog] = []
        self._build()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(outer, text="Input").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(outer, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(outer, text="Select file", command=self.select_file).grid(row=0, column=2, padx=4, pady=4)
        ttk.Button(outer, text="Select folder", command=self.select_folder).grid(row=0, column=3, padx=4, pady=4)

        ttk.Label(outer, text="Output folder").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(outer, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(outer, text="Select output", command=self.select_output).grid(row=1, column=2, columnspan=2, sticky="ew", padx=4, pady=4)

        labels = ttk.LabelFrame(outer, text="PennyTel semantic labels", padding=8)
        labels.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 8))
        for i in range(8):
            labels.columnconfigure(i, weight=1 if i in (1, 3, 5, 7) else 0)

        ttk.Checkbutton(
            labels,
            text="Use exact folder-name presets when available",
            variable=self.use_folder_labels_var,
            command=self.refresh_tree,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(labels, text="Target: PennyTel schema v1 (parser v0.1)").grid(row=0, column=4, columnspan=4, sticky="e", pady=(0, 6))

        ttk.Label(labels, text="Slice override").grid(row=1, column=0, sticky="w")
        ttk.Entry(labels, textvariable=self.slice_var, width=15).grid(row=1, column=1, sticky="ew", padx=(4, 12))
        ttk.Label(labels, text="Run type override").grid(row=1, column=2, sticky="w")
        ttk.Entry(labels, textvariable=self.run_type_var, width=22).grid(row=1, column=3, sticky="ew", padx=(4, 12))
        ttk.Label(labels, text="Role override").grid(row=1, column=4, sticky="w")
        role = ttk.Combobox(labels, textvariable=self.role_var, values=("", *core.PENNYTEL_ROLES), state="readonly", width=18)
        role.grid(row=1, column=5, sticky="ew", padx=(4, 12))
        ttk.Button(labels, text="Refresh labels", command=self.refresh_tree).grid(row=1, column=6, columnspan=2, sticky="ew")

        ttk.Label(labels, text="Session mode").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(labels, textvariable=self.session_mode_var, values=("", "Fresh", "Resumed"), state="readonly", width=16).grid(row=2, column=1, sticky="ew", padx=(4, 12), pady=(6, 0))
        ttk.Label(labels, text="Context mode").grid(row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Combobox(
            labels,
            textvariable=self.context_mode_var,
            values=("", "Full Repo", "Compact Packet", "Resumed Context", "Orchestrated Packet", "Other"),
            state="readonly",
            width=22,
        ).grid(row=2, column=3, sticky="ew", padx=(4, 12), pady=(6, 0))
        ttk.Label(labels, text="Result").grid(row=2, column=4, sticky="w", pady=(6, 0))
        ttk.Combobox(
            labels,
            textvariable=self.result_var,
            values=("", "Completed", "Accepted", "Needs repair", "Rejected", "Blocked", "Aborted"),
            state="readonly",
            width=18,
        ).grid(row=2, column=5, sticky="ew", padx=(4, 12), pady=(6, 0))

        ttk.Label(
            labels,
            text="Blank overrides mean: infer slice from Codex cwd/branch and labels from exact category folders. Ambiguous rows are not emitted.",
        ).grid(row=3, column=0, columnspan=8, sticky="w", pady=(7, 0))

        cols = ("file", "model", "thinking", "minutes", "slice", "run_type", "role", "status")
        self.tree = ttk.Treeview(outer, columns=cols, show="headings", height=15)
        headings = {
            "file": "File",
            "model": "Model",
            "thinking": "Thinking",
            "minutes": "Minutes",
            "slice": "Slice",
            "run_type": "Run type",
            "role": "Role",
            "status": "Status",
        }
        widths = {"file": 260, "model": 120, "thinking": 85, "minutes": 70, "slice": 70, "run_type": 145, "role": 110, "status": 145}
        for col in cols:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(4, 8))
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=4, column=4, sticky="ns", pady=(4, 8))
        self.tree.configure(yscrollcommand=scrollbar.set)

        bottom = ttk.Frame(outer)
        bottom.grid(row=5, column=0, columnspan=4, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Parse data", command=self.parse_data).grid(row=0, column=1, sticky="e", padx=(8, 0))

        for var in (self.slice_var, self.run_type_var, self.role_var):
            var.trace_add("write", lambda *_: self.refresh_tree())

    def select_file(self) -> None:
        value = filedialog.askopenfilename(title="Choose Codex rollout", filetypes=(("Codex JSONL", "*.jsonl"), ("All files", "*")))
        if value:
            self.input_var.set(value)
            self.scan(Path(value))

    def select_folder(self) -> None:
        value = filedialog.askdirectory(title="Choose folder containing Codex rollouts")
        if value:
            self.input_var.set(value)
            self.scan(Path(value))

    def select_output(self) -> None:
        value = filedialog.askdirectory(title="Choose output folder")
        if value:
            self.output_var.set(value)

    def scan(self, root: Path) -> None:
        self.input_root = root
        paths = discover_logs(root)
        if not paths:
            self.items = []
            self.refresh_tree()
            self.status_var.set("No rollout-*.jsonl files found.")
            return

        items: list[InspectedLog] = []
        errors = 0
        for path in paths:
            try:
                items.append(inspect_log(path))
            except Exception as exc:
                errors += 1
                items.append(InspectedLog(path, [], None, {}, {}, None, None, None, None, None, None, f"ERROR: {exc}"))
        self.items = items
        self.refresh_tree()
        self.status_var.set(f"Scanned {len(items)} rollout file(s){f'; {errors} inspection error(s)' if errors else ''}.")

    def effective_labels(self, item: InspectedLog) -> tuple[str | None, str | None, str | None]:
        slice_id = self.slice_var.get().strip() or item.slice_id
        manual_run_type = self.run_type_var.get().strip() or None
        manual_role = self.role_var.get().strip() or None
        if self.use_folder_labels_var.get():
            run_type = manual_run_type or item.inferred_run_type
            role = manual_role or item.inferred_role
        else:
            run_type = manual_run_type
            role = manual_role
        return slice_id, run_type, role

    def refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        for item in self.items:
            slice_id, run_type, role = self.effective_labels(item)
            status = item.status
            raw_model = item.context.get("model") if isinstance(item.context, dict) else None
            if raw_model == "codex-auto-review":
                status = "SKIP auto-review"
            elif item.turn is not None:
                status = "Ready" if slice_id and run_type and role else "Needs labels"
            self.tree.insert(
                "",
                "end",
                values=(
                    item.path.name,
                    item.model or "",
                    item.thinking or "",
                    f"{item.wall_minutes:.2f}" if item.wall_minutes is not None else "",
                    slice_id or "",
                    run_type or "",
                    role or "",
                    status,
                ),
            )

    def parse_data(self) -> None:
        if not self.input_root or not self.items:
            messagebox.showerror("Nothing to parse", "Choose and scan a rollout file or folder first.")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showerror("Output folder required", "Choose an output folder first.")
            return
        output_root = Path(output_text).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)

        emitted = 0
        skipped_auto = 0
        skipped_labels: list[str] = []
        failed: list[str] = []

        for item in self.items:
            raw_model = item.context.get("model") if isinstance(item.context, dict) else None
            if raw_model == "codex-auto-review":
                skipped_auto += 1
                continue
            if item.turn is None:
                failed.append(f"{item.path.name}: {item.status}")
                continue

            slice_id, run_type, role = self.effective_labels(item)
            if not slice_id or not run_type or not role:
                skipped_labels.append(item.path.name)
                continue

            args = SimpleNamespace(
                turn_id=item.turn.turn_id,
                model=None,
                thinking=None,
                run_id=stable_run_id(item),
                slice_id=slice_id,
                run_type=run_type,
                role=role,
                candidate=None,
                session_mode=self.session_mode_var.get().strip() or None,
                context_mode=self.context_mode_var.get().strip() or None,
                result=self.result_var.get().strip() or None,
                notes=None,
            )
            try:
                run = core.build_run(item.records, item.path, args)
                dataset = core.dataset_for_run(run)
                destination = output_path_for(item, self.input_root, output_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")
                emitted += 1
            except Exception as exc:
                failed.append(f"{item.path.name}: {exc}")

        summary = [f"Wrote {emitted} PennyTel JSON file(s)."]
        if skipped_auto:
            summary.append(f"Skipped {skipped_auto} codex-auto-review file(s).")
        if skipped_labels:
            summary.append(f"Skipped {len(skipped_labels)} file(s) needing semantic labels.")
        if failed:
            summary.append(f"{len(failed)} file(s) failed or need turn selection.")
        self.status_var.set(" ".join(summary))

        details: list[str] = []
        if skipped_labels:
            details.append("Needs labels:\n" + "\n".join(skipped_labels[:12]))
        if failed:
            details.append("Not emitted:\n" + "\n".join(failed[:12]))
        if details:
            messagebox.showwarning("Batch finished with skips", "\n\n".join(summary + details))
        else:
            messagebox.showinfo("Batch complete", "\n".join(summary))


def main() -> None:
    ParserUI().mainloop()


if __name__ == "__main__":
    main()
