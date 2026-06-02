#!/usr/bin/env python3
"""
Git Chunky Processor - GUI Fixed Version 2

What this does:
- Reads a chunk log like:
    Chunk #1 (25 files, 120.5MB):
    - Content/File A.uasset
    - Content/File B.umap
- Adds files chunk-by-chunk
- Commits each chunk
- Pushes after each chunk
- Saves processed chunk numbers so you can resume safely

Important fix from the older version:
- Git work is intentionally processed sequentially, not in parallel.
  Parallel git push/commit operations can cause rejected pushes and ordering problems.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, List, Optional, Set, Tuple


# -----------------------------
# Data model
# -----------------------------

@dataclass
class Chunk:
    number: int
    file_count: int
    size_mb: float
    files: List[str] = field(default_factory=list)


# -----------------------------
# Core helpers
# -----------------------------

CHUNK_HEADER_RE = re.compile(r"^Chunk\s+#(\d+)\s+\((\d+)\s+files?,\s+([\d.]+)\s*MB\):\s*$", re.IGNORECASE)
FILE_LINE_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
# Matches the size suffix produced by the chunk report, e.g. " (13.23MB)".
# Without stripping this, the GUI tries to add paths like
# "Content\Foo.uasset (13.23MB)", which obviously do not exist.
FILE_SIZE_SUFFIX_RE = re.compile(r"\s+\([\d.]+\s*MB\)\s*$", re.IGNORECASE)


def parse_chunks(log_file_path: str | Path) -> List[Chunk]:
    """Parse chunk definitions from a log file."""
    log_path = Path(log_file_path)
    chunks: List[Chunk] = []
    current: Optional[Chunk] = None

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            header = CHUNK_HEADER_RE.match(line.strip())
            if header:
                if current:
                    chunks.append(current)
                current = Chunk(
                    number=int(header.group(1)),
                    file_count=int(header.group(2)),
                    size_mb=float(header.group(3)),
                )
                continue

            file_match = FILE_LINE_RE.match(line)
            if current and file_match:
                # Keep the full path after "- ", including spaces in filenames,
                # but strip the trailing size text from the report:
                #   - Content\Asset.uasset (13.23MB)
                # becomes:
                #   Content\Asset.uasset
                parsed_path = file_match.group(1).strip().strip('"')
                parsed_path = FILE_SIZE_SUFFIX_RE.sub("", parsed_path).strip().strip('"')
                if parsed_path and not parsed_path.startswith("..."):
                    current.files.append(parsed_path)

    if current:
        chunks.append(current)

    return chunks


def run_git(
    repo_path: str | Path,
    args: List[str],
    log: Callable[[str], None],
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command inside repo_path without using os.chdir()."""
    cmd = ["git", *args]
    log(f"$ {' '.join(cmd)}")

    completed = subprocess.run(
        cmd,
        cwd=str(repo_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )

    if completed.stdout.strip():
        log(completed.stdout.strip())
    if completed.stderr.strip():
        log(completed.stderr.strip())

    if check and completed.returncode != 0:
        raise RuntimeError(f"Command failed with code {completed.returncode}: {' '.join(cmd)}")

    return completed


def validate_repo(repo_path: str | Path) -> Tuple[bool, str]:
    repo = Path(repo_path)
    if not repo.exists() or not repo.is_dir():
        return False, "Repository folder does not exist."
    if not (repo / ".git").exists():
        return False, "Selected folder is not a Git repository."
    return True, "OK"


def load_processed_chunks(processed_file: str | Path) -> Set[int]:
    path = Path(processed_file)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(x) for x in data}
    except Exception:
        return set()


def save_processed_chunks(processed_file: str | Path, processed: Set[int]) -> None:
    path = Path(processed_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(processed), indent=2), encoding="utf-8")


