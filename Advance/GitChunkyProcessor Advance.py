import os
import re
import sys
import json
import time
import queue
import shutil
import tempfile
import threading
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


APP_TITLE = "Git Chunky Processor - Dark GUI v4"


# -----------------------------
# Data
# -----------------------------

@dataclass
class Chunk:
    number: int
    file_count: int
    size_mb: float
    files: List[str] = field(default_factory=list)
    status: str = "Pending"
    note: str = ""


# -----------------------------
# Parsing
# -----------------------------

CHUNK_RE = re.compile(r"^\s*Chunk\s+#(\d+)\s+\((\d+)\s+files?,\s*([\d.]+)MB\):\s*$", re.IGNORECASE)
FILE_RE = re.compile(r"^\s*-\s+(.+?)\s+\(([\d.]+)MB\)\s*$")


def normalize_rel_path(path_text: str) -> str:
    """Convert a log path into a clean Git path."""
    p = path_text.strip().strip('"').strip("'")
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def parse_chunks(log_path: str) -> List[Chunk]:
    chunks: List[Chunk] = []
    current: Optional[Chunk] = None

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")

            cm = CHUNK_RE.match(line.strip())
            if cm:
                if current:
                    chunks.append(current)
                current = Chunk(
                    number=int(cm.group(1)),
                    file_count=int(cm.group(2)),
                    size_mb=float(cm.group(3)),
                    files=[],
                )
                continue

            if current:
                fm = FILE_RE.match(line)
                if fm:
                    current.files.append(normalize_rel_path(fm.group(1)))

    if current:
        chunks.append(current)

    return chunks


# -----------------------------
# Git Engine
# -----------------------------

class GitError(RuntimeError):
    pass


class GitEngine:
    def __init__(self, repo_path: str, log_func):
        self.repo = Path(repo_path)
        self.git_dir = self.repo / ".git"
        self.log = log_func

    def ensure_repo(self):
        if not self.repo.exists() or not self.repo.is_dir():
            raise GitError("Repository folder does not exist.")
        if not self.git_dir.exists():
            raise GitError("Selected folder is not a Git repository.")

    def run(self, args: List[str], check=True, capture=True, timeout=None) -> subprocess.CompletedProcess:
        cmd_text = " ".join(args)
        attempts = 6

        for attempt in range(1, attempts + 1):
            self.cleanup_stale_index_lock()

            self.log(f"$ {cmd_text}")

            try:
                cp = subprocess.run(
                    args,
                    cwd=str(self.repo),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.STDOUT if capture else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as e:
                raise GitError(f"Command timed out: {cmd_text}") from e

            out = cp.stdout or ""
            if out.strip():
                self.log(out.rstrip())

            if cp.returncode == 0 or not check:
                return cp

            if self.is_index_lock_error(out) and attempt < attempts:
                self.log("Git index is locked. Waiting briefly, then retrying...")
                time.sleep(2)
                self.cleanup_stale_index_lock(min_age_seconds=0)
                continue

            raise GitError(f"Command failed with code {cp.returncode}: {cmd_text}\n{out}")

        return cp

    @staticmethod
    def is_index_lock_error(output: str) -> bool:
        text = (output or "").lower()
        return "index.lock" in text and "unable to create" in text

    def cleanup_stale_index_lock(self, min_age_seconds: int = 10):
        lock = self.git_dir / "index.lock"
        if not lock.exists():
            return

        # Wait briefly in case a Git process is legitimately finishing.
        age = time.time() - lock.stat().st_mtime
        if age < min_age_seconds:
            return

        if self.any_git_process_running():
            return

        try:
            lock.unlink()
            self.log(f"🧹 Removed stale Git lock: {lock}")
        except Exception as e:
            raise GitError(f"Could not remove stale index.lock. Close Git/Editor processes or remove manually: {lock}\n{e}")

    @staticmethod
    def any_git_process_running() -> bool:
        # Best effort. No psutil dependency.
        if os.name == "nt":
            try:
                cp = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq git.exe"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                return "git.exe" in (cp.stdout or "").lower()
            except Exception:
                return False
        else:
            try:
                cp = subprocess.run(["pgrep", "-x", "git"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                return cp.returncode == 0
            except Exception:
                return False

    def remote_v(self):
        self.run(["git", "remote", "-v"], check=False)

    def valid_files(self, files: List[str]) -> Tuple[List[str], List[str]]:
        valid, missing = [], []
        for f in files:
            clean = normalize_rel_path(f)
            abs_path = (self.repo / clean).resolve()
            try:
                abs_path.relative_to(self.repo.resolve())
            except ValueError:
                missing.append(clean)
                continue

            if abs_path.exists():
                valid.append(clean)
            else:
                missing.append(clean)
        return valid, missing

    def add_files_fast(self, files: List[str]):
        if not files:
            return

        # NUL-separated pathspec file. This fixes the Windows Git long-path BUG caused by newline + --pathspec-file-nul.
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="chunky_pathspec_", suffix=".txt")
            with os.fdopen(fd, "wb") as tmp:
                for p in files:
                    tmp.write(normalize_rel_path(p).encode("utf-8"))
                    tmp.write(b"\0")

            self.run(["git", "add", "--pathspec-from-file", tmp_path, "--pathspec-file-nul"])
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def has_staged_changes(self) -> bool:
        cp = self.run(["git", "diff", "--cached", "--quiet"], check=False)
        return cp.returncode != 0

    def commit(self, message: str) -> bool:
        if not self.has_staged_changes():
            self.log("⚠️ No staged changes. Commit skipped.")
            return False

        self.run(["git", "commit", "-m", message])
        return True

    def pull_rebase(self):
        self.run(["git", "pull", "--rebase"])

    def push(self, do_pull_rebase=False):
        if do_pull_rebase:
            self.pull_rebase()

        try:
            self.run(["git", "push"])
        except GitError:
            self.log("⚠️ Push failed. Trying git pull --rebase then push again...")
            self.pull_rebase()
            self.run(["git", "push"])


# -----------------------------
# Resume
# -----------------------------

def load_processed(path: str) -> set:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {int(x) for x in data}
    except Exception:
        return set()


def save_processed(path: str, processed: set):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(processed), indent=2), encoding="utf-8")
    tmp.replace(p)


