#!/usr/bin/env python3
"""
Git Chunk Processor Dashboard
- Browser UI inputs instead of CLI inputs
- Browse buttons for log file, repo folder, processed_chunks.json
- Chunk grid with equal cells showing only chunk numbers
- Active/selected chunk highlighted
- Right-side selected chunk detail card
- Live console
- Stable UI refresh: does NOT rebuild the whole UI every tick
- Pause auto refresh button
- Auto pauses refresh while selecting/copying text
- Visualize Chunks Only button parses the log and updates the UI without git commands
- Processing modes:
    full         = git add + commit + push
    commit_only  = git add + commit only
    push_only    = git push only
    dry_run      = simulate only

Run:
    python git_chunk_processor_dashboard_stable.py
"""

import json
import os
import re
import sys
import time
import threading
import subprocess
import queue
import webbrowser
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Suppress console output when running as exe
if getattr(sys, 'frozen', False):
    import io
    # Redirect stdout and stderr to null device
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

APP_NAME = "Git Chunk Processor Dashboard"
HOST = "127.0.0.1"
PORT = 8765

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = SCRIPT_DIR / "chunk_processor_state.json"
DEFAULT_PROCESSED_PATH = SCRIPT_DIR / "processed_chunks.json"
DEFAULT_LOGS_DIR = SCRIPT_DIR / "Logs"

STATE_LOCK = threading.Lock()
PROCESS_THREAD = None
STOP_REQUESTED = False
CONSOLE_MAX_LINES = 800
ACTIVE_PROCESSES = []
ACTIVE_PROCESSES_LOCK = threading.Lock()
SERVER_INSTANCE = None

# In-memory processed-chunks cache — avoids re-reading the JSON file on every chunk
_PROCESSED_CACHE: set | None = None
_PROCESSED_CACHE_PATH: str = ""
_PROCESSED_LOCK = threading.Lock()


def now_text():
    return datetime.now().strftime("%H:%M:%S")


def clean_pasted_path(value: str) -> str:
    if value is None:
        return ""
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value.strip()


# ── Debounced state persistence ──────────────────────────────────────────────
_STATE_DIRTY = False
_STATE_DIRTY_LOCK = threading.Lock()
_LAST_SAVE_TIME = 0.0
_SAVE_INTERVAL = 0.3  # max one disk write per 0.3 s


def _flush_state():
    """Write STATE to disk. Always call with STATE_LOCK held."""
    DEFAULT_STATE_PATH.write_text(json.dumps(STATE, indent=2), encoding="utf-8")


def save_state():
    """Mark state dirty; a background flusher writes at most every _SAVE_INTERVAL seconds."""
    global _STATE_DIRTY
    with _STATE_DIRTY_LOCK:
        _STATE_DIRTY = True


def _state_flusher():
    """Background thread: flushes dirty state periodically."""
    global _STATE_DIRTY, _LAST_SAVE_TIME
    while True:
        time.sleep(0.1)
        with _STATE_DIRTY_LOCK:
            dirty = _STATE_DIRTY
        if dirty:
            now = time.monotonic()
            if now - _LAST_SAVE_TIME >= _SAVE_INTERVAL:
                with STATE_LOCK:
                    try:
                        _flush_state()
                    except Exception:
                        pass
                _LAST_SAVE_TIME = time.monotonic()
                with _STATE_DIRTY_LOCK:
                    _STATE_DIRTY = False


threading.Thread(target=_state_flusher, daemon=True).start()


def make_default_state():
    return {
        "app": APP_NAME,
        "running": False,
        "stop_requested": False,
        "started_at": None,
        "ended_at": None,
        "mode": "full",
        "log_file": "",
        "repo_path": "",
        "processed_chunks_file": str(DEFAULT_PROCESSED_PATH),
        "logs_dir": str(DEFAULT_LOGS_DIR),
        "start_chunk": "",
        "end_chunk": "",
        "pause_between_chunks": 0,
        "push_every_n": 1,
        "current_chunk": None,
        "selected_chunk": None,
        "overall_progress": 0,
        "eta": "-",
        "average_chunk_seconds": 0,
        "stats": {
            "total": 0,
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0
        },
        "chunks": [],
        "console": [],
        "last_error": "",
        "git": {
            "branch": "",
            "remote": "",
            "lfs": "",
            "status": ""
        }
    }


STATE = make_default_state()





def add_console(message, level="info"):
    line = {
        "time": now_text(),
        "level": level,
        "message": str(message)
    }
    with STATE_LOCK:
        STATE["console"].append(line)
        if len(STATE["console"]) > CONSOLE_MAX_LINES:
            STATE["console"] = STATE["console"][-CONSOLE_MAX_LINES:]
    save_state()


def update_state(**kwargs):
    with STATE_LOCK:
        for k, v in kwargs.items():
            STATE[k] = v
    save_state()


_STATE_VERSION = 0  # incremented whenever STATE changes meaningfully


def _inc_version():
    global _STATE_VERSION
    _STATE_VERSION += 1


# Patch save_state to also increment version
_orig_save_state = save_state


def save_state():
    _orig_save_state()
    _inc_version()


def get_state_copy():
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def load_processed_chunks(path) -> set:
    """Load processed chunk numbers from JSON. Returns a set of ints."""
    global _PROCESSED_CACHE, _PROCESSED_CACHE_PATH
    path_str = str(path)
    with _PROCESSED_LOCK:
        if _PROCESSED_CACHE is not None and _PROCESSED_CACHE_PATH == path_str:
            return set(_PROCESSED_CACHE)
    p = Path(path)
    if not p.exists():
        with _PROCESSED_LOCK:
            _PROCESSED_CACHE = set()
            _PROCESSED_CACHE_PATH = path_str
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        result = {int(x) for x in data}
    except Exception:
        result = set()
    with _PROCESSED_LOCK:
        _PROCESSED_CACHE = set(result)
        _PROCESSED_CACHE_PATH = path_str
    return result


def save_processed_chunk(path, chunk_number):
    """Add chunk_number to the processed set and persist atomically (no re-read)."""
    global _PROCESSED_CACHE, _PROCESSED_CACHE_PATH
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    path_str = str(p)
    with _PROCESSED_LOCK:
        if _PROCESSED_CACHE is None or _PROCESSED_CACHE_PATH != path_str:
            # First call or path changed — load from disk
            if p.exists():
                try:
                    existing = {int(x) for x in json.loads(p.read_text(encoding="utf-8"))}
                except Exception:
                    existing = set()
            else:
                existing = set()
            _PROCESSED_CACHE = existing
            _PROCESSED_CACHE_PATH = path_str
        _PROCESSED_CACHE.add(int(chunk_number))
        serialised = json.dumps(sorted(_PROCESSED_CACHE), indent=2)
    # Durable atomic write: flush the temp file before rename. This narrows the
    # crash window and the startup reconciliation repairs the remaining edge case
    # where a remote push finishes immediately before local JSON persistence.
    tmp = p.with_suffix(".json.tmp")
    last_error = None
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(serialised)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(p)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.08 * (attempt + 1))
    # Last-resort direct durable write. Surface an error only if this also fails.
    try:
        with open(p, "w", encoding="utf-8") as handle:
            handle.write(serialised)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        raise last_error


_CHUNK_RE = re.compile(r"Chunk\s+#(\d+)\s+\((\d+)\s+files?,\s+([\d.]+)MB\):", re.IGNORECASE)
_FILE_RE  = re.compile(r"^\s*-\s+(.+?)(?:\s+\(([\d.]+)MB\))?\s*$")


def parse_chunks(log_file_path):
    chunks = []
    current = None

    with open(log_file_path, "r", encoding="utf-8", errors="ignore", buffering=1 << 20) as f:
        for raw in f:
            line = raw.rstrip("\n")
            cm = _CHUNK_RE.search(line)
            if cm:
                if current:
                    chunks.append(current)
                current = {
                    "number": int(cm.group(1)),
                    "file_count": int(cm.group(2)),
                    "size_mb": float(cm.group(3)),
                    "status": "pending",
                    "stage": "pending",
                    "progress": 0,
                    "error": "",
                    "push_output": "",
                    "started_at": None,
                    "ended_at": None,
                    "duration_seconds": None,
                    "files": []
                }
                continue

            if current:
                fm = _FILE_RE.match(line)
                if fm:
                    file_path = fm.group(1).strip()
                    size_mb = float(fm.group(2)) if fm.group(2) else 0.0
                    fp = Path(file_path)
                    current["files"].append({
                        "path": file_path,
                        "name": fp.name,
                        "folder": str(fp.parent),
                        "size_mb": size_mb,
                        "status": "pending"
                    })

    if current:
        chunks.append(current)

    return chunks


def _build_chunk_index():
    """Rebuild the chunk index {number -> list_index}. Call with STATE_LOCK held."""
    STATE["_chunk_index"] = {c["number"]: i for i, c in enumerate(STATE["chunks"])}