def normalize_file_for_git(repo_path: str | Path, file_path: str) -> Optional[str]:
    """Return a repo-relative path suitable for git add, or None if missing.

    The chunk report usually stores Windows-style relative paths such as:
        Content\Characters\Foo.uasset

    This function normalises those paths safely. It also strips any accidental
    trailing size suffix as a defensive fallback.
    """
    repo = Path(repo_path).resolve()

    cleaned = FILE_SIZE_SUFFIX_RE.sub("", str(file_path).strip().strip('"')).strip()
    if not cleaned or cleaned.startswith("..."):
        return None

    # Treat backslash-only paths from the log as repo-relative paths.
    # On non-Windows Python, Path("Content\Foo.uasset") is one filename,
    # so we split it with PureWindowsPath first. On Windows this also works.
    win_candidate = PureWindowsPath(cleaned)

    if win_candidate.is_absolute():
        candidate = Path(cleaned)
    else:
        candidate = repo.joinpath(*win_candidate.parts)

    try:
        abs_candidate = candidate.resolve()
    except OSError:
        return None

    if not abs_candidate.exists():
        return None

    try:
        rel = abs_candidate.relative_to(repo)
    except ValueError:
        # File exists but is outside the selected repo.
        return None

    return rel.as_posix()


def staged_has_changes(repo_path: str | Path, log: Callable[[str], None]) -> bool:
    result = run_git(repo_path, ["diff", "--cached", "--quiet"], log, check=False)
    return result.returncode != 0


def working_tree_has_unstaged_changes(repo_path: str | Path, log: Callable[[str], None]) -> bool:
    result = run_git(repo_path, ["diff", "--quiet"], log, check=False)
    return result.returncode != 0