# -----------------------------
# GUI
# -----------------------------

class ChunkyGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1520x930")
        self.minsize(1180, 760)

        self.bg = "#1b1f27"
        self.panel = "#242a34"
        self.panel2 = "#2d3541"
        self.panel3 = "#374150"
        self.input_bg = "#202631"
        self.fg = "#edf1f6"
        self.muted = "#b4bfcc"
        self.accent = "#6bb8ee"
        self.green = "#69d39b"
        self.yellow = "#f0c96b"
        self.red = "#f07b7b"
        self.border = "#465263"

        self.configure(bg=self.bg)

        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None

        self.chunks: List[Chunk] = []
        self.chunk_items: Dict[int, str] = {}

        self.start_time = 0.0
        self.processed_chunks_count = 0
        self.processed_files_count = 0
        self.total_files_count = 0

        self._style()
        self._build()
        self.after(80, self._drain_queues)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=self.bg, foreground=self.fg, fieldbackground=self.input_bg, bordercolor=self.border)
        style.configure("TFrame", background=self.bg)
        style.configure("Card.TFrame", background=self.panel)
        style.configure("TLabel", background=self.bg, foreground=self.fg, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=self.bg, foreground=self.muted, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=self.panel, foreground=self.fg, font=("Segoe UI", 10))
        style.configure("CardMuted.TLabel", background=self.panel, foreground=self.muted, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.bg, foreground=self.fg, font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", background=self.bg, foreground=self.muted, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=self.panel, foreground=self.fg, font=("Segoe UI Semibold", 11))
        style.configure("MetricValue.TLabel", background=self.panel, foreground=self.fg, font=("Segoe UI Semibold", 15))
        style.configure("MetricName.TLabel", background=self.panel, foreground=self.muted, font=("Segoe UI", 8))
        style.configure("TButton", background=self.panel3, foreground=self.fg, padding=(14, 8), borderwidth=0, focusthickness=0)
        style.configure("Primary.TButton", background=self.accent, foreground="#16202a", padding=(18, 9), borderwidth=0, font=("Segoe UI Semibold", 10))
        style.configure("Danger.TButton", background="#68414a", foreground=self.fg, padding=(14, 8), borderwidth=0)
        style.configure("Ghost.TButton", background=self.panel2, foreground=self.fg, padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", "#434e60"), ("disabled", "#303743")], foreground=[("disabled", "#8d98a8")])
        style.map("Primary.TButton", background=[("active", "#86c7f3"), ("disabled", "#466072")], foreground=[("disabled", "#a9bbc8")])
        style.map("Danger.TButton", background=[("active", "#79505a"), ("disabled", "#463941")])
        style.configure("TCheckbutton", background=self.panel, foreground=self.fg, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", self.panel)], foreground=[("disabled", "#687384")])
        style.configure("TSpinbox", fieldbackground=self.input_bg, background=self.panel3, foreground=self.fg, arrowsize=14)
        style.configure("Horizontal.TProgressbar", background=self.accent, troughcolor=self.panel3, bordercolor=self.panel3, lightcolor=self.accent, darkcolor=self.accent)
        style.configure("Treeview", background=self.input_bg, foreground=self.fg, fieldbackground=self.input_bg, rowheight=30, bordercolor=self.border, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=self.panel2, foreground=self.fg, relief="flat", font=("Segoe UI Semibold", 10))
        style.map("Treeview", background=[("selected", "#3c5870")], foreground=[("selected", "#ffffff")])

    def _build_legacy(self):
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        # Inputs
        input_frame = ttk.LabelFrame(root, text="Paths", style="Panel.TLabelframe")
        input_frame.pack(fill="x", pady=(0, 8))

        self.log_var = tk.StringVar()
        self.repo_var = tk.StringVar()
        self.resume_var = tk.StringVar()

        self._path_row(input_frame, 0, "Chunk Log File:", self.log_var, self.browse_log)
        self._path_row(input_frame, 1, "Git Repository:", self.repo_var, self.browse_repo)
        self._path_row(input_frame, 2, "Resume File:", self.resume_var, self.browse_resume)

        # Settings
        settings = ttk.LabelFrame(root, text="Speed / Safety Settings", style="Panel.TLabelframe")
        settings.pack(fill="x", pady=(0, 8))

        self.push_enabled = tk.BooleanVar(value=True)
        self.pull_before_push = tk.BooleanVar(value=False)
        self.auto_skip_missing = tk.BooleanVar(value=False)
        self.commit_group_size = tk.IntVar(value=1)
        self.push_every_commits = tk.IntVar(value=1)
        self.max_log_lines = tk.IntVar(value=1500)

        ttk.Checkbutton(settings, text="Push enabled", variable=self.push_enabled).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Checkbutton(settings, text="Pull --rebase before push", variable=self.pull_before_push).grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ttk.Checkbutton(settings, text="Skip missing files", variable=self.auto_skip_missing).grid(row=0, column=2, padx=8, pady=8, sticky="w")

        ttk.Label(settings, text="Commit group size:").grid(row=0, column=3, padx=(20, 4), pady=8)
        ttk.Spinbox(settings, from_=1, to=5000, textvariable=self.commit_group_size, width=8).grid(row=0, column=4, padx=4, pady=8)

        ttk.Label(settings, text="Push every commits:").grid(row=0, column=5, padx=(20, 4), pady=8)
        ttk.Spinbox(settings, from_=1, to=5000, textvariable=self.push_every_commits, width=8).grid(row=0, column=6, padx=4, pady=8)

        ttk.Label(settings, text="Log lines:").grid(row=0, column=7, padx=(20, 4), pady=8)
        ttk.Spinbox(settings, from_=200, to=10000, textvariable=self.max_log_lines, width=8).grid(row=0, column=8, padx=4, pady=8)

        ttk.Label(settings, text="Default: one chunk per commit, push after every commit", foreground=self.muted).grid(
            row=0, column=9, padx=18, pady=8, sticky="w"
        )

        # Buttons
        btns = ttk.Frame(root)
        btns.pack(fill="x", pady=(0, 8))

        self.load_btn = ttk.Button(btns, text="Load / Preview Chunks", command=self.load_chunks)
        self.start_btn = ttk.Button(btns, text="Start Processing", command=self.start_processing)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_processing, state="disabled")
        self.clear_btn = ttk.Button(btns, text="Clear Log", command=self.clear_log)

        self.load_btn.pack(side="left", padx=(0, 8))
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.clear_btn.pack(side="left", padx=(0, 8))

        # Progress
        prog = ttk.Frame(root)
        prog.pack(fill="x", pady=(0, 8))

        self.overall_progress = ttk.Progressbar(prog, orient="horizontal", mode="determinate")
        self.overall_progress.pack(fill="x", expand=True, side="left", padx=(0, 12))

        self.summary_label = ttk.Label(prog, text="Finished")
        self.summary_label.pack(side="left", padx=(0, 20))

        self.current_label = ttk.Label(prog, text="Current: -")
        self.current_label.pack(side="left", padx=(0, 20))

        self.speed_label = ttk.Label(prog, text="Speed: -")
        self.speed_label.pack(side="left")

        chunk_prog = ttk.Frame(root)
        chunk_prog.pack(fill="x", pady=(0, 8))

        ttk.Label(chunk_prog, text="Current chunk files:").pack(side="left", padx=(0, 8))
        self.chunk_progress = ttk.Progressbar(chunk_prog, orient="horizontal", mode="determinate", length=360)
        self.chunk_progress.pack(side="left", padx=(0, 12))
        self.chunk_progress_label = ttk.Label(chunk_prog, text="0 / 0")
        self.chunk_progress_label.pack(side="left")

        # Split main area
        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True)

        # Tree
        tree_frame = ttk.LabelFrame(paned, text="Per-Chunk Status", style="Panel.TLabelframe")
        paned.add(tree_frame, weight=3)

        cols = ("chunk", "files", "size", "status", "note")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("chunk", text="Chunk")
        self.tree.heading("files", text="Files")
        self.tree.heading("size", text="Size MB")
        self.tree.heading("status", text="Status")
        self.tree.heading("note", text="Note")

        self.tree.column("chunk", width=80, anchor="center")
        self.tree.column("files", width=80, anchor="center")
        self.tree.column("size", width=100, anchor="center")
        self.tree.column("status", width=140, anchor="center")
        self.tree.column("note", width=900, anchor="w")

        self.tree.tag_configure("Pending", foreground=self.muted)
        self.tree.tag_configure("Processing", foreground=self.yellow)
        self.tree.tag_configure("Committed", foreground=self.accent)
        self.tree.tag_configure("Pushed", foreground=self.green)
        self.tree.tag_configure("Skipped", foreground=self.yellow)
        self.tree.tag_configure("Failed", foreground=self.red)

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")

        # Log
        log_frame = ttk.LabelFrame(paned, text="Live Log", style="Panel.TLabelframe")
        paned.add(log_frame, weight=2)

        self.log_text = tk.Text(
            log_frame,
            bg="#0d0d0d",
            fg="#dcdcdc",
            insertbackground="#ffffff",
            relief="flat",
            wrap="none",
            font=("Consolas", 10),
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")

        self.log("👋 Select your chunk log and repo, load chunks, then click Start Processing.")

    def _path_row_legacy(self, parent, row, label, var, command):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
        entry = tk.Entry(parent, textvariable=var, bg=self.panel, fg=self.fg, insertbackground=self.fg, relief="flat")
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=2, padx=8, pady=5)
        parent.columnconfigure(1, weight=1)

    def _build(self):
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=18, pady=16)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 16))

        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="Git Chunky Processor", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="Batch commits for large repositories with resumable progress and safer Git retries.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.clear_btn = ttk.Button(header, text="Clear Log", command=self.clear_log, style="Ghost.TButton")
        self.stop_btn = ttk.Button(header, text="Stop", command=self.stop_processing, state="disabled", style="Danger.TButton")
        self.start_btn = ttk.Button(header, text="Start", command=self.start_processing, style="Primary.TButton")
        self.load_btn = ttk.Button(header, text="Load Chunks", command=self.load_chunks, style="Ghost.TButton")
        self.clear_btn.pack(side="right", padx=(8, 0))
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.start_btn.pack(side="right", padx=(8, 0))
        self.load_btn.pack(side="right", padx=(8, 0))

        top = ttk.Frame(root)
        top.pack(fill="x", pady=(0, 14))
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)

        input_frame = self._section(top, "Paths")
        input_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        self.log_var = tk.StringVar()
        self.repo_var = tk.StringVar()
        self.resume_var = tk.StringVar()

        self._path_row(input_frame, 0, "Chunk log", self.log_var, self.browse_log)
        self._path_row(input_frame, 1, "Repository", self.repo_var, self.browse_repo)
        self._path_row(input_frame, 2, "Resume JSON", self.resume_var, self.browse_resume)

        settings = self._section(top, "Processing")
        settings.grid(row=0, column=1, sticky="nsew")

        self.push_enabled = tk.BooleanVar(value=True)
        self.pull_before_push = tk.BooleanVar(value=False)
        self.auto_skip_missing = tk.BooleanVar(value=False)
        self.commit_group_size = tk.IntVar(value=1)
        self.push_every_commits = tk.IntVar(value=1)
        self.max_log_lines = tk.IntVar(value=1500)

        checks = ttk.Frame(settings, style="Card.TFrame")
        checks.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 12))
        ttk.Checkbutton(checks, text="Push enabled", variable=self.push_enabled).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(checks, text="Pull rebase", variable=self.pull_before_push).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(checks, text="Skip missing", variable=self.auto_skip_missing).pack(side="left")

        self._setting_spin(settings, 2, 0, "Chunks per commit", self.commit_group_size, 1, 5000)
        self._setting_spin(settings, 2, 2, "Push every commits", self.push_every_commits, 1, 5000)
        self._setting_spin(settings, 3, 0, "Log lines", self.max_log_lines, 200, 10000)
        ttk.Label(settings, text="Current default pushes each chunk before moving to the next.", style="CardMuted.TLabel").grid(
            row=3, column=2, columnspan=2, sticky="w", padx=14, pady=8
        )

        metrics = ttk.Frame(root)
        metrics.pack(fill="x", pady=(0, 14))
        for i in range(4):
            metrics.columnconfigure(i, weight=1)
        self.summary_label = self._metric_card(metrics, 0, "Chunks", "Ready")
        self.current_label = self._metric_card(metrics, 1, "Current", "-")
        self.pushed_metric_label = self._metric_card(metrics, 2, "Pushed", "0 pushed / 0 remaining")
        self.speed_label = self._metric_card(metrics, 3, "Speed", "-")

        progress_panel = self._section(root, "Progress")
        progress_panel.pack(fill="x", pady=(0, 14))
        ttk.Label(progress_panel, text="Overall processed", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", padx=14, pady=(0, 5))
        ttk.Label(progress_panel, text="Pushed to remote", style="CardMuted.TLabel").grid(row=1, column=1, sticky="w", padx=14, pady=(0, 5))
        ttk.Label(progress_panel, text="Current chunk files", style="CardMuted.TLabel").grid(row=1, column=2, sticky="w", padx=14, pady=(0, 5))
        self.overall_progress = ttk.Progressbar(progress_panel, orient="horizontal", mode="determinate")
        self.overall_progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        self.pushed_progress = ttk.Progressbar(progress_panel, orient="horizontal", mode="determinate")
        self.pushed_progress.grid(row=2, column=1, sticky="ew", padx=14, pady=(0, 12))
        self.chunk_progress = ttk.Progressbar(progress_panel, orient="horizontal", mode="determinate")
        self.chunk_progress.grid(row=2, column=2, sticky="ew", padx=14, pady=(0, 12))
        self.chunk_progress_label = ttk.Label(progress_panel, text="0 / 0", style="CardMuted.TLabel")
        self.chunk_progress_label.grid(row=2, column=3, sticky="w", padx=(0, 14), pady=(0, 12))
        self.pushed_progress_label = ttk.Label(progress_panel, text="0 / 0", style="CardMuted.TLabel")
        self.pushed_progress_label.grid(row=3, column=1, sticky="w", padx=14, pady=(0, 12))
        progress_panel.columnconfigure(0, weight=2)
        progress_panel.columnconfigure(1, weight=1)
        progress_panel.columnconfigure(2, weight=1)

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True)

        tree_frame = self._section(paned, "Chunk Queue")
        paned.add(tree_frame, weight=3)

        cols = ("chunk", "files", "size", "status", "note")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("chunk", text="Chunk")
        self.tree.heading("files", text="Files")
        self.tree.heading("size", text="Size MB")
        self.tree.heading("status", text="Status")
        self.tree.heading("note", text="Note")

        self.tree.column("chunk", width=90, anchor="center", stretch=False)
        self.tree.column("files", width=90, anchor="center", stretch=False)
        self.tree.column("size", width=110, anchor="center", stretch=False)
        self.tree.column("status", width=150, anchor="center", stretch=False)
        self.tree.column("note", width=900, anchor="w")

        self.tree.tag_configure("Pending", foreground=self.muted)
        self.tree.tag_configure("Processing", foreground=self.yellow)
        self.tree.tag_configure("Committed", foreground=self.accent)
        self.tree.tag_configure("Pushed", foreground=self.green)
        self.tree.tag_configure("Skipped", foreground=self.yellow)
        self.tree.tag_configure("Failed", foreground=self.red)

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=(0, 14))
        ysb.grid(row=1, column=1, sticky="ns", padx=(0, 14), pady=(0, 14))
        tree_frame.rowconfigure(1, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        log_frame = self._section(paned, "Live Log")
        paned.add(log_frame, weight=2)

        self.log_text = tk.Text(
            log_frame,
            bg=self.input_bg,
            fg="#dbe4ef",
            insertbackground=self.fg,
            relief="flat",
            wrap="none",
            font=("Cascadia Mono", 10),
            padx=12,
            pady=10,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.border,
            highlightcolor=self.accent,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=(0, 14))
        log_scroll.grid(row=1, column=1, sticky="ns", padx=(0, 14), pady=(0, 14))
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log("Select your chunk log and repo, load chunks, then click Start.")

    def _section(self, parent, title: str):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(0, 12, 0, 0))
        ttk.Label(frame, text=title, style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=14, pady=(0, 10))
        frame.columnconfigure(0, weight=1)
        return frame

    def _path_row(self, parent, row, label, var, command):
        ttk.Label(parent, text=label, style="CardMuted.TLabel").grid(row=row + 1, column=0, sticky="w", padx=(14, 10), pady=6)
        entry = tk.Entry(
            parent,
            textvariable=var,
            bg=self.input_bg,
            fg=self.fg,
            insertbackground=self.fg,
            relief="flat",
            font=("Segoe UI", 10),
            highlightthickness=1,
            highlightbackground=self.border,
            highlightcolor=self.accent,
        )
        entry.grid(row=row + 1, column=1, sticky="ew", padx=(0, 10), pady=6, ipady=7)
        ttk.Button(parent, text="Browse", command=command, style="Ghost.TButton").grid(row=row + 1, column=2, padx=(0, 14), pady=6)
        parent.columnconfigure(1, weight=1)

    def _setting_spin(self, parent, row, col, label, var, minimum, maximum):
        ttk.Label(parent, text=label, style="CardMuted.TLabel").grid(row=row, column=col, sticky="w", padx=(14, 8), pady=8)
        ttk.Spinbox(parent, from_=minimum, to=maximum, textvariable=var, width=8).grid(row=row, column=col + 1, sticky="w", padx=(0, 14), pady=8)

    def _metric_card(self, parent, col, name, value):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 12, 0))
        ttk.Label(card, text=name.upper(), style="CardMuted.TLabel").pack(anchor="w")
        label = ttk.Label(card, text=value, style="MetricValue.TLabel")
        label.pack(anchor="w", pady=(3, 0))
        return label

    def browse_log(self):
        p = filedialog.askopenfilename(title="Select chunk log", filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")])
        if p:
            self.log_var.set(p)
            repo_guess = self.extract_repo_from_log(p)
            if repo_guess and not self.repo_var.get():
                self.repo_var.set(repo_guess)
            if not self.resume_var.get():
                base = Path(repo_guess if repo_guess else Path(p).parent)
                self.resume_var.set(str(base / "chunky_logs" / "processed_chunks.json"))

    def browse_repo(self):
        p = filedialog.askdirectory(title="Select Git repository")
        if p:
            self.repo_var.set(p)
            if not self.resume_var.get():
                self.resume_var.set(str(Path(p) / "chunky_logs" / "processed_chunks.json"))

    def browse_resume(self):
        p = filedialog.asksaveasfilename(title="Select resume JSON", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.resume_var.set(p)

    @staticmethod
    def extract_repo_from_log(log_path: str) -> Optional[str]:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "Processing repository at:" in line:
                        return line.split("Processing repository at:", 1)[1].strip()
                    if line.startswith("Repository:"):
                        return line.split("Repository:", 1)[1].strip()
        except Exception:
            return None
        return None

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_q.put(f"[{ts}] {msg}\n")

    def clear_log(self):
        self.log_text.delete("1.0", "end")

    def load_chunks(self):
        log_file = self.log_var.get().strip()
        if not log_file or not os.path.isfile(log_file):
            messagebox.showerror("Missing log", "Select a valid chunk log file.")
            return

        t0 = time.time()
        try:
            self.chunks = parse_chunks(log_file)
        except Exception as e:
            messagebox.showerror("Parse error", str(e))
            return

        self.chunk_items.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        total_files = 0
        total_mb = 0.0

        # Insert in batches to avoid freezing for huge 50k lists.
        for ch in self.chunks:
            total_files += len(ch.files)
            total_mb += ch.size_mb
            item = self.tree.insert(
                "",
                "end",
                values=(f"#{ch.number}", len(ch.files), f"{ch.size_mb:.2f}", ch.status, ch.note),
                tags=(ch.status,),
            )
            self.chunk_items[ch.number] = item

        self.total_files_count = total_files
        self.overall_progress["maximum"] = max(1, len(self.chunks))
        self.overall_progress["value"] = 0
        self.pushed_progress["maximum"] = max(1, len(self.chunks))
        self.pushed_progress["value"] = 0
        self.pushed_progress_label.config(text=f"0 / {len(self.chunks)}")
        self.pushed_metric_label.config(text=f"0 pushed / {len(self.chunks)} remaining")
        self.update_push_progress()
        self.summary_label.config(text=f"Loaded {len(self.chunks)} chunks")
        self.log(f"🔍 Loaded {len(self.chunks)} chunks, {total_files} parsed files, {total_mb:.2f}MB header total in {time.time() - t0:.2f}s.")

    def start_processing(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        if not self.chunks:
            self.load_chunks()
            if not self.chunks:
                return

        repo = self.repo_var.get().strip()
        resume = self.resume_var.get().strip()
        if not repo:
            messagebox.showerror("Missing repo", "Select a Git repository.")
            return
        if not resume:
            messagebox.showerror("Missing resume file", "Select a resume file path.")
            return

        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.load_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        self.start_time = time.time()
        self.processed_chunks_count = 0
        self.processed_files_count = 0

        self.worker_thread = threading.Thread(target=self.worker, daemon=True)
        self.worker_thread.start()

    def stop_processing(self):
        self.stop_event.set()
        self.log("🛑 Stop requested. Current Git command will finish first.")

    def set_chunk_status(self, chunk: Chunk, status: str, note: str = ""):
        chunk.status = status
        chunk.note = note
        self.ui_q.put(("chunk", chunk.number, status, note))

    def worker(self):
        try:
            self._worker_impl()
        except Exception as e:
            self.log(f"❌ Fatal error: {e}")
        finally:
            self.ui_q.put(("done",))

    def _worker_impl(self):
        repo = self.repo_var.get().strip()
        resume_file = self.resume_var.get().strip()

        group_size = max(1, int(self.commit_group_size.get()))
        push_every = max(1, int(self.push_every_commits.get()))

        engine = GitEngine(repo, self.log)
        engine.ensure_repo()

        processed = load_processed(resume_file)
        remaining = [c for c in self.chunks if c.number not in processed]

        self.log(f"🚀 Started {APP_TITLE}")
        self.log(f"📦 Total chunks found: {len(self.chunks)}")
        self.log(f"✅ Already processed: {len(processed)}")
        self.log(f"🧩 Remaining chunks: {len(remaining)}")
        engine.remote_v()

        commits_since_push = 0
        pending_push_chunks: List[Chunk] = []

        for i in range(0, len(remaining), group_size):
            if self.stop_event.is_set():
                self.log("🛑 Stopped by user.")
                break

            group = remaining[i:i + group_size]
            group_label = f"#{group[0].number}-#{group[-1].number}" if len(group) > 1 else f"#{group[0].number}"
            self.log("\n" + "=" * 90)
            self.log(f"🚀 Processing chunk group {group_label} ({len(group)} chunks)")

            group_files: List[str] = []
            group_file_count = 0
            missing_all: List[str] = []

            for ch in group:
                self.set_chunk_status(ch, "Processing", "Queued in current commit group")
                self.ui_q.put(("current", ch.number, 0, max(1, len(ch.files))))

                valid, missing = engine.valid_files(ch.files)
                missing_all.extend(missing)

                if missing and not self.auto_skip_missing.get():
                    preview = "\n".join(missing[:20])
                    raise GitError(
                        f"Chunk #{ch.number} has {len(missing)} missing files. Enable 'Skip missing files' or fix files.\n{preview}"
                    )

                group_files.extend(valid)
                group_file_count += len(valid)

                self.ui_q.put(("current", ch.number, len(ch.files), max(1, len(ch.files))))

            # Remove duplicates while preserving order.
            seen = set()
            unique_files = []
            for f in group_files:
                if f not in seen:
                    seen.add(f)
                    unique_files.append(f)

            if missing_all:
                self.log(f"⚠️ Missing files skipped in group {group_label}: {len(missing_all)}")

            if not unique_files:
                for ch in group:
                    processed.add(ch.number)
                    self.set_chunk_status(ch, "Skipped", "No valid files")
                save_processed(resume_file, processed)
                continue

            self.log(f"📌 Adding {len(unique_files)} unique files for group {group_label}")
            engine.add_files_fast(unique_files)

            msg = f"Chunks {group_label} - {len(group)} chunks, {group_file_count} files"
            committed = engine.commit(msg)

            if committed:
                commits_since_push += 1
                for ch in group:
                    processed.add(ch.number)
                    self.set_chunk_status(ch, "Committed", "Committed locally")
                    self.processed_chunks_count += 1
                    self.processed_files_count += len(ch.files)
                    pending_push_chunks.append(ch)

                save_processed(resume_file, processed)
                self.log(f"✅ Commit saved for group {group_label}")

                if self.push_enabled.get() and commits_since_push >= push_every:
                    self.log(f"⬆️ Pushing after {commits_since_push} commits...")
                    engine.push(do_pull_rebase=self.pull_before_push.get())
                    commits_since_push = 0
                    for ch in pending_push_chunks:
                        self.set_chunk_status(ch, "Pushed", "Pushed to remote")
                    pending_push_chunks.clear()
                    self.log("✅ Push complete")
            else:
                for ch in group:
                    processed.add(ch.number)
                    self.set_chunk_status(ch, "Skipped", "No changes staged")
                save_processed(resume_file, processed)

            self.update_speed_labels()

        if self.push_enabled.get() and commits_since_push > 0 and not self.stop_event.is_set():
            self.log(f"⬆️ Final push for remaining {commits_since_push} commits...")
            engine.push(do_pull_rebase=self.pull_before_push.get())
            self.log("✅ Final push complete")

        self.log("\n🏁 Processing finished.")

        if self.push_enabled.get() and not self.stop_event.is_set() and pending_push_chunks:
            for ch in pending_push_chunks:
                self.set_chunk_status(ch, "Pushed", "Pushed to remote")
            pending_push_chunks.clear()

    def update_speed_labels(self):
        elapsed = max(0.001, time.time() - self.start_time)
        cps = self.processed_chunks_count / elapsed
        fps = self.processed_files_count / elapsed
        remaining = max(0, len(self.chunks) - self.processed_chunks_count)
        eta = remaining / cps if cps > 0 else 0
        self.ui_q.put(("speed", cps, fps, eta))

    def update_push_progress(self):
        total = len(self.chunks)
        pushed = sum(1 for c in self.chunks if c.status == "Pushed")
        remaining = max(0, total - pushed)
        self.pushed_progress["maximum"] = max(1, total)
        self.pushed_progress["value"] = pushed
        self.pushed_progress_label.config(text=f"{pushed} / {total}")
        self.pushed_metric_label.config(text=f"{pushed} pushed / {remaining} remaining")

    @staticmethod
    def fmt_eta(seconds: float) -> str:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _drain_queues(self):
        # Logs
        log_count = 0
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", msg)
            log_count += 1

        if log_count:
            self.log_text.see("end")
            max_lines = int(self.max_log_lines.get())
            current_lines = int(self.log_text.index("end-1c").split(".")[0])
            if current_lines > max_lines:
                self.log_text.delete("1.0", f"{current_lines - max_lines}.0")

        # UI events
        while True:
            try:
                ev = self.ui_q.get_nowait()
            except queue.Empty:
                break

            kind = ev[0]

            if kind == "chunk":
                _, num, status, note = ev
                item = self.chunk_items.get(num)
                if item:
                    vals = list(self.tree.item(item, "values"))
                    vals[3] = status
                    vals[4] = note
                    self.tree.item(item, values=vals, tags=(status,))
                    if status == "Processing":
                        self.tree.see(item)

                done = sum(1 for c in self.chunks if c.status in ("Committed", "Pushed", "Skipped"))
                self.overall_progress["value"] = done
                self.summary_label.config(text=f"{done} / {len(self.chunks)} chunks")
                self.update_push_progress()

            elif kind == "current":
                _, num, current, total = ev
                self.current_label.config(text=f"Current: #{num}")
                self.chunk_progress["maximum"] = max(1, total)
                self.chunk_progress["value"] = current
                self.chunk_progress_label.config(text=f"{current} / {total}")

            elif kind == "speed":
                _, cps, fps, eta = ev
                self.speed_label.config(text=f"Speed: {cps:.2f} chunks/s | {fps:.1f} files/s | ETA {self.fmt_eta(eta)}")

            elif kind == "done":
                self.start_btn.config(state="normal")
                self.load_btn.config(state="normal")
                self.stop_btn.config(state="disabled")
                self.summary_label.config(text="Finished")
                self.update_push_progress()

        self.after(80, self._drain_queues)


def main():
    app = ChunkyGUI()
    app.mainloop()


if __name__ == "__main__":
    main()