def recalc_stats_unlocked():
    chunks = STATE["chunks"]
    total = len(chunks)
    completed = sum(1 for c in chunks if c.get("status") == "completed")
    failed = sum(1 for c in chunks if c.get("status") == "failed")
    skipped = sum(1 for c in chunks if c.get("status") == "skipped")
    processing = sum(1 for c in chunks if c.get("status") == "processing")
    pending = max(total - completed - failed - skipped - processing, 0)
    done_like = completed + skipped
    overall = int((done_like / total) * 100) if total else 0
    STATE["stats"] = {
        "total": total,
        "pending": pending,
        "processing": processing,
        "completed": completed,
        "failed": failed,
        "skipped": skipped
    }
    STATE["overall_progress"] = overall


def set_chunk_status(chunk_number, **updates):
    with STATE_LOCK:
        idx = STATE.get("_chunk_index", {}).get(chunk_number)
        if idx is not None and idx < len(STATE["chunks"]):
            STATE["chunks"][idx].update(updates)
        else:
            # Fallback linear scan (shouldn't happen after index is built)
            for c in STATE["chunks"]:
                if c["number"] == chunk_number:
                    c.update(updates)
                    break
        recalc_stats_unlocked()
    save_state()


def set_file_status(chunk_number, file_path, status):
    with STATE_LOCK:
        idx = STATE.get("_chunk_index", {}).get(chunk_number)
        if idx is not None and idx < len(STATE["chunks"]):
            chunk = STATE["chunks"][idx]
            for f in chunk["files"]:
                if f["path"] == file_path:
                    f["status"] = status
                    break
        else:
            for c in STATE["chunks"]:
                if c["number"] == chunk_number:
                    for f in c["files"]:
                        if f["path"] == file_path:
                            f["status"] = status
                            break
                    break
    save_state()


def _no_window_flags():
    """Return kwargs that suppress the console window on Windows."""
    if sys.platform.startswith("win"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_cmd(cmd, cwd=None, stream=False, input_text=None):
    add_console(f"$ {' '.join(cmd)}", "cmd")

    if stream:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **_no_window_flags()
        )
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.append(proc)
        output_lines = []
        try:
            while True:
                # Graceful stop means "finish the current Git command/chunk".
                # Do not kill an in-flight push: terminating it can leave a pushed
                # commit missing from processed_chunks.json.
                line = proc.stdout.readline()
                if line:
                    text = line.rstrip()
                    output_lines.append(text)
                    add_console(text, "git")
                if line == "" and proc.poll() is not None:
                    break
        finally:
            with ACTIVE_PROCESSES_LOCK:
                if proc in ACTIVE_PROCESSES:
                    ACTIVE_PROCESSES.remove(proc)
        return proc.returncode, "\n".join(output_lines)

    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        **_no_window_flags()
    )
    out = (result.stdout or "") + (result.stderr or "")
    if out.strip():
        for line in out.splitlines():
            add_console(line, "git")
    return result.returncode, out


def git_add_files(files, repo):
    """
    Add files to the git index efficiently.
    Uses --stdin for large batches to avoid OS argument-length limits,
    falling back to large CLI batches otherwise.
    """
    # Try stdin method first (fastest, no arg-length limit)
    null_sep = "\0".join(files) + "\0"
    code, out = run_cmd(["git", "add", "-z", "--stdin"], cwd=repo, input_text=null_sep)
    if code == 0:
        return code, out
    # Fallback: batched CLI (batch_size 500 to cut round-trips vs old 50)
    batch_size = 500
    combined_out = ""
    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        code, out = run_cmd(["git", "add", "--"] + batch, cwd=repo)
        combined_out += out
        if code != 0:
            return code, combined_out
    return 0, combined_out



_CHUNK_COMMIT_RE = re.compile(r"^Chunk\s+#(\d+)\s+-", re.IGNORECASE)


def get_repo_branch_and_remote(repo: Path):
    """Return (branch, remote). Defaults to origin when branch config is absent."""
    code, out = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    branch = out.strip() if code == 0 else ""
    if not branch or branch == "HEAD":
        return "", "origin"
    code, out = run_cmd(["git", "config", "--get", f"branch.{branch}.remote"], cwd=repo)
    remote = out.strip() if code == 0 and out.strip() else "origin"
    return branch, remote


def get_chunk_commit_map(repo: Path):
    """Map chunk number to newest matching local commit SHA."""
    code, out = run_cmd(["git", "log", "--format=%H%x09%s"], cwd=repo)
    result = {}
    if code != 0:
        return result
    for line in out.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        match = _CHUNK_COMMIT_RE.match(subject.strip())
        if match:
            result.setdefault(int(match.group(1)), sha.strip())
    return result


def get_remote_branch_sha(repo: Path, remote: str, branch: str):
    """Read the live remote branch SHA without mutating local refs."""
    if not branch:
        return ""
    code, out = run_cmd(["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"], cwd=repo)
    if code != 0 or not out.strip():
        return ""
    return out.split()[0].strip()


def is_ancestor(repo: Path, ancestor_sha: str, descendant_sha: str):
    if not ancestor_sha or not descendant_sha:
        return False
    code, _ = run_cmd(["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha], cwd=repo)
    return code == 0


def reconcile_processed_chunks(repo: Path, processed_path: str):
    """
    Repair processed_chunks.json after an interrupted dashboard shutdown.
    A chunk is repaired only when its dashboard commit exists locally and is
    confirmed reachable from the live remote branch SHA.
    """
    branch, remote = get_repo_branch_and_remote(repo)
    commit_map = get_chunk_commit_map(repo)
    remote_sha = get_remote_branch_sha(repo, remote, branch)
    # When another machine advanced the remote branch, the live SHA may not yet
    # exist in this local object database. Fetch commit objects only (no checkout).
    if remote_sha:
        code, _ = run_cmd(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=repo)
        if code != 0:
            run_cmd(["git", "fetch", "--no-tags", remote, f"refs/heads/{branch}"], cwd=repo)
    processed = load_processed_chunks(processed_path)
    repaired = []
    if not remote_sha:
        add_console("Remote reconciliation skipped: remote branch SHA could not be read.", "warning")
        return processed, commit_map, branch, remote, remote_sha, repaired
    for chunk_number, commit_sha in sorted(commit_map.items()):
        if chunk_number not in processed and is_ancestor(repo, commit_sha, remote_sha):
            save_processed_chunk(processed_path, chunk_number)
            processed.add(chunk_number)
            repaired.append(chunk_number)
    if repaired:
        joined = ", ".join(f"#{n}" for n in repaired)
        add_console(f"Recovered pushed chunks missing from processed JSON: {joined}", "success")
    return processed, commit_map, branch, remote, remote_sha, repaired


def repo_default_paths(repo_path: str):
    """Return repo-local dashboard paths and create the Logs folder."""
    repo = Path(clean_pasted_path(repo_path))
    logs_dir = repo / "Logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "repo_path": str(repo),
        "log_file": str(repo / "git_chunks.log"),
        "processed_chunks_file": str(repo / "processed_chunks.json"),
        "logs_dir": str(logs_dir),
    }


def has_staged_changes(repo: Path):
    code, _ = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo)
    return code == 1


def current_head_sha(repo: Path):
    code, out = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo)
    return out.strip() if code == 0 else ""


def push_exact_commit(repo: Path, remote: str, branch: str, commit_sha: str):
    """Push one exact commit boundary; newer local commits are not accidentally included."""
    return run_cmd(["git", "push", remote, f"{commit_sha}:refs/heads/{branch}"], cwd=repo, stream=True)