def ensure_remote_is_reasonable(repo_path: str | Path, log: Callable[[str], None]) -> None:
    """Warn only. This avoids blocking local-only repos."""
    result = run_git(repo_path, ["remote", "-v"], log, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        log("⚠️ No Git remote found. Commit will work, but push will fail unless a remote is configured.")


def add_commit_push_chunk(
    chunk: Chunk,
    repo_path: str | Path,
    processed_file: str | Path,
    log: Callable[[str], None],
    push_after_commit: bool = True,
    pull_rebase_before_push: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    """Process one chunk in the selected repository."""
    if stop_event and stop_event.is_set():
        log("⏹️ Stopped before processing next chunk.")
        return False

    log("\n" + "=" * 90)
    log(f"🚀 Processing Chunk #{chunk.number} ({chunk.file_count} files, {chunk.size_mb}MB)")

    valid_files: List[str] = []
    missing_files: List[str] = []

    for file_path in chunk.files:
        rel = normalize_file_for_git(repo_path, file_path)
        if rel is None:
            missing_files.append(file_path)
        else:
            valid_files.append(rel)

    if missing_files:
        log(f"⚠️ Missing/outside-repo files skipped: {len(missing_files)}")
        for item in missing_files[:25]:
            log(f"   - {item}")
        if len(missing_files) > 25:
            log(f"   ... and {len(missing_files) - 25} more")

    if not valid_files:
        log(f"❌ Chunk #{chunk.number} has no valid files to add. Skipping.")
        return False

    # Add files in smaller batches to avoid very long command lines on Windows.
    batch_size = 100
    for i in range(0, len(valid_files), batch_size):
        batch = valid_files[i:i + batch_size]
        run_git(repo_path, ["add", "--", *batch], log)
        log(f"✅ Added batch {i // batch_size + 1}: {len(batch)} files")

    if not staged_has_changes(repo_path, log):
        log(f"⚠️ Chunk #{chunk.number}: nothing changed after staging. Marking as processed.")
        processed = load_processed_chunks(processed_file)
        processed.add(chunk.number)
        save_processed_chunks(processed_file, processed)
        return True

    commit_message = f"Chunk #{chunk.number} - {len(valid_files)} files"
    run_git(repo_path, ["commit", "-m", commit_message], log)
    log(f"✅ Committed Chunk #{chunk.number}")

    if push_after_commit:
        if pull_rebase_before_push:
            log("🔄 Pulling with rebase before push...")
            run_git(repo_path, ["pull", "--rebase"], log, check=False)

        push_result = run_git(repo_path, ["push"], log, check=False)
        if push_result.returncode != 0:
            log("⚠️ Initial push failed. Trying: git pull --rebase, then git push again...")
            rebase_result = run_git(repo_path, ["pull", "--rebase"], log, check=False)
            if rebase_result.returncode != 0:
                log("❌ Pull --rebase failed. Please resolve Git conflicts manually, then run again.")
                return False

            push_retry = run_git(repo_path, ["push"], log, check=False)
            if push_retry.returncode != 0:
                log("❌ Push failed again. Please check remote permissions, branch, or Git LFS limits.")
                return False

        log(f"✅ Pushed Chunk #{chunk.number}")
    else:
        log("ℹ️ Push disabled. Commit created locally only.")

    processed = load_processed_chunks(processed_file)
    processed.add(chunk.number)
    save_processed_chunks(processed_file, processed)
    log(f"🎉 Chunk #{chunk.number} completed and saved to resume file.")
    return True


def process_all_chunks(
    chunks: Iterable[Chunk],
    repo_path: str | Path,
    processed_file: str | Path,
    log: Callable[[str], None],
    progress: Callable[[int, int], None],
    push_after_commit: bool,
    pull_rebase_before_push: bool,
    stop_event: threading.Event,
) -> None:
    chunks_list = list(chunks)
    processed = load_processed_chunks(processed_file)
    todo = [c for c in chunks_list if c.number not in processed]

    log(f"📦 Total chunks found: {len(chunks_list)}")
    log(f"✅ Already processed: {len(processed)}")
    log(f"🧩 Remaining chunks: {len(todo)}")

    if not todo:
        progress(len(chunks_list), len(chunks_list))
        log("🎉 Nothing to do. All chunks are already processed.")
        return

    ensure_remote_is_reasonable(repo_path, log)

    done_count = len(chunks_list) - len(todo)
    progress(done_count, len(chunks_list))

    for chunk in todo:
        if stop_event.is_set():
            log("⏹️ Processing stopped by user.")
            break

        ok = add_commit_push_chunk(
            chunk,
            repo_path,
            processed_file,
            log,
            push_after_commit=push_after_commit,
            pull_rebase_before_push=pull_rebase_before_push,
            stop_event=stop_event,
        )

        if ok:
            done_count += 1
            progress(done_count, len(chunks_list))
        else:
            log(f"❌ Stopped at Chunk #{chunk.number}. Fix the issue and run again to resume.")
            break

    log("\n🏁 Processing finished.")


# -----------------------------
# GUI
# -----------------------------

class ChunkyGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Git Chunky Processor - Safe GUI v2")
        self.geometry("980x700")
        self.minsize(860, 600)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.progress_queue: queue.Queue[Tuple[int, int]] = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.log_file_var = tk.StringVar()
        self.repo_path_var = tk.StringVar()
        self.processed_file_var = tk.StringVar(value="")
        self.push_var = tk.BooleanVar(value=True)
        self.pull_rebase_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self.after(100, self._drain_queues)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Chunk Log File:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.log_file_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self.browse_log).grid(row=0, column=2)

        ttk.Label(top, text="Git Repository:").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.repo_path_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self.browse_repo).grid(row=1, column=2)

        ttk.Label(top, text="Resume File:").grid(row=2, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.processed_file_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self.browse_processed).grid(row=2, column=2)

        top.columnconfigure(1, weight=1)

        options = ttk.Frame(self)
        options.pack(fill="x", **pad)
        ttk.Checkbutton(options, text="Push after each commit", variable=self.push_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(options, text="Pull --rebase before push", variable=self.pull_rebase_var).pack(side="left")

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="Start Processing", command=self.start_processing)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_processing, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Preview Chunks", command=self.preview_chunks).pack(side="left")
        ttk.Button(actions, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)

        progress_frame = ttk.Frame(self)
        progress_frame.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x", expand=True, side="left")
        ttk.Label(progress_frame, textvariable=self.status_var, width=24).pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(self, text="Live Log")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, wrap="word", height=20)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self._log("👋 Select your chunk log and repo, then click Start Processing.")

    def browse_log(self) -> None:
        path = filedialog.askopenfilename(
            title="Select chunk log file",
            filetypes=[("Log/Text files", "*.log *.txt"), ("All files", "*.*")],
        )
        if path:
            self.log_file_var.set(path)
            if not self.processed_file_var.get().strip():
                self._auto_set_processed_file()

    def browse_repo(self) -> None:
        path = filedialog.askdirectory(title="Select Git repository folder")
        if path:
            self.repo_path_var.set(path)
            if not self.processed_file_var.get().strip():
                self._auto_set_processed_file()

    def browse_processed(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Select resume JSON file",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.processed_file_var.set(path)

    def _auto_set_processed_file(self) -> None:
        repo = self.repo_path_var.get().strip()
        if repo:
            logs_dir = Path(repo) / "chunky_logs"
            self.processed_file_var.set(str(logs_dir / "processed_chunks.json"))

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.update_idletasks()

    def log_from_worker(self, message: str) -> None:
        self.log_queue.put(message)

    def progress_from_worker(self, done: int, total: int) -> None:
        self.progress_queue.put((done, total))

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")

    def preview_chunks(self) -> None:
        log_file = self.log_file_var.get().strip()
        if not log_file or not Path(log_file).is_file():
            messagebox.showerror("Missing log file", "Please select a valid chunk log file first.")
            return
        try:
            chunks = parse_chunks(log_file)
            total_files = sum(len(c.files) for c in chunks)
            total_size = sum(c.size_mb for c in chunks)
            self._log(f"🔍 Preview: {len(chunks)} chunks, {total_files} parsed files, {total_size:.2f}MB total from headers.")
            for c in chunks[:10]:
                self._log(f"   Chunk #{c.number}: header says {c.file_count} files, parsed {len(c.files)} files, {c.size_mb}MB")
            if len(chunks) > 10:
                self._log(f"   ... {len(chunks) - 10} more chunks")
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc))

    def start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running", "Processing is already running.")
            return

        log_file = self.log_file_var.get().strip()
        repo_path = self.repo_path_var.get().strip()
        processed_file = self.processed_file_var.get().strip()

        if not log_file or not Path(log_file).is_file():
            messagebox.showerror("Missing log file", "Please select a valid chunk log file.")
            return

        ok, msg = validate_repo(repo_path)
        if not ok:
            messagebox.showerror("Invalid repository", msg)
            return

        if not processed_file:
            processed_file = str(Path(repo_path) / "chunky_logs" / "processed_chunks.json")
            self.processed_file_var.set(processed_file)

        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running...")
        self.progress.configure(value=0, maximum=100)

        def worker_main() -> None:
            try:
                self.log_from_worker("🚀 Started Git Chunky Processor GUI")
                chunks = parse_chunks(log_file)
                if not chunks:
                    self.log_from_worker("❌ No chunks found in the selected log file.")
                    return

                process_all_chunks(
                    chunks=chunks,
                    repo_path=repo_path,
                    processed_file=processed_file,
                    log=self.log_from_worker,
                    progress=self.progress_from_worker,
                    push_after_commit=self.push_var.get(),
                    pull_rebase_before_push=self.pull_rebase_var.get(),
                    stop_event=self.stop_event,
                )
            except Exception as exc:
                self.log_from_worker(f"❌ Error: {exc}")
            finally:
                self.log_from_worker("__WORKER_DONE__")

        self.worker = threading.Thread(target=worker_main, daemon=True)
        self.worker.start()

    def stop_processing(self) -> None:
        self.stop_event.set()
        self._log("⏹️ Stop requested. The current Git command will finish first, then processing will stop.")

    def _drain_queues(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if message == "__WORKER_DONE__":
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                self.status_var.set("Finished")
            else:
                self._log(message)

        while True:
            try:
                done, total = self.progress_queue.get_nowait()
            except queue.Empty:
                break

            self.progress.configure(maximum=max(total, 1), value=done)
            self.status_var.set(f"{done}/{total} chunks")

        self.after(100, self._drain_queues)


if __name__ == "__main__":
    app = ChunkyGUI()
    app.mainloop()