def process_worker_pipeline(config, chunks, repo: Path):
    """
    Safe two-worker pipeline:
      - this worker stages and commits chunks sequentially (Git has one shared index)
      - one push worker pushes exact commit SHAs sequentially while the next chunk commits
    """
    global STOP_REQUESTED
    branch, remote = get_repo_branch_and_remote(repo)
    if not branch:
        raise RuntimeError("Pipeline mode requires a named Git branch (not detached HEAD).")

    tasks = queue.Queue()
    push_errors = []
    sentinel = object()

    def push_worker():
        while True:
            item = tasks.get()
            try:
                if item is sentinel:
                    return
                n, commit_sha, chunk_started = item
                set_chunk_status(n, stage="pushing", progress=88)
                add_console(f"Pipeline push started for Chunk #{n} ({commit_sha[:10]}).", "info")
                code, out = push_exact_commit(repo, remote, branch, commit_sha)
                if code != 0:
                    msg = f"git push failed after Chunk #{n}."
                    push_errors.append((n, msg))
                    set_chunk_status(n, status="failed", stage="push_failed", progress=100, error=msg,
                                     push_output=out[-6000:] if out else "")
                    add_console(msg, "error")
                    continue
                duration = time.time() - chunk_started
                set_chunk_status(n, status="completed", stage="done", progress=100,
                                 push_output=out[-6000:] if out else "",
                                 ended_at=datetime.now().isoformat(timespec="seconds"),
                                 duration_seconds=round(duration, 2))
                save_processed_chunk(config["processed_chunks_file"], n)
                add_console(f"Chunk #{n} pushed and recorded atomically.", "success")
            finally:
                tasks.task_done()

    pusher = threading.Thread(target=push_worker, name="git-chunk-pusher", daemon=True)
    pusher.start()

    for chunk in chunks:
        if STOP_REQUESTED:
            add_console("Stop requested. No new chunks will be committed; queued pushes will finish.", "warning")
            break
        n = chunk["number"]
        if chunk["status"] == "skipped":
            continue
        chunk_start = time.time()
        set_chunk_status(n, status="processing", stage="checking", progress=5,
                         started_at=datetime.now().isoformat(timespec="seconds"), error="", push_output="")
        update_state(current_chunk=n, selected_chunk=n)
        add_console(f"Pipeline commit processing Chunk #{n} ({len(chunk['files'])} files, {chunk['size_mb']} MB)", "info")

        missing = [f["path"] for f in chunk["files"] if not (repo / f["path"]).exists()]
        if missing:
            msg = f"Chunk #{n} failed: {len(missing)} missing files."
            set_chunk_status(n, status="failed", stage="missing_files", progress=100, error=msg)
            add_console(msg, "error")
            continue
        for f in chunk["files"]:
            set_file_status(n, f["path"], "found")

        files = [f["path"] for f in chunk["files"]]
        set_chunk_status(n, stage="adding", progress=20)
        code, _ = git_add_files(files, repo)
        if code != 0:
            msg = f"git add failed in Chunk #{n}."
            set_chunk_status(n, status="failed", stage="add_failed", progress=100, error=msg)
            add_console(msg, "error")
            continue
        for fp in files:
            set_file_status(n, fp, "added")

        if not has_staged_changes(repo):
            # This commonly occurs after an interrupted previous run. The files
            # are already represented by HEAD, so recording the chunk is safe.
            set_chunk_status(n, status="completed", stage="already_in_head_reconciled", progress=100,
                             ended_at=datetime.now().isoformat(timespec="seconds"))
            save_processed_chunk(config["processed_chunks_file"], n)
            add_console(f"Chunk #{n} already exists in HEAD. Reconciled processed JSON.", "success")
            continue

        set_chunk_status(n, stage="committing", progress=60)
        code, out = run_cmd(["git", "commit", "-m", f"Chunk #{n} - {len(files)} files"], cwd=repo)
        if code != 0:
            msg = f"git commit failed in Chunk #{n}."
            set_chunk_status(n, status="failed", stage="commit_failed", progress=100, error=msg)
            add_console(msg, "error")
            continue
        commit_sha = current_head_sha(repo)
        if not commit_sha:
            msg = f"Could not read commit SHA after Chunk #{n}."
            set_chunk_status(n, status="failed", stage="sha_read_failed", progress=100, error=msg)
            add_console(msg, "error")
            continue
        set_chunk_status(n, stage="committed_queued_for_push", progress=78)
        add_console(f"Chunk #{n} committed and queued for push ({commit_sha[:10]}).", "success")
        tasks.put((n, commit_sha, chunk_start))

        pause_seconds = float(config.get("pause_between_chunks") or 0)
        if pause_seconds > 0 and not STOP_REQUESTED:
            time.sleep(pause_seconds)

    tasks.put(sentinel)
    tasks.join()
    pusher.join()
    update_state(current_chunk=None)
    if push_errors:
        raise RuntimeError(f"Pipeline completed with {len(push_errors)} push failure(s).")
    add_console("Pipeline queue drained successfully.", "success")

def inspect_git(repo_path):
    repo = Path(repo_path)
    info = {
        "branch": "",
        "remote": "",
        "lfs": "",
        "status": ""
    }

    code, out = run_cmd(["git", "branch", "--show-current"], cwd=repo)
    if code == 0:
        info["branch"] = out.strip()

    code, out = run_cmd(["git", "remote", "-v"], cwd=repo)
    if code == 0:
        first = out.splitlines()[0] if out.splitlines() else ""
        info["remote"] = first

    code, out = run_cmd(["git", "lfs", "version"], cwd=repo)
    info["lfs"] = "Installed" if code == 0 else "Not found"

    code, out = run_cmd(["git", "status", "--short"], cwd=repo)
    if code == 0:
        info["status"] = "Has changes" if out.strip() else "Clean"

    with STATE_LOCK:
        STATE["git"] = info
    save_state()


def validate_start_payload(payload):
    log_file = clean_pasted_path(payload.get("log_file", ""))
    repo_path = clean_pasted_path(payload.get("repo_path", ""))
    processed_chunks_file = clean_pasted_path(payload.get("processed_chunks_file", "")) or str(DEFAULT_PROCESSED_PATH)
    logs_dir = clean_pasted_path(payload.get("logs_dir", "")) or str(DEFAULT_LOGS_DIR)
    mode = payload.get("mode", "full")
    start_chunk_raw = clean_pasted_path(str(payload.get("start_chunk", "") or ""))
    end_chunk_raw = clean_pasted_path(str(payload.get("end_chunk", "") or ""))
    pause_raw = clean_pasted_path(str(payload.get("pause_between_chunks", "") or "0"))

    if mode not in {"full", "commit_only", "push_only", "dry_run", "commit_n_then_push", "pipeline"}:
        return None, "Invalid processing mode."

    start_chunk = int(start_chunk_raw) if start_chunk_raw else None
    end_chunk = int(end_chunk_raw) if end_chunk_raw else None
    pause_between_chunks = float(pause_raw) if pause_raw else 0

    push_every_n_raw = clean_pasted_path(str(payload.get("push_every_n", "") or "1"))
    try:
        push_every_n = max(1, int(push_every_n_raw))
    except (ValueError, TypeError):
        push_every_n = 1

    if start_chunk is not None and start_chunk < 1:
        return None, "Start chunk must be 1 or higher."
    if end_chunk is not None and end_chunk < 1:
        return None, "End chunk must be 1 or higher."
    if start_chunk is not None and end_chunk is not None and start_chunk > end_chunk:
        return None, "Start chunk cannot be greater than end chunk."
    if pause_between_chunks < 0:
        return None, "Pause between chunks cannot be negative."

    if mode != "push_only":
        if not log_file or not Path(log_file).is_file():
            return None, "git_chunks.log file not found."

    if not repo_path or not Path(repo_path).is_dir():
        return None, "Git repository folder not found."

    if not (Path(repo_path) / ".git").exists():
        return None, "Selected folder is not a Git repository."

    if processed_chunks_file:
        p = Path(processed_chunks_file)
        if p.suffix.lower() != ".json":
            return None, "processed_chunks file should be a .json file."
        p.parent.mkdir(parents=True, exist_ok=True)

    Path(logs_dir).mkdir(parents=True, exist_ok=True)

    return {
        "log_file": log_file,
        "repo_path": repo_path,
        "processed_chunks_file": processed_chunks_file,
        "logs_dir": logs_dir,
        "mode": mode,
        "start_chunk": start_chunk,
        "end_chunk": end_chunk,
        "pause_between_chunks": pause_between_chunks,
        "push_every_n": push_every_n
    }, ""


def format_seconds(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def process_worker(config):
    global STOP_REQUESTED

    STOP_REQUESTED = False
    completed_durations = []
    _duration_sum = 0.0  # running total for O(1) average
    committed_since_push = []  # tracks chunk numbers committed but not yet pushed (commit_n_then_push mode)

    update_state(
        running=True,
        stop_requested=False,
        started_at=datetime.now().isoformat(timespec="seconds"),
        ended_at=None,
        mode=config["mode"],
        log_file=config["log_file"],
        repo_path=config["repo_path"],
        processed_chunks_file=config["processed_chunks_file"],
        logs_dir=config["logs_dir"],
        start_chunk=config.get("start_chunk") or "",
        end_chunk=config.get("end_chunk") or "",
        pause_between_chunks=config.get("pause_between_chunks") or 0,
        push_every_n=config.get("push_every_n") or 1,
        current_chunk=None,
        last_error=""
    )

    add_console("Processing started.", "info")
    add_console(f"Mode: {config['mode']}", "info")
    if config.get("start_chunk"):
        add_console(f"Start chunk: {config.get('start_chunk')}", "info")
    if config.get("end_chunk"):
        add_console(f"End chunk: {config.get('end_chunk')}", "info")
    if config.get("pause_between_chunks"):
        add_console(f"Pause between chunks: {config.get('pause_between_chunks')} sec", "info")
    if config["mode"] == "commit_n_then_push":
        add_console(f"Push every N chunks: {config.get('push_every_n', 1)}", "info")
    add_console(f"Repository: {config['repo_path']}", "info")

    try:
        inspect_git(config["repo_path"])

        if config["mode"] == "push_only":
            add_console("Push-only mode selected. Running git push once.", "info")
            with STATE_LOCK:
                STATE["chunks"] = []
                STATE["_chunk_index"] = {}
                recalc_stats_unlocked()
            save_state()
            code, _ = run_cmd(["git", "push"], cwd=Path(config["repo_path"]), stream=True)
            if code == 0:
                add_console("Push completed successfully.", "success")
            else:
                add_console("Push failed.", "error")
                update_state(last_error="Push failed.")
            return

        # Reset processed-chunk in-memory cache so we re-read from disk at start
        global _PROCESSED_CACHE, _PROCESSED_CACHE_PATH
        with _PROCESSED_LOCK:
            _PROCESSED_CACHE = None
            _PROCESSED_CACHE_PATH = ""

        chunks = parse_chunks(config["log_file"])
        processed, _commit_map, _branch, _remote, _remote_sha, _repaired = reconcile_processed_chunks(
            Path(config["repo_path"]), config["processed_chunks_file"]
        )

        start_chunk = config.get("start_chunk")
        end_chunk = config.get("end_chunk")

        for c in chunks:
            if start_chunk is not None and c["number"] < start_chunk:
                c["status"] = "skipped"
                c["stage"] = "before_start_chunk"
                c["progress"] = 100
            elif end_chunk is not None and c["number"] > end_chunk:
                c["status"] = "skipped"
                c["stage"] = "after_end_chunk"
                c["progress"] = 100
            elif c["number"] in processed:
                c["status"] = "skipped"
                c["stage"] = "already_processed"
                c["progress"] = 100
            else:
                c["status"] = "pending"
                c["stage"] = "pending"
                c["progress"] = 0

        with STATE_LOCK:
            STATE["chunks"] = chunks
            STATE["selected_chunk"] = chunks[0]["number"] if chunks else None
            _build_chunk_index()
            recalc_stats_unlocked()
        save_state()

        add_console(f"Parsed {len(chunks)} chunks.", "success")

        repo = Path(config["repo_path"])
        if config["mode"] == "pipeline":
            add_console("Pipeline mode enabled: sequential commits + one exact-SHA push worker.", "info")
            process_worker_pipeline(config, chunks, repo)
            add_console("Processing finished.", "success")
            return

        for chunk in chunks:
            if STOP_REQUESTED:
                add_console("Stop requested. Processing paused.", "warning")
                break

            n = chunk["number"]

            if chunk["status"] == "skipped":
                add_console(f"Chunk #{n} already processed. Skipping.", "info")
                continue

            chunk_start = time.time()
            set_chunk_status(
                n,
                status="processing",
                stage="checking",
                progress=5,
                started_at=datetime.now().isoformat(timespec="seconds"),
                error="",
                push_output=""
            )
            update_state(current_chunk=n, selected_chunk=n)
            add_console(f"Processing Chunk #{n} ({len(chunk['files'])} files, {chunk['size_mb']} MB)", "info")

            missing = []
            # Parallelise file-existence checks with threads (I/O bound)
            def _check_file(f):
                full = repo / f["path"]
                if full.exists():
                    set_file_status(n, f["path"], "found")
                else:
                    missing.append(f["path"])
                    set_file_status(n, f["path"], "missing")

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(32, len(chunk["files"]) or 1)) as ex:
                list(ex.map(_check_file, chunk["files"]))

            if missing:
                msg = f"Chunk #{n} failed: {len(missing)} missing files."
                set_chunk_status(n, status="failed", stage="missing_files", progress=100, error=msg)
                add_console(msg, "error")
                continue

            if config["mode"] == "dry_run":
                set_chunk_status(n, status="completed", stage="dry_run_done", progress=100)
                add_console(f"[DRY RUN] Chunk #{n}: would add, commit, and maybe push depending on mode.", "success")
                save_processed_chunk(config["processed_chunks_file"], n)
                pause_seconds = float(config.get("pause_between_chunks") or 0)
                if pause_seconds > 0 and not STOP_REQUESTED:
                    add_console(f"Pausing {pause_seconds:g} seconds before next chunk.", "info")
                    time.sleep(pause_seconds)
                continue

            set_chunk_status(n, stage="adding", progress=20)
            files = [f["path"] for f in chunk["files"]]

            code, _ = git_add_files(files, repo)
            if code != 0:
                msg = f"git add failed in Chunk #{n}."
                set_chunk_status(n, status="failed", stage="add_failed", progress=100, error=msg)
                add_console(msg, "error")
                continue

            for fp in files:
                set_file_status(n, fp, "added")
            set_chunk_status(n, progress=55)

            # Graceful stop is checked only at chunk boundaries so the current
            # chunk can finish commit + push + JSON persistence safely.
            if not has_staged_changes(repo):
                set_chunk_status(n, status="completed", stage="already_in_head_reconciled", progress=100,
                                 ended_at=datetime.now().isoformat(timespec="seconds"))
                save_processed_chunk(config["processed_chunks_file"], n)
                add_console(f"Chunk #{n}: files already exist in HEAD. Reconciled processed JSON.", "success")
                continue

            set_chunk_status(n, stage="committing", progress=65)
            commit_message = f"Chunk #{n} - {len(files)} files"
            code, out = run_cmd(["git", "commit", "-m", commit_message], cwd=repo)

            if code != 0:
                lower = out.lower()
                if "nothing to commit" in lower or "no changes added" in lower:
                    set_chunk_status(n, status="skipped", stage="nothing_to_commit", progress=100)
                    save_processed_chunk(config["processed_chunks_file"], n)
                    add_console(f"Chunk #{n}: nothing to commit. Marked processed.", "warning")
                    pause_seconds = float(config.get("pause_between_chunks") or 0)
                    if pause_seconds > 0 and not STOP_REQUESTED:
                        add_console(f"Pausing {pause_seconds:g} seconds before next chunk.", "info")
                        time.sleep(pause_seconds)
                    continue

                msg = f"git commit failed in Chunk #{n}."
                set_chunk_status(n, status="failed", stage="commit_failed", progress=100, error=msg)
                add_console(msg, "error")
                continue

            add_console(f"Chunk #{n} committed.", "success")
            set_chunk_status(n, stage="committed", progress=78)

            if config["mode"] == "commit_only":
                duration = time.time() - chunk_start
                completed_durations.append(duration)
                set_chunk_status(
                    n,
                    status="completed",
                    stage="commit_only_done",
                    progress=100,
                    ended_at=datetime.now().isoformat(timespec="seconds"),
                    duration_seconds=round(duration, 2)
                )
                save_processed_chunk(config["processed_chunks_file"], n)
                add_console(f"Chunk #{n} complete in commit-only mode.", "success")
                pause_seconds = float(config.get("pause_between_chunks") or 0)
                if pause_seconds > 0 and not STOP_REQUESTED:
                    add_console(f"Pausing {pause_seconds:g} seconds before next chunk.", "info")
                    time.sleep(pause_seconds)
                continue

            # commit_n_then_push: batch commits, push every N
            if config["mode"] == "commit_n_then_push":
                push_every_n = int(config.get("push_every_n") or 1)
                committed_since_push.append(n)
                set_chunk_status(n, stage="committed_waiting_push", progress=80)

                should_push = (len(committed_since_push) >= push_every_n)
                # Also push if this is the last pending chunk
                if not should_push:
                    with STATE_LOCK:
                        remaining_pending = STATE["stats"]["pending"]
                    if remaining_pending == 0:
                        should_push = True
                        add_console("Last chunk in batch — pushing now.", "info")

                if should_push:
                    chunk_nums = ", ".join(f"#{x}" for x in committed_since_push)
                    add_console(f"Pushing after {len(committed_since_push)} commits (chunks {chunk_nums}).", "info")
                    set_chunk_status(n, stage="pushing", progress=85)
                    code, out = run_cmd(["git", "push"], cwd=repo, stream=True)

                    if code != 0:
                        msg = f"git push failed after Chunk #{n}."
                        for bn in committed_since_push:
                            set_chunk_status(bn, status="failed", stage="push_failed", progress=100, error=msg)
                        add_console(msg, "error")
                        committed_since_push.clear()
                        continue

                    for bn in committed_since_push:
                        duration = time.time() - chunk_start
                        set_chunk_status(
                            bn,
                            status="completed",
                            stage="done",
                            progress=100,
                            push_output=out[-6000:] if out else "",
                            ended_at=datetime.now().isoformat(timespec="seconds"),
                            duration_seconds=round(duration, 2)
                        )
                        save_processed_chunk(config["processed_chunks_file"], bn)

                    add_console(f"Push complete for chunks {chunk_nums}.", "success")
                    committed_since_push.clear()
                else:
                    add_console(f"Chunk #{n} committed ({len(committed_since_push)}/{push_every_n}). Waiting for more before push.", "info")
                    duration = time.time() - chunk_start
                    completed_durations.append(duration)
                    _duration_sum += duration

                with STATE_LOCK:
                    remaining = STATE["stats"]["pending"]
                    avg = _duration_sum / len(completed_durations) if completed_durations else 0
                    STATE["average_chunk_seconds"] = round(avg, 2)
                    eta_seconds = int(avg * remaining)
                    STATE["eta"] = format_seconds(eta_seconds) if eta_seconds > 0 else "-"
                save_state()

                pause_seconds = float(config.get("pause_between_chunks") or 0)
                if pause_seconds > 0 and not STOP_REQUESTED:
                    add_console(f"Pausing {pause_seconds:g} seconds before next chunk.", "info")
                    time.sleep(pause_seconds)
                continue

            # full mode: push after every commit
            set_chunk_status(n, stage="pushing", progress=85)
            code, out = run_cmd(["git", "push"], cwd=repo, stream=True)
            set_chunk_status(n, push_output=out[-6000:] if out else "")

            if code != 0:
                msg = f"git push failed after Chunk #{n}."
                set_chunk_status(n, status="failed", stage="push_failed", progress=100, error=msg)
                add_console(msg, "error")
                continue

            duration = time.time() - chunk_start
            completed_durations.append(duration)
            _duration_sum += duration
            set_chunk_status(
                n,
                status="completed",
                stage="done",
                progress=100,
                ended_at=datetime.now().isoformat(timespec="seconds"),
                duration_seconds=round(duration, 2)
            )
            save_processed_chunk(config["processed_chunks_file"], n)
            add_console(f"Chunk #{n} completed and pushed.", "success")

            with STATE_LOCK:
                remaining = STATE["stats"]["pending"]
                avg = _duration_sum / len(completed_durations) if completed_durations else 0
                STATE["average_chunk_seconds"] = round(avg, 2)
                eta_seconds = int(avg * remaining)
                STATE["eta"] = format_seconds(eta_seconds) if eta_seconds > 0 else "-"
            save_state()

            pause_seconds = float(config.get("pause_between_chunks") or 0)
            if pause_seconds > 0 and not STOP_REQUESTED:
                add_console(f"Pausing {pause_seconds:g} seconds before next chunk.", "info")
                time.sleep(pause_seconds)

        add_console("Processing finished.", "success")

    except Exception as e:
        update_state(last_error=str(e))
        add_console(f"Fatal error: {e}", "error")

    finally:
        update_state(
            running=False,
            stop_requested=STOP_REQUESTED,
            current_chunk=None,
            ended_at=datetime.now().isoformat(timespec="seconds")
        )



def validate_visualize_payload(payload):
    """Validate UI payload for visualize-only mode. This does NOT require a Git repo."""
    log_file = clean_pasted_path(payload.get("log_file", ""))
    repo_path = clean_pasted_path(payload.get("repo_path", ""))
    processed_chunks_file = clean_pasted_path(payload.get("processed_chunks_file", "")) or str(DEFAULT_PROCESSED_PATH)
    logs_dir = clean_pasted_path(payload.get("logs_dir", "")) or str(DEFAULT_LOGS_DIR)
    start_chunk_raw = clean_pasted_path(str(payload.get("start_chunk", "") or ""))
    end_chunk_raw = clean_pasted_path(str(payload.get("end_chunk", "") or ""))

    if not log_file or not Path(log_file).is_file():
        return None, "git_chunks.log file not found."

    if processed_chunks_file:
        p = Path(processed_chunks_file)
        if p.suffix.lower() != ".json":
            return None, "processed_chunks file should be a .json file."
        p.parent.mkdir(parents=True, exist_ok=True)

    Path(logs_dir).mkdir(parents=True, exist_ok=True)

    # Repo is optional for visualization. If valid, we use it only for file-found checks.
    repo_valid = bool(repo_path and Path(repo_path).is_dir())

    return {
        "log_file": log_file,
        "repo_path": repo_path,
        "repo_valid": repo_valid,
        "processed_chunks_file": processed_chunks_file,
        "logs_dir": logs_dir,
        "start_chunk": start_chunk_raw,
        "end_chunk": end_chunk_raw,
        "mode": "visualize_only"
    }, ""


def visualize_chunks_only(payload):
    """Parse and display chunks in the dashboard without running git add/commit/push."""
    current = get_state_copy()
    if current["running"]:
        return False, "Processor is running. Stop it before visualizing only."

    config, error = validate_visualize_payload(payload)
    if error:
        return False, error

    try:
        chunks = parse_chunks(config["log_file"])
        processed = load_processed_chunks(config["processed_chunks_file"])
        repo = Path(config["repo_path"]) if config.get("repo_valid") else None

        for c in chunks:
            n = c["number"]
            if n in processed:
                c["status"] = "skipped"
                c["stage"] = "already_processed"
                c["progress"] = 100
            else:
                c["status"] = "pending"
                c["stage"] = "visualized"
                c["progress"] = 0

            # Optional read-only file existence check when repo path is valid.
            if repo:
                found_count = 0
                missing_count = 0
                for f in c["files"]:
                    if (repo / f["path"]).exists():
                        f["status"] = "found"
                        found_count += 1
                    else:
                        f["status"] = "missing"
                        missing_count += 1
                c["found_files"] = found_count
                c["missing_files"] = missing_count

        with STATE_LOCK:
            STATE["running"] = False
            STATE["stop_requested"] = False
            STATE["started_at"] = None
            STATE["ended_at"] = datetime.now().isoformat(timespec="seconds")
            STATE["mode"] = "visualize_only"
            STATE["log_file"] = config["log_file"]
            STATE["repo_path"] = config["repo_path"]
            STATE["processed_chunks_file"] = config["processed_chunks_file"]
            STATE["logs_dir"] = config["logs_dir"]
            STATE["start_chunk"] = config.get("start_chunk", "")
            STATE["end_chunk"] = config.get("end_chunk", "")
            STATE["pause_between_chunks"] = 0
            STATE["current_chunk"] = None
            STATE["selected_chunk"] = chunks[0]["number"] if chunks else None
            STATE["chunks"] = chunks
            STATE["last_error"] = ""
            STATE["eta"] = "-"
            STATE["average_chunk_seconds"] = 0
            _build_chunk_index()
            recalc_stats_unlocked()

        save_state()
        add_console(f"Visualized {len(chunks)} chunks only. No git command was executed.", "success")
        if repo:
            add_console("Repo path was provided, so file existence was checked read-only.", "info")
        else:
            add_console("Repo path was not required for visualize-only mode.", "info")
        return True, "Chunks visualized only."
    except Exception as e:
        update_state(last_error=str(e))
        add_console(f"Visualize-only failed: {e}", "error")
        return False, str(e)


def start_processing(payload):
    global PROCESS_THREAD, STOP_REQUESTED

    current = get_state_copy()
    if current["running"]:
        return False, "Processor already running."

    config, error = validate_start_payload(payload)
    if error:
        return False, error

    STOP_REQUESTED = False
    PROCESS_THREAD = threading.Thread(target=process_worker, args=(config,), daemon=True)
    PROCESS_THREAD.start()
    return True, "Started."


def stop_processing():
    global STOP_REQUESTED
    STOP_REQUESTED = True
    # Graceful stop: finish the current chunk and any queued exact-SHA pushes.
    # Killing an active push is what caused pushed commits to be absent from JSON.
    update_state(stop_requested=True)
    add_console("Stop requested. The current chunk will finish safely, then processing will pause.", "warning")
    return True


def open_path(path):
    path = clean_pasted_path(path)
    if not path:
        return False, "No path provided."
    p = Path(path)
    if not p.exists():
        return False, "Path does not exist."

    if sys.platform.startswith("win"):
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)], **_no_window_flags())
    else:
        subprocess.Popen(["xdg-open", str(p)], **_no_window_flags())
    return True, "Opened."


def browse_dialog(kind):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        if kind == "log":
            result = filedialog.askopenfilename(
                title="Select git_chunks.log",
                filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")]
            )
        elif kind == "repo":
            result = filedialog.askdirectory(title="Select Git repository folder")
        elif kind == "processed":
            result = filedialog.asksaveasfilename(
                title="Select processed_chunks.json location",
                defaultextension=".json",
                initialfile="processed_chunks.json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
        elif kind == "logs_dir":
            result = filedialog.askdirectory(title="Select logs folder")
        else:
            result = ""

        root.destroy()
        return result or ""
    except Exception as e:
        add_console(f"Browse dialog failed: {e}", "error")
        return ""


def cleanup_and_shutdown():
    """Clean up all resources and shutdown the server."""
    global STOP_REQUESTED, ACTIVE_PROCESSES, SERVER_INSTANCE
    
    try:
        # Stop any processing
        STOP_REQUESTED = True
        
        # Terminate all active processes
        with ACTIVE_PROCESSES_LOCK:
            active_copy = list(ACTIVE_PROCESSES)
        for proc in active_copy:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        
        ACTIVE_PROCESSES.clear()
        
        # Shutdown the server
        if SERVER_INSTANCE:
            try:
                SERVER_INSTANCE.shutdown()
            except Exception:
                pass
    except Exception:
        pass


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Git Chunk Processor Dashboard</title>
<style>
:root{
  --bg:#0b1120; --panel:#111827; --panel2:#0f172a; --line:#263244;
  --text:#e5e7eb; --muted:#94a3b8; --blue:#3b82f6; --green:#22c55e;
  --red:#ef4444; --orange:#f59e0b; --purple:#a855f7; --gray:#475569;
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#08111f,#0b1120);color:var(--text);font-family:Segoe UI,Arial,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);background:rgba(15,23,42,.96);position:sticky;top:0;z-index:5}
h1{margin:0 0 12px;font-size:22px}
.input-grid{display:grid;grid-template-columns:150px 1fr auto;gap:8px;align-items:center;margin:6px 0}
label{color:#cbd5e1;font-size:13px}
input,select{background:#0b1220;color:#f8fafc;border:1px solid #334155;border-radius:8px;padding:9px 10px;min-height:38px}
button{background:#2563eb;color:white;border:0;border-radius:8px;padding:9px 12px;font-weight:700;cursor:pointer;transition:transform .12s ease,box-shadow .12s ease,filter .12s ease,background .12s ease}
button:hover{background:#1d4ed8;transform:translateY(-1px);box-shadow:0 6px 16px rgba(0,0,0,.22);filter:brightness(1.08)}
button:active{transform:translateY(1px) scale(.98);box-shadow:none;filter:brightness(.92)}
button[disabled]{cursor:not-allowed;opacity:.62;transform:none;box-shadow:none}
button.secondary{background:#334155}
button.secondary:hover{background:#475569}
button.danger{background:#dc2626}
button.danger:hover{background:#ef4444}
button.success{background:#16a34a}
button.success:hover{background:#22c55e}
.action-feedback{margin-top:10px;min-height:36px;display:flex;align-items:center;padding:8px 11px;border:1px solid #334155;border-radius:9px;background:#0b1220;color:#cbd5e1;font-size:13px}
.action-feedback.success{border-color:#166534;color:#bbf7d0;background:#052e16}
.action-feedback.warning{border-color:#92400e;color:#fde68a;background:#451a03}
.action-feedback.error{border-color:#991b1b;color:#fecaca;background:#450a0a}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.stats{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin-top:14px}
.stat{background:#0b1220;border:1px solid var(--line);border-radius:10px;padding:10px}
.stat .n{font-size:20px;font-weight:800}
.stat .t{font-size:12px;color:var(--muted)}
.progress-wrap{height:12px;background:#1e293b;border-radius:99px;overflow:hidden;border:1px solid #334155;margin-top:12px}
.progress-bar{height:100%;background:linear-gradient(90deg,#2563eb,#22c55e);width:0%;transition:width .25s}
.layout{display:grid;grid-template-columns:minmax(420px,1.1fr) minmax(420px,.9fr);gap:16px;padding:16px}
.panel{background:rgba(17,24,39,.92);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.panel h2{font-size:16px;margin:0;padding:13px 15px;background:#0f172a;border-bottom:1px solid var(--line)}
.chunk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(58px,1fr));gap:8px;padding:14px;align-items:stretch}
.chunk-cell{position:relative;min-height:44px;border:1px solid #334155;background:#1e293b;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:800;cursor:pointer;user-select:none;overflow:hidden}
.chunk-cell:hover{border-color:#60a5fa;transform:translateY(-1px)}
.chunk-cell.active{background:#1d4ed8;border-color:#93c5fd;box-shadow:0 0 0 2px rgba(59,130,246,.25)}
.chunk-cell.selected{outline:2px solid #f8fafc}
.chunk-cell.completed{background:rgba(34,197,94,.22);border-color:rgba(34,197,94,.55)}
.chunk-cell.failed{background:rgba(239,68,68,.22);border-color:rgba(239,68,68,.55)}
.chunk-cell.skipped{background:rgba(100,116,139,.25)}
.chunk-cell.processing{background:rgba(168,85,247,.24);border-color:rgba(168,85,247,.7)}
.chunk-cell .mini{position:absolute;bottom:0;left:0;height:4px;background:#38bdf8;width:0%}
.detail{padding:15px}
.card-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.badge{display:inline-block;padding:4px 9px;border-radius:999px;font-size:12px;font-weight:800;background:#334155}
.badge.completed{background:#14532d;color:#bbf7d0}
.badge.failed{background:#7f1d1d;color:#fecaca}
.badge.processing{background:#4c1d95;color:#ddd6fe}
.badge.skipped{background:#334155;color:#cbd5e1}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.meta div{background:#0b1220;border:1px solid var(--line);border-radius:10px;padding:9px}
.meta small{display:block;color:var(--muted);font-size:11px}
.stage{font-size:13px;color:#cbd5e1;margin-top:8px}
.error{background:#3f1111;border:1px solid #7f1d1d;color:#fecaca;border-radius:10px;padding:10px;margin:10px 0;white-space:pre-wrap}
.files{border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:12px}
.file-head,.file-row{display:grid;grid-template-columns:36px 1fr 1.3fr 78px 80px;gap:8px;align-items:center}
.file-head{background:#0f172a;color:#cbd5e1;font-size:12px;font-weight:800;padding:9px}
.file-row{padding:8px 9px;border-top:1px solid #263244;font-size:12px}
.file-row div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-status{font-weight:800;text-transform:capitalize}
.console{height:360px;overflow:auto;background:#050b14;color:#dbeafe;font-family:Consolas,monospace;font-size:12px;padding:12px;line-height:1.45;user-select:text}
.console .error{background:transparent;border:0;color:#fca5a5;padding:0;margin:0}
.console .success{color:#86efac}
.console .warning{color:#fde68a}
.console .cmd{color:#93c5fd}
.console .git{color:#cbd5e1}
.help{color:#94a3b8;font-size:12px;margin-top:8px}
.search-row{display:flex;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}
.search-row input{width:100%}
@media(max-width:980px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.input-grid{grid-template-columns:1fr}.input-grid button{width:100%}}
</style>
</head>
<body>
<header>
  <h1>Git Chunk Processor Dashboard</h1>

  <div class="input-grid">
    <label>git_chunks.log</label>
    <input id="logFile" placeholder="Paste / drag-drop git_chunks.log path">
    <button onclick="browsePath('log')">Browse</button>
  </div>

  <div class="input-grid">
    <label>Git repo folder</label>
    <input id="repoPath" placeholder="Paste / drag-drop Git repository folder path">
    <button onclick="browsePath('repo')">Browse</button>
  </div>

  <div class="input-grid">
    <label>processed_chunks.json</label>
    <input id="processedPath" placeholder="Select processed_chunks.json location">
    <button onclick="browsePath('processed')">Browse</button>
  </div>

  <div class="input-grid">
    <label>Logs folder</label>
    <input id="logsDir" placeholder="Optional logs folder">
    <button onclick="browsePath('logs_dir')">Browse</button>
  </div>

  <div class="input-grid">
    <label>Start chunk</label>
    <input id="startChunk" type="number" min="1" placeholder="Optional, e.g. 25">
    <span></span>
  </div>

  <div class="input-grid">
    <label>End chunk</label>
    <input id="endChunk" type="number" min="1" placeholder="Optional, e.g. 100">
    <span></span>
  </div>

  <div class="input-grid">
    <label>Pause between chunks</label>
    <input id="pauseBetweenChunks" type="number" min="0" step="0.1" placeholder="Seconds, e.g. 2">
    <span></span>
  </div>

  <div class="input-grid" id="pushEveryNRow" style="display:none">
    <label>Push every N chunks</label>
    <input id="pushEveryN" type="number" min="1" step="1" value="2" placeholder="e.g. 2 = commit 2 then push">
    <span></span>
  </div>

  <div class="toolbar">
    <select id="mode" onchange="togglePushEveryN()">
      <option value="full">Full Process: Add → Commit → Push</option>
      <option value="commit_only">Commit Only: Add → Commit</option>
      <option value="commit_n_then_push">Commit N then Push: Add → Commit × N → Push</option>
      <option value="pipeline">Fast Safe Pipeline: Commit Next Chunk While Previous Pushes</option>
      <option value="push_only">Push Only</option>
      <option value="dry_run">Dry Run: Simulation Only</option>
    </select>
    <button class="secondary" onclick="visualizeOnly()">Visualize Chunks Only</button>
    <button class="success" id="startBtn" onclick="startProcessing()" title="Start processing the selected chunk range">▶ Start Processing</button>
    <button class="danger" id="stopBtn" onclick="stopProcessing()" title="Finish the active chunk safely, then pause">■ Stop After Current Chunk</button>
    <button class="secondary" id="refreshBtn" onclick="toggleRefresh()">Pause Auto Refresh</button>
    <button class="secondary" onclick="showActiveChunk()">Show Active</button>
    <button class="secondary" onclick="openStateJson()">Open State JSON</button>
    <button class="secondary" onclick="openProcessed()">Open Processed JSON</button>
  </div>

  <div id="actionFeedback" class="action-feedback">Ready. Select a repository to auto-fill its dashboard paths.</div>

  <div class="stats">
    <div class="stat"><div class="n" id="total">0</div><div class="t">Total</div></div>
    <div class="stat"><div class="n" id="completed">0</div><div class="t">Completed</div></div>
    <div class="stat"><div class="n" id="processing">0</div><div class="t">Processing</div></div>
    <div class="stat"><div class="n" id="pending">0</div><div class="t">Pending</div></div>
    <div class="stat"><div class="n" id="failed">0</div><div class="t">Failed</div></div>
    <div class="stat"><div class="n" id="skipped">0</div><div class="t">Skipped</div></div>
    <div class="stat"><div class="n" id="eta">-</div><div class="t">ETA</div></div>
  </div>
  <div class="progress-wrap"><div class="progress-bar" id="overallBar"></div></div>
  <div class="help">Tip: auto refresh pauses while you select/copy text. Use “Pause Auto Refresh” for long copy work.</div>
</header>

<div class="layout">
  <section class="panel">
    <h2>Chunks</h2>
    <div class="search-row">
      <input id="chunkSearch" placeholder="Search chunk number or file path...">
      <select id="statusFilter" title="Filter chunks by status">
        <option value="all">All Status</option>
        <option value="pending">Pending</option>
        <option value="processing">Processing</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
        <option value="skipped">Skipped</option>
      </select>
      <button class="secondary" onclick="showActiveChunk()">Show Active</button>
    </div>
    <div id="chunkGrid" class="chunk-grid"></div>
  </section>

  <section class="panel">
    <h2>Selected Chunk Details</h2>
    <div id="detail" class="detail">Select a chunk.</div>
  </section>
</div>

<section class="panel" style="margin:0 16px 16px;">
  <h2>Live Console</h2>
  <div id="console" class="console"></div>
</section>

<script>
let state = null;
let selectedChunk = null;
let autoRefreshEnabled = true;
let userInteracting = false;
let chunkCellMap = new Map();
let lastConsoleLength = 0;
let lastChunkSignature = "";
let searchText = "";
let statusFilter = "all";

function cleanPath(v){
  v = (v || "").trim();
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
    v = v.substring(1, v.length - 1);
  }
  return v.trim();
}

["logFile","repoPath","processedPath","logsDir"].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener("drop", e => {
    e.preventDefault();
    const text = e.dataTransfer.getData("text/plain");
    if (text) {
      el.value = cleanPath(text);
      if (id === "repoPath") applyRepoDefaults(el.value);
    }
  });
  el.addEventListener("dragover", e => e.preventDefault());
});

document.addEventListener("mousedown", () => { userInteracting = true; });
document.addEventListener("mouseup", () => {
  setTimeout(() => {
    if (!window.getSelection().toString()) userInteracting = false;
  }, 1200);
});
document.addEventListener("selectionchange", () => {
  const txt = window.getSelection().toString();
  if (txt && txt.length > 0) userInteracting = true;
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") {
    setTimeout(() => { userInteracting = false; }, 800);
  }
});

document.getElementById("chunkSearch").addEventListener("input", e => {
  searchText = e.target.value.toLowerCase().trim();
  rebuildChunkGrid();
});

document.getElementById("statusFilter").addEventListener("change", e => {
  statusFilter = e.target.value || "all";
  rebuildChunkGrid();
});

function toggleRefresh(){
  autoRefreshEnabled = !autoRefreshEnabled;
  document.getElementById("refreshBtn").textContent = autoRefreshEnabled ? "Pause Auto Refresh" : "Resume Auto Refresh";
}

function showActiveChunk(){
  if (!state || !state.current_chunk) {
    alert("No active chunk right now.");
    return;
  }
  selectedChunk = state.current_chunk;
  const searchInput = document.getElementById("chunkSearch");
  const statusSelect = document.getElementById("statusFilter");
  if (searchInput) {
    searchInput.value = "";
    searchText = "";
  }
  if (statusSelect) {
    statusSelect.value = "all";
    statusFilter = "all";
  }
  rebuildChunkGrid();
  updateChunkGridStable(true);
  updateDetailStable(true);
  const cell = chunkCellMap.get(selectedChunk);
  if (cell) cell.scrollIntoView({behavior:"smooth", block:"center", inline:"center"});
}

let _stateVersion = "";

async function api(path, data=null){
  const opts = data ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)} : {};
  const res = await fetch(path, opts);
  if (res.status === 304) return null;
  return await res.json();
}

function setFeedback(message, level="info"){
  const box = document.getElementById("actionFeedback");
  box.textContent = message;
  box.className = "action-feedback" + (level === "info" ? "" : " " + level);
}

async function applyRepoDefaults(repoPath){
  const repo = cleanPath(repoPath);
  if (!repo) return;
  const res = await api("/repo_defaults", {repo_path: repo});
  if (!res || !res.ok) {
    setFeedback((res && res.message) || "Could not create repo-local dashboard paths.", "error");
    return;
  }
  document.getElementById("repoPath").value = res.paths.repo_path;
  document.getElementById("logFile").value = res.paths.log_file;
  document.getElementById("processedPath").value = res.paths.processed_chunks_file;
  document.getElementById("logsDir").value = res.paths.logs_dir;
  setFeedback("Repository selected. Log, processed JSON, and Logs folder paths were updated automatically.", "success");
}

document.getElementById("repoPath").addEventListener("change", e => applyRepoDefaults(e.target.value));
document.getElementById("repoPath").addEventListener("blur", e => applyRepoDefaults(e.target.value));

async function browsePath(kind){
  const res = await api("/browse?kind=" + encodeURIComponent(kind));
  if (!res || !res.path) return;
  if (kind === "log") document.getElementById("logFile").value = res.path;
  if (kind === "repo") {
    document.getElementById("repoPath").value = res.path;
    await applyRepoDefaults(res.path);
  }
  if (kind === "processed") document.getElementById("processedPath").value = res.path;
  if (kind === "logs_dir") document.getElementById("logsDir").value = res.path;
}


async function visualizeOnly(){
  const payload = {
    log_file: cleanPath(document.getElementById("logFile").value),
    repo_path: cleanPath(document.getElementById("repoPath").value),
    processed_chunks_file: cleanPath(document.getElementById("processedPath").value),
    logs_dir: cleanPath(document.getElementById("logsDir").value),
    start_chunk: cleanPath(document.getElementById("startChunk").value),
    end_chunk: cleanPath(document.getElementById("endChunk").value)
  };
  const res = await api("/visualize", payload);
  if (!res.ok) alert(res.message || "Failed to visualize chunks");
  await refreshState(true);
}

async function startProcessing(){
  const btn = document.getElementById("startBtn");
  btn.disabled = true;
  btn.textContent = "Starting…";
  setFeedback("Starting chunk processing…", "info");
  const payload = {
    log_file: cleanPath(document.getElementById("logFile").value),
    repo_path: cleanPath(document.getElementById("repoPath").value),
    processed_chunks_file: cleanPath(document.getElementById("processedPath").value),
    logs_dir: cleanPath(document.getElementById("logsDir").value),
    mode: document.getElementById("mode").value,
    start_chunk: cleanPath(document.getElementById("startChunk").value),
    end_chunk: cleanPath(document.getElementById("endChunk").value),
    pause_between_chunks: cleanPath(document.getElementById("pauseBetweenChunks").value),
    push_every_n: cleanPath(document.getElementById("pushEveryN").value) || "1"
  };
  try {
    const res = await api("/start", payload);
    if (!res.ok) {
      setFeedback(res.message || "Failed to start", "error");
      alert(res.message || "Failed to start");
    } else {
      setFeedback("Processing started. The dashboard will update as each chunk advances.", "success");
    }
    await refreshState(true);
  } finally {
    btn.disabled = false;
    btn.textContent = "▶ Start Processing";
  }
}

function togglePushEveryN(){
  const mode = document.getElementById("mode").value;
  const row = document.getElementById("pushEveryNRow");
  row.style.display = (mode === "commit_n_then_push") ? "grid" : "none";
}

async function stopProcessing(){
  const btn = document.getElementById("stopBtn");
  btn.disabled = true;
  btn.textContent = "Stopping safely…";
  setFeedback("Stop requested. The active chunk and queued push will finish safely before processing pauses.", "warning");
  try {
    await api("/stop", {});
    await refreshState(true);
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "■ Stop After Current Chunk";
    }, 900);
  }
}

async function openProcessed(){
  const path = cleanPath(document.getElementById("processedPath").value);
  const res = await api("/open", {path});
  if (!res.ok) alert(res.message || "Could not open path");
}

async function openStateJson(){
  const res = await api("/open_state", {});
  if (!res.ok) alert(res.message || "Could not open state JSON");
}

async function refreshState(force=false){
  if (!force && (!autoRefreshEnabled || userInteracting)) return;
  try {
    const headers = {};
    if (_stateVersion && !force) headers["X-State-Version"] = _stateVersion;
    const res = await fetch("/state", { headers });
    if (res.status === 304) return;  // nothing changed — skip all DOM work
    const newState = await res.json();
    if (newState._version !== undefined) _stateVersion = String(newState._version);
    const first = !state;
    state = newState;

    if (first) {
      document.getElementById("logFile").value = state.log_file || "";
      document.getElementById("repoPath").value = state.repo_path || "";
      document.getElementById("processedPath").value = state.processed_chunks_file || "";
      document.getElementById("logsDir").value = state.logs_dir || "";
      document.getElementById("startChunk").value = state.start_chunk || "";
      document.getElementById("endChunk").value = state.end_chunk || "";
      document.getElementById("pauseBetweenChunks").value = state.pause_between_chunks || "";
      document.getElementById("mode").value = state.mode || "full";
      if (state.push_every_n) document.getElementById("pushEveryN").value = state.push_every_n;
      togglePushEveryN();
    }

    updateStats();
    updateChunkGridStable();
    updateDetailStable();
    updateConsoleStable();
  } catch(e) {
    console.error("Refresh error", e);
  }
}

function updateStats(){
  const s = state.stats || {};
  ["total","completed","processing","pending","failed","skipped"].forEach(k => {
    document.getElementById(k).textContent = s[k] ?? 0;
  });
  document.getElementById("eta").textContent = state.eta || "-";
  document.getElementById("overallBar").style.width = (state.overall_progress || 0) + "%";
}

function chunkVisible(c){
  if (statusFilter !== "all" && String(c.status || "pending") !== statusFilter) return false;
  if (!searchText) return true;
  if (String(c.number).includes(searchText)) return true;
  if (String(c.status || "").toLowerCase().includes(searchText)) return true;
  if (String(c.stage || "").toLowerCase().includes(searchText)) return true;
  return (c.files || []).some(f => (f.path || "").toLowerCase().includes(searchText));
}

function chunkSignature(){
  return (state.chunks || [])
    .filter(chunkVisible)
    .map(c => `${c.number}:${c.status}:${c.progress}:${state.current_chunk===c.number}:${selectedChunk===c.number}`)
    .join("|");
}

function rebuildChunkGrid(){
  const grid = document.getElementById("chunkGrid");
  grid.innerHTML = "";
  chunkCellMap.clear();

  (state?.chunks || []).filter(chunkVisible).forEach(c => {
    const cell = document.createElement("div");
    cell.className = "chunk-cell";
    cell.dataset.chunk = c.number;
    cell.title = `Chunk #${c.number} - ${c.status}`;
    cell.innerHTML = `<span>${c.number}</span><div class="mini"></div>`;
    cell.addEventListener("click", () => {
      selectedChunk = c.number;
      updateChunkGridStable(true);
      updateDetailStable(true);
    });
    grid.appendChild(cell);
    chunkCellMap.set(c.number, cell);
  });

  lastChunkSignature = "";
  updateChunkGridStable(true);
}

function updateChunkGridStable(force=false){
  const sig = chunkSignature();
  if (!force && sig === lastChunkSignature) return;

  const visible = (state.chunks || []).filter(chunkVisible);
  if (visible.length !== chunkCellMap.size) {
    rebuildChunkGrid();
    return;
  }

  visible.forEach(c => {
    let cell = chunkCellMap.get(c.number);
    if (!cell) {
      rebuildChunkGrid();
      return;
    }

    const isActive = state.current_chunk === c.number;
    const isSelected = selectedChunk === c.number;

    cell.className = "chunk-cell";
    if (c.status) cell.classList.add(c.status);
    if (isActive) cell.classList.add("active");
    if (isSelected) cell.classList.add("selected");

    cell.title = `Chunk #${c.number} - ${c.status} - ${c.stage}`;
    const mini = cell.querySelector(".mini");
    if (mini) mini.style.width = (c.progress || 0) + "%";
  });

  lastChunkSignature = sig;

  if (!selectedChunk && state.current_chunk) selectedChunk = state.current_chunk;
  if (!selectedChunk && visible.length) selectedChunk = visible[0].number;
}

function escapeHtml(str){
  return String(str ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;");
}

function selectedChunkData(){
  const chunks = state.chunks || [];
  if (!selectedChunk && state.current_chunk) selectedChunk = state.current_chunk;
  if (!selectedChunk && chunks.length) selectedChunk = chunks[0].number;
  return chunks.find(c => c.number === selectedChunk) || null;
}

let lastDetailSig = "";
function updateDetailStable(force=false){
  const c = selectedChunkData();
  const detail = document.getElementById("detail");
  if (!c) {
    detail.textContent = "No chunk selected.";
    return;
  }

  const sig = `${c.number}:${c.status}:${c.stage}:${c.progress}:${c.error}:${c.files?.length}:${c.push_output?.length}`;
  if (!force && sig === lastDetailSig) return;
  lastDetailSig = sig;

  const files = c.files || [];
  const fileRows = files.map((f, i) => `
    <div class="file-row">
      <div>${i+1}</div>
      <div title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
      <div title="${escapeHtml(f.folder)}">${escapeHtml(f.folder)}</div>
      <div>${Number(f.size_mb || 0).toFixed(2)} MB</div>
      <div class="file-status">${escapeHtml(f.status || "pending")}</div>
    </div>
  `).join("");

  detail.innerHTML = `
    <div class="card-top">
      <div>
        <h3 style="margin:0">Chunk #${c.number}</h3>
        <div class="stage">Stage: <strong>${escapeHtml(c.stage || "-")}</strong></div>
      </div>
      <div class="badge ${escapeHtml(c.status)}">${escapeHtml(c.status || "pending")}</div>
    </div>

    <div class="meta">
      <div><small>Files</small><strong>${files.length}</strong></div>
      <div><small>Size</small><strong>${Number(c.size_mb || 0).toFixed(2)} MB</strong></div>
      <div><small>Duration</small><strong>${c.duration_seconds ? c.duration_seconds + "s" : "-"}</strong></div>
    </div>

    <div class="progress-wrap"><div class="progress-bar" style="width:${c.progress || 0}%"></div></div>

    ${c.error ? `<div class="error">${escapeHtml(c.error)}</div>` : ""}

    <div class="files">
      <div class="file-head">
        <div>#</div><div>File</div><div>Folder</div><div>Size</div><div>Status</div>
      </div>
      ${fileRows}
    </div>

    ${c.push_output ? `<h3>Push Output</h3><pre class="error">${escapeHtml(c.push_output)}</pre>` : ""}
  `;
}

function updateConsoleStable(){
  const con = document.getElementById("console");
  const lines = state.console || [];
  if (lines.length === lastConsoleLength) return;

  const atBottom = con.scrollTop + con.clientHeight >= con.scrollHeight - 20;

  if (lastConsoleLength > lines.length) {
    con.innerHTML = "";
    lastConsoleLength = 0;
  }

  const newLines = lines.slice(lastConsoleLength);
  newLines.forEach(l => {
    const div = document.createElement("div");
    div.className = l.level || "info";
    div.textContent = `[${l.time}] ${l.message}`;
    con.appendChild(div);
  });

  lastConsoleLength = lines.length;
  if (atBottom) con.scrollTop = con.scrollHeight;
}

setInterval(() => refreshState(false), 1500);
refreshState(true);

// Send shutdown signal when page closes
window.addEventListener("beforeunload", () => {
  navigator.sendBeacon("/shutdown", "");
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            return self._send_html()

        if parsed.path == "/state":
            client_ver = self.headers.get("X-State-Version", "")
            current_ver = str(_STATE_VERSION)
            if client_ver == current_ver:
                self.send_response(304)
                self.send_header("X-State-Version", current_ver)
                self.end_headers()
                return
            data = get_state_copy()
            data["_version"] = _STATE_VERSION
            return self._send_json(data)

        if parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            kind = qs.get("kind", [""])[0]
            path = browse_dialog(kind)
            return self._send_json({"path": path})

        return self._send_json({"ok": False, "message": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/visualize":
            payload = self._read_json()
            ok, msg = visualize_chunks_only(payload)
            return self._send_json({"ok": ok, "message": msg})

        if parsed.path == "/repo_defaults":
            payload = self._read_json()
            repo_path = clean_pasted_path(payload.get("repo_path", ""))
            if not repo_path or not Path(repo_path).is_dir():
                return self._send_json({"ok": False, "message": "Git repository folder not found."})
            if not (Path(repo_path) / ".git").exists():
                return self._send_json({"ok": False, "message": "Selected folder is not a Git repository."})
            paths = repo_default_paths(repo_path)
            return self._send_json({"ok": True, "paths": paths})

        if parsed.path == "/start":
            payload = self._read_json()
            ok, msg = start_processing(payload)
            return self._send_json({"ok": ok, "message": msg})

        if parsed.path == "/stop":
            stop_processing()
            return self._send_json({"ok": True})

        if parsed.path == "/open":
            payload = self._read_json()
            ok, msg = open_path(payload.get("path", ""))
            return self._send_json({"ok": ok, "message": msg})

        if parsed.path == "/open_state":
            if not DEFAULT_STATE_PATH.exists():
                save_state()
            ok, msg = open_path(str(DEFAULT_STATE_PATH))
            return self._send_json({"ok": ok, "message": msg})

        if parsed.path == "/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=cleanup_and_shutdown, daemon=False).start()
            return

        return self._send_json({"ok": False, "message": "Not found"}, 404)


def main():
    global SERVER_INSTANCE
    
    try:
        DEFAULT_LOGS_DIR.mkdir(parents=True, exist_ok=True)

        if not DEFAULT_STATE_PATH.exists():
            save_state()

        url = f"http://{HOST}:{PORT}"

        server = ThreadingHTTPServer((HOST, PORT), Handler)
        SERVER_INSTANCE = server

        def open_browser():
            time.sleep(0.7)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

        server.serve_forever()
    except Exception:
        # Silently catch all exceptions to prevent terminal from appearing
        pass
    finally:
        try:
            cleanup_and_shutdown()
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()