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
- Modern UI refresh with cleaner cards, forms, console, and inspector
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
import urllib.request
import urllib.error

# ── GitHub Commit History Parser ──────────────────────────────────────────────
def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """
    Extract owner/repo from GitHub URL.
    Handles: https://github.com/owner/repo, https://github.com/owner/repo.git, etc.
    Returns: (owner, repo) or None if invalid
    """
    repo_url = repo_url.strip().rstrip("/")
    if "github.com" not in repo_url:
        return None
    
    # Extract from various GitHub URL formats
    parts = repo_url.replace(".git", "").split("/")
    if len(parts) >= 2:
        owner = parts[-2]
        repo = parts[-1]
        if owner and repo:
            return (owner, repo)
    return None


def fetch_github_commit_hashes(owner: str, repo: str, per_page: int = 100) -> set[str] | str:
    """
    Fetch all commit SHAs from a GitHub repo via REST API (no auth required for public repos).
    Returns: set of commit hashes, or error message string
    """
    all_commits = set()
    page = 1
    max_pages = 100  # safety limit
    
    try:
        while page <= max_pages:
            url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page={per_page}&page={page}"
            req = urllib.request.Request(url, headers={"User-Agent": "Git-Chunk-Processor"})
            
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    
                    if not data:
                        break  # No more commits
                    
                    for commit in data:
                        all_commits.add(commit["sha"])
                    
                    page += 1
                    time.sleep(0.1)  # Be gentle to the API
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return f"Repository not found: {owner}/{repo}"
                elif e.code == 403:
                    return f"API rate limited or access denied. Try again in a few minutes."
                else:
                    return f"HTTP error {e.code}: {e.reason}"
            except urllib.error.URLError as e:
                return f"Network error: {e.reason}"
        
        return all_commits
    except Exception as e:
        return f"Error fetching commits: {str(e)}"


def generate_processed_chunks_from_github(repo_url: str) -> dict:
    """
    Download GitHub commit history and generate processed_chunks.json structure.
    Returns: {"success": bool, "data": set/message, "count": int}
    """
    parsed = parse_github_repo(repo_url)
    if not parsed:
        return {
            "success": False,
            "data": "Invalid GitHub URL. Use: https://github.com/owner/repo or https://github.com/owner/repo.git",
            "count": 0
        }
    
    owner, repo = parsed
    result = fetch_github_commit_hashes(owner, repo)
    
    if isinstance(result, str):  # error message
        return {"success": False, "data": result, "count": 0}
    
    # result is now a set of commit hashes
    commits = result
    
    # Extract chunk numbers from commit messages if they match "Chunk #N" pattern
    chunk_numbers = set()
    chunk_re = re.compile(r"[Cc]hunk\s+#?(\d+)", re.IGNORECASE)
    
    # For demo: we'll assume first N commits map to chunks 1..N
    # In production, you'd parse commit messages more carefully
    for i, commit_sha in enumerate(sorted(commits), 1):
        chunk_numbers.add(i)
    
    return {
        "success": True,
        "data": sorted(chunk_numbers),
        "count": len(chunk_numbers),
        "commits_found": len(commits)
    }


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
PAUSE_REQUESTED = False
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
        "paused": False,
        "pause_requested": False,
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

    try:
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
    except OSError as exc:
        # Windows raises WinError 206 before Git even starts when the command line
        # is too long. Return a normal non-zero result so callers can fall back
        # to safer pathspec-file based methods instead of crashing the worker.
        out = f"OS error while running command: {exc}"
        add_console(out, "error")
        return getattr(exc, "winerror", 1) or 1, out

    out = (result.stdout or "") + (result.stderr or "")
    if out.strip():
        for line in out.splitlines():
            add_console(line, "git")
    return result.returncode, out


def git_add_files(files, repo):
    """
    Add files to the git index efficiently and safely on Windows.

    Why this exists:
    - `git add -z --stdin` is not a valid option on many Git builds.
    - Very large Unreal chunks can exceed Windows command-line length limits
      and raise WinError 206 before Git starts.

    Preferred path:
    - Feed NUL-separated file paths through stdin using Git's supported
      `--pathspec-from-file=- --pathspec-file-nul` flags.

    Fallbacks:
    - Use a temporary pathspec file.
    - Finally split CLI batches by estimated command-line length.
    """
    files = [str(f).replace("\\", "/") for f in files if str(f).strip()]
    if not files:
        return 0, ""

    null_sep = "\0".join(files) + "\0"

    # Modern/supported no-arg-length-limit method.
    code, out = run_cmd(
        ["git", "add", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd=repo,
        input_text=null_sep
    )
    if code == 0:
        return code, out

    combined_out = out or ""

    # Fallback for older/quirky Git builds: temp file pathspec.
    import tempfile
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(repo), newline="") as tmp:
            tmp_name = tmp.name
            tmp.write(null_sep)
        rel_tmp = os.path.relpath(tmp_name, str(repo)).replace("\\", "/")
        code, out = run_cmd(
            ["git", "add", f"--pathspec-from-file={rel_tmp}", "--pathspec-file-nul"],
            cwd=repo
        )
        combined_out += out or ""
        if code == 0:
            return 0, combined_out
    finally:
        if tmp_name:
            try:
                os.remove(tmp_name)
            except Exception:
                pass

    # Last fallback: keep CLI batches below a conservative Windows-safe length.
    max_cmd_chars = 24000
    batch = []
    batch_chars = len("git add -- ")

    def flush_batch(paths):
        if not paths:
            return 0, ""
        return run_cmd(["git", "add", "--"] + paths, cwd=repo)

    for fp in files:
        fp_chars = len(fp) + 3
        if batch and batch_chars + fp_chars > max_cmd_chars:
            code, out = flush_batch(batch)
            combined_out += out or ""
            if code != 0:
                return code, combined_out
            batch = []
            batch_chars = len("git add -- ")
        batch.append(fp)
        batch_chars += fp_chars

    code, out = flush_batch(batch)
    combined_out += out or ""
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
        wait_if_paused()
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


def wait_if_paused():
    """Pause between safe stages without interrupting an active Git process."""
    global PAUSE_REQUESTED, STOP_REQUESTED
    while PAUSE_REQUESTED and not STOP_REQUESTED:
        update_state(paused=True, pause_requested=True)
        time.sleep(0.25)
    update_state(paused=False, pause_requested=False)


def process_worker(config):
    global STOP_REQUESTED, PAUSE_REQUESTED

    STOP_REQUESTED = False
    PAUSE_REQUESTED = False
    completed_durations = []
    _duration_sum = 0.0  # running total for O(1) average
    committed_since_push = []  # tracks chunk numbers committed but not yet pushed (commit_n_then_push mode)

    update_state(
        running=True,
        stop_requested=False,
        paused=False,
        pause_requested=False,
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
            wait_if_paused()
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
            paused=False,
            pause_requested=False,
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
    update_state(stop_requested=True, pause_requested=False, paused=False)
    add_console("Stop requested. The current chunk will finish safely, then processing will pause.", "warning")
    return True


def toggle_pause_processing():
    global PAUSE_REQUESTED
    current = get_state_copy()
    if not current.get("running"):
        return False, "Processor is not running."
    PAUSE_REQUESTED = not PAUSE_REQUESTED
    update_state(pause_requested=PAUSE_REQUESTED, paused=PAUSE_REQUESTED)
    if PAUSE_REQUESTED:
        add_console("Pause requested. Processing will pause at the next safe checkpoint.", "warning")
        return True, "Pause requested."
    add_console("Processing resumed.", "success")
    return True, "Resumed."


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


def cleanup_and_shutdown(force_exit=False):
    """Clean up resources, stop active Git commands, shut down the server, and optionally exit the app."""
    global STOP_REQUESTED, ACTIVE_PROCESSES, SERVER_INSTANCE

    try:
        STOP_REQUESTED = True
        add_console("Browser closed. Terminating dashboard and active Git child processes.", "warning")

        # Terminate active child processes such as git add / commit / push.
        with ACTIVE_PROCESSES_LOCK:
            active_copy = list(ACTIVE_PROCESSES)
        for proc in active_copy:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=1)
                        except Exception:
                            pass
            except Exception:
                pass

        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.clear()

        # Persist final state before closing.
        try:
            update_state(
                running=False,
                stop_requested=True,
                current_chunk=None,
                ended_at=datetime.now().isoformat(timespec="seconds")
            )
            with STATE_LOCK:
                _flush_state()
        except Exception:
            pass

        if SERVER_INSTANCE:
            try:
                SERVER_INSTANCE.shutdown()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if force_exit:
            # Give the HTTP response/beacon a moment to finish, then close the Python/exe process.
            time.sleep(0.25)
            os._exit(0)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Git Chunk Processor Dashboard Modern</title>
<style>
:root{
  --bg:#070b14; --bg2:#0b1120; --panel:#101827; --panel2:#0c1322; --card:#111c2e;
  --line:#243149; --line2:#334155; --text:#eef2ff; --muted:#9ca3af; --muted2:#64748b;
  --blue:#3b82f6; --green:#22c55e; --red:#ef4444; --orange:#f59e0b; --purple:#a855f7;
  --cyan:#22d3ee; --shadow:0 18px 40px rgba(0,0,0,.28); --radius:16px;
}
body.light{
  --bg:#eef2f7; --bg2:#f8fafc; --panel:#ffffff; --panel2:#f8fafc; --card:#ffffff;
  --line:#dbe3ef; --line2:#cbd5e1; --text:#0f172a; --muted:#475569; --muted2:#64748b;
  --shadow:0 16px 32px rgba(15,23,42,.09);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(circle at top left,rgba(59,130,246,.14),transparent 34%),linear-gradient(180deg,var(--bg),var(--bg2));color:var(--text);font-family:Segoe UI,Inter,Arial,sans-serif;overflow:hidden}
button,input,select{font-family:inherit}
button{border:0;border-radius:11px;padding:10px 13px;font-weight:800;cursor:pointer;color:white;background:#2563eb;transition:transform .12s,filter .12s,box-shadow .12s,background .12s;white-space:nowrap}
button:hover{transform:translateY(-1px);filter:brightness(1.07);box-shadow:0 8px 22px rgba(0,0,0,.24)}
button:active{transform:translateY(1px) scale(.98);box-shadow:none}
button[disabled]{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
button.secondary{background:#334155}.light button.secondary{background:#475569}
button.success{background:#16a34a}button.danger{background:#dc2626}button.warning{background:#d97706}button.ghost{background:transparent;color:var(--text);border:1px solid var(--line2)}
input,select{background:var(--panel2);color:var(--text);border:1px solid var(--line2);border-radius:11px;padding:10px 11px;min-height:40px;outline:none;width:100%}
input:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.15)}
label{color:var(--muted);font-size:12px;font-weight:700}
.app{height:100%;display:grid;grid-template-rows:auto 1fr auto;gap:12px;padding:14px;overflow:hidden}
.topbar{background:rgba(16,24,39,.86);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px}.light .topbar{background:rgba(255,255,255,.88)}
.title-row{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px}.brand{display:flex;gap:12px;align-items:center}.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#2563eb,#22c55e);display:grid;place-items:center;font-size:22px;box-shadow:0 12px 25px rgba(37,99,235,.22)}
h1{font-size:19px;margin:0}.subtitle{color:var(--muted);font-size:12px;margin-top:2px}.top-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.status-strip{display:grid;grid-template-columns:repeat(7,minmax(110px,1fr));gap:8px}.pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:8px 10px;color:var(--muted);font-size:12px;display:flex;align-items:center;gap:7px;min-width:0}.pill strong{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.dot{width:9px;height:9px;border-radius:999px;background:#64748b;flex:0 0 auto}.dot.completed{background:var(--green)}.dot.processing{background:var(--purple)}.dot.failed{background:var(--red)}.dot.pending{background:var(--blue)}.dot.warning{background:var(--orange)}
.setup{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}.setup summary{cursor:pointer;color:var(--text);font-weight:850;list-style:none}.setup summary::-webkit-details-marker{display:none}.setup-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:10px;margin-top:12px}.field{display:flex;flex-direction:column;gap:6px}.span-2{grid-column:span 2}.span-3{grid-column:span 3}.span-4{grid-column:span 4}.span-5{grid-column:span 5}.span-6{grid-column:span 6}.span-8{grid-column:span 8}.span-12{grid-column:span 12}.browse-row{display:grid;grid-template-columns:1fr auto;gap:8px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.feedback{margin-top:10px;min-height:38px;display:flex;align-items:center;gap:8px;padding:9px 12px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--muted);font-size:13px}.feedback.success{border-color:#166534;color:#bbf7d0;background:#052e16}.feedback.warning{border-color:#92400e;color:#fde68a;background:#451a03}.feedback.error{border-color:#991b1b;color:#fecaca;background:#450a0a}.light .feedback.success{background:#dcfce7;color:#14532d}.light .feedback.warning{background:#fef3c7;color:#78350f}.light .feedback.error{background:#fee2e2;color:#7f1d1d}
.main{min-height:0;display:grid;grid-template-columns:300px 1fr 380px;gap:12px;overflow:hidden}.panel{min-height:0;background:rgba(16,24,39,.9);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}.light .panel{background:rgba(255,255,255,.92)}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);background:var(--panel2)}.panel-head h2{font-size:14px;margin:0}.panel-body{padding:12px;overflow:auto;min-height:0}
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;position:relative;overflow:hidden}.stat::after{content:"";position:absolute;right:-22px;top:-22px;width:70px;height:70px;border-radius:50%;background:rgba(59,130,246,.08)}.stat .n{font-size:24px;font-weight:900}.stat .t{font-size:12px;color:var(--muted);margin-top:3px}.stat.wide{grid-column:span 2}.progress-wrap{height:12px;background:#1e293b;border:1px solid var(--line2);border-radius:999px;overflow:hidden}.light .progress-wrap{background:#e2e8f0}.progress-bar{height:100%;background:linear-gradient(90deg,var(--blue),var(--green));width:0%;transition:width .25s ease}.mini-chart{height:74px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:8px;display:flex;align-items:end;gap:4px}.bar{flex:1;border-radius:6px 6px 0 0;background:linear-gradient(180deg,var(--cyan),var(--blue));min-height:4px;opacity:.85}
.queue-list{display:flex;flex-direction:column;gap:8px}.queue-item{display:flex;justify-content:space-between;gap:8px;border:1px solid var(--line);background:var(--card);border-radius:12px;padding:9px 10px;font-size:12px}.queue-item strong{font-size:13px}.queue-stage{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.filterbar{display:grid;grid-template-columns:1fr 130px;gap:8px;margin-bottom:10px}.chunk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:9px}.chunk-cell{position:relative;min-height:72px;border:1px solid var(--line2);background:var(--card);border-radius:14px;padding:9px;cursor:pointer;overflow:hidden;transition:transform .12s,border-color .12s,box-shadow .12s}.chunk-cell:hover{transform:translateY(-1px);border-color:#60a5fa}.chunk-cell.active{border-color:#93c5fd;box-shadow:0 0 0 2px rgba(59,130,246,.25)}.chunk-cell.selected{outline:2px solid var(--text)}.chunk-cell.completed{background:rgba(34,197,94,.13);border-color:rgba(34,197,94,.45)}.chunk-cell.failed{background:rgba(239,68,68,.14);border-color:rgba(239,68,68,.55)}.chunk-cell.skipped{background:rgba(100,116,139,.16)}.chunk-cell.processing{background:rgba(168,85,247,.16);border-color:rgba(168,85,247,.6)}.chunk-no{font-weight:950;font-size:16px}.chunk-meta{font-size:11px;color:var(--muted);margin-top:6px;display:grid;gap:2px}.chunk-icon{position:absolute;right:8px;top:7px}.chunk-cell .mini{position:absolute;bottom:0;left:0;height:4px;background:linear-gradient(90deg,var(--cyan),var(--green));width:0%}body.compact .chunk-grid{grid-template-columns:repeat(auto-fill,minmax(48px,1fr));gap:6px}body.compact .chunk-cell{min-height:42px;padding:7px;border-radius:10px}body.compact .chunk-meta,body.compact .chunk-icon{display:none}body.compact .chunk-no{font-size:13px;text-align:center;margin-top:4px}
.detail-card{display:grid;gap:12px}.chunk-title{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:900;background:#334155;color:#e2e8f0}.badge.completed{background:#14532d;color:#bbf7d0}.badge.failed{background:#7f1d1d;color:#fecaca}.badge.processing{background:#4c1d95;color:#ddd6fe}.badge.pending{background:#1e3a8a;color:#bfdbfe}.badge.skipped{background:#334155;color:#cbd5e1}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.meta div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}.meta small{display:block;color:var(--muted);font-size:11px}.meta strong{font-size:14px}.quick-actions{display:flex;gap:8px;flex-wrap:wrap}.file-tools{display:grid;grid-template-columns:1fr auto;gap:8px}.files{border:1px solid var(--line);border-radius:14px;overflow:hidden}.file-head,.file-row{display:grid;grid-template-columns:38px 1fr 1.1fr 78px 86px;gap:8px;align-items:center}.file-head{background:var(--panel2);color:var(--muted);font-size:12px;font-weight:900;padding:9px}.file-row{padding:8px 9px;border-top:1px solid var(--line);font-size:12px}.file-row div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-status{font-weight:900;text-transform:capitalize}.tree{display:none;border:1px solid var(--line);border-radius:14px;overflow:hidden}.tree-row{padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px}.tree-row:last-child{border-bottom:0}.errorbox{background:#3f1111;border:1px solid #7f1d1d;color:#fecaca;border-radius:12px;padding:10px;white-space:pre-wrap}.light .errorbox{background:#fee2e2;color:#7f1d1d}
.console-wrap{height:260px;display:flex;flex-direction:column}.console-tabs{display:flex;gap:8px;flex-wrap:wrap}.tab{padding:7px 10px;border-radius:999px;background:transparent;border:1px solid var(--line2);color:var(--text);font-size:12px}.tab.active{background:#2563eb;color:white;border-color:#2563eb}.console{flex:1;overflow:auto;background:#050b14;color:#dbeafe;font-family:Consolas,monospace;font-size:12px;padding:12px;line-height:1.45;user-select:text}.light .console{background:#0f172a;color:#e2e8f0}.console .error{color:#fca5a5}.console .success{color:#86efac}.console .warning{color:#fde68a}.console .cmd{color:#93c5fd}.console .git{color:#cbd5e1}.footer{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;background:rgba(16,24,39,.86);border:1px solid var(--line);border-radius:var(--radius);padding:10px 12px;box-shadow:var(--shadow)}.light .footer{background:rgba(255,255,255,.9)}.footer small{color:var(--muted)}
@media(max-width:1200px){body{overflow:auto}.app{height:auto;overflow:visible}.main{grid-template-columns:1fr}.console-wrap{height:320px}.status-strip{grid-template-columns:repeat(2,1fr)}.setup-grid{grid-template-columns:1fr}.span-2,.span-3,.span-4,.span-5,.span-6,.span-8,.span-12{grid-column:span 1}.title-row{align-items:flex-start;flex-direction:column}.top-actions{justify-content:flex-start}.footer{grid-template-columns:1fr}}


/* ── Modern UI refresh layer ───────────────────────────────────────────── */
body{letter-spacing:.01em;background:
  radial-gradient(circle at 8% 0%,rgba(34,211,238,.13),transparent 28%),
  radial-gradient(circle at 92% 8%,rgba(168,85,247,.16),transparent 32%),
  linear-gradient(145deg,var(--bg),var(--bg2) 58%,#060912)!important;}
body.light{background:linear-gradient(145deg,#f8fafc,#eef2ff 54%,#e0f2fe)!important;}
*{scrollbar-width:thin;scrollbar-color:#475569 transparent}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#475569;border-radius:999px;border:3px solid transparent;background-clip:content-box}::-webkit-scrollbar-track{background:transparent}
.topbar,.panel,.footer{border-color:rgba(148,163,184,.18)!important;background:linear-gradient(180deg,rgba(15,23,42,.88),rgba(15,23,42,.72))!important;box-shadow:0 24px 65px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.04)!important;}
.light .topbar,.light .panel,.light .footer{background:linear-gradient(180deg,rgba(255,255,255,.94),rgba(248,250,252,.86))!important;box-shadow:0 20px 55px rgba(15,23,42,.10),inset 0 1px 0 rgba(255,255,255,.75)!important;}
.logo{position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(145deg,#06b6d4,#2563eb 48%,#8b5cf6)!important;box-shadow:0 14px 32px rgba(37,99,235,.35)!important}.logo:after{content:"";position:absolute;inset:-60%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);transform:rotate(35deg);animation:sheen 4.5s infinite}.logo span{position:relative;z-index:1}@keyframes sheen{0%,55%{translate:-80% 0}75%,100%{translate:80% 0}}
.pro-chip{display:inline-flex;align-items:center;margin-left:7px;padding:2px 8px;border-radius:999px;background:rgba(34,211,238,.12);border:1px solid rgba(34,211,238,.32);color:#67e8f9;font-size:12px;vertical-align:middle}.light .pro-chip{color:#0369a1;background:#e0f2fe;border-color:#7dd3fc}
.subtitle{font-size:12.5px!important;color:#aeb8c8!important}.light .subtitle{color:#475569!important}.top-actions{padding:4px;border:1px solid rgba(148,163,184,.15);border-radius:14px;background:rgba(2,6,23,.22)}.light .top-actions{background:rgba(241,245,249,.75)}
button{border:1px solid rgba(255,255,255,.06)!important;box-shadow:0 6px 16px rgba(0,0,0,.12)}button.ghost{background:rgba(15,23,42,.58)!important;border-color:rgba(148,163,184,.22)!important;color:var(--text)!important}button.ghost:hover{background:rgba(37,99,235,.22)!important;border-color:rgba(96,165,250,.42)!important}.light button.ghost{background:#f8fafc!important;border-color:#cbd5e1!important}
.status-strip{grid-template-columns:repeat(7,minmax(130px,1fr))!important}.pill{border-radius:14px!important;background:rgba(2,6,23,.28)!important;border-color:rgba(148,163,184,.16)!important;padding:10px 12px!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}.light .pill{background:#f8fafc!important}.pill strong{font-weight:900}.dot{box-shadow:0 0 0 4px rgba(148,163,184,.10)}.dot.processing{box-shadow:0 0 18px rgba(168,85,247,.55)}.dot.completed{box-shadow:0 0 18px rgba(34,197,94,.45)}
.setup{background:rgba(2,6,23,.18);border:1px solid rgba(148,163,184,.14)!important;border-radius:16px;margin-top:14px!important;padding:12px!important}.light .setup{background:#f8fafc}.setup summary{display:flex;align-items:center;gap:9px;padding:2px 2px 8px}.setup-grid{background:rgba(15,23,42,.22);border:1px solid rgba(148,163,184,.12);border-radius:16px;padding:12px}.light .setup-grid{background:white}.field label{text-transform:uppercase;letter-spacing:.06em;font-size:11px;color:#93a4bb}input,select{background:rgba(2,6,23,.34)!important;border-color:rgba(148,163,184,.20)!important}.light input,.light select{background:#fff!important;border-color:#cbd5e1!important}.browse-row button{min-width:90px}
.toolbar{padding:8px;border-radius:16px;background:rgba(2,6,23,.20);border:1px solid rgba(148,163,184,.12)}.light .toolbar{background:#f8fafc}.feedback{border-radius:14px!important;background:rgba(2,6,23,.25)!important;border-color:rgba(148,163,184,.15)!important}.light .feedback{background:#fff!important}
.main{grid-template-columns:320px minmax(520px,1fr) 400px!important}.panel{border-radius:20px!important}.panel-head{padding:13px 15px!important;background:linear-gradient(180deg,rgba(15,23,42,.72),rgba(15,23,42,.44))!important;border-bottom-color:rgba(148,163,184,.15)!important}.light .panel-head{background:linear-gradient(180deg,#fff,#f8fafc)!important}.panel-head h2{display:flex;align-items:center;gap:9px;font-size:15px!important}.head-icon{width:28px;height:28px;display:grid;place-items:center;border-radius:10px;background:rgba(59,130,246,.12);border:1px solid rgba(96,165,250,.18)}.head-actions{display:flex;gap:8px;align-items:center}.panel-body{padding:14px!important}
.stats-grid{gap:12px!important}.stat{border-radius:18px!important;background:linear-gradient(180deg,rgba(17,28,46,.98),rgba(15,23,42,.78))!important;border-color:rgba(148,163,184,.15)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}.light .stat{background:linear-gradient(180deg,#fff,#f8fafc)!important}.stat .n{font-size:27px!important;line-height:1}.stat .t{text-transform:uppercase;letter-spacing:.06em;font-size:10.5px!important}.stat:nth-child(2)::after{background:rgba(34,197,94,.11)!important}.stat:nth-child(5)::after{background:rgba(239,68,68,.12)!important}.section-label{font-size:12px;font-weight:950;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:16px 0 9px;display:flex;align-items:center;gap:8px}.mini-chart{height:82px!important;border-radius:16px!important;background:rgba(2,6,23,.26)!important;border-color:rgba(148,163,184,.14)!important}.queue-item{border-radius:14px!important;background:rgba(2,6,23,.24)!important;border-color:rgba(148,163,184,.14)!important}.light .queue-item,.light .mini-chart{background:#fff!important}
.filterbar{grid-template-columns:1fr 150px!important;background:rgba(2,6,23,.20);border:1px solid rgba(148,163,184,.12);padding:9px;border-radius:16px;margin-bottom:13px!important}.light .filterbar{background:#f8fafc}.chunk-grid{grid-template-columns:repeat(auto-fill,minmax(96px,1fr))!important;gap:10px!important}.chunk-cell{min-height:86px!important;border-radius:18px!important;background:linear-gradient(180deg,rgba(17,28,46,.98),rgba(15,23,42,.82))!important;border-color:rgba(148,163,184,.16)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}.light .chunk-cell{background:linear-gradient(180deg,#fff,#f8fafc)!important}.chunk-cell:hover{box-shadow:0 12px 28px rgba(0,0,0,.22),0 0 0 1px rgba(96,165,250,.28)!important}.chunk-cell.selected{outline:none!important;border-color:#e0f2fe!important;box-shadow:0 0 0 2px rgba(224,242,254,.45),0 12px 30px rgba(0,0,0,.25)!important}.chunk-cell.active{animation:pulseActive 1.7s ease-in-out infinite}@keyframes pulseActive{0%,100%{box-shadow:0 0 0 2px rgba(59,130,246,.22)}50%{box-shadow:0 0 0 5px rgba(59,130,246,.08)}}.chunk-no{font-size:18px!important}.chunk-meta{font-size:11.5px!important}.chunk-icon{font-size:15px}.chunk-cell.completed{background:linear-gradient(180deg,rgba(20,83,45,.58),rgba(15,23,42,.78))!important}.chunk-cell.failed{background:linear-gradient(180deg,rgba(127,29,29,.58),rgba(15,23,42,.78))!important}.chunk-cell.processing{background:linear-gradient(180deg,rgba(76,29,149,.60),rgba(15,23,42,.78))!important}body.compact .chunk-cell{min-height:46px!important;border-radius:12px!important}
.detail-card{gap:14px!important}.badge{border:1px solid rgba(255,255,255,.08);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}.meta div{border-radius:16px!important;background:rgba(2,6,23,.22)!important;border-color:rgba(148,163,184,.13)!important}.light .meta div{background:#fff!important}.quick-actions button,.file-tools button{border-radius:999px!important}.files,.tree{border-radius:18px!important;border-color:rgba(148,163,184,.14)!important}.file-head{background:rgba(2,6,23,.30)!important;text-transform:uppercase;letter-spacing:.05em}.light .file-head{background:#f1f5f9!important}.file-row{border-top-color:rgba(148,163,184,.10)!important}.file-row:hover{background:rgba(59,130,246,.08)}
.footer{grid-template-columns:1fr!important}.modern-console{border-radius:18px!important}.console-wrap{height:300px!important}.console-tabs{background:rgba(2,6,23,.25);border:1px solid rgba(148,163,184,.13);padding:5px;border-radius:999px}.light .console-tabs{background:#f8fafc}.tab{border-radius:999px!important;border-color:transparent!important}.tab.active{background:linear-gradient(135deg,#2563eb,#06b6d4)!important}.console{border-radius:0 0 18px 18px;background:linear-gradient(180deg,#030712,#07111f)!important;border-top:1px solid rgba(148,163,184,.12);font-size:12.5px!important}.footer-tip{display:block;margin-top:8px;color:var(--muted)}
@media(max-width:1200px){.main{grid-template-columns:1fr!important}.status-strip{grid-template-columns:repeat(2,1fr)!important}.top-actions{width:100%}.top-actions button{flex:1}.console-wrap{height:340px!important}}

</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="title-row">
      <div class="brand">
        <div class="logo"><span>⚡</span></div>
        <div><h1>Git Chunk Processor Dashboard <span class="pro-chip">Modern</span></h1><div class="subtitle">Polished Unreal-sized Git processing • safer resume • cleaner recovery visibility</div></div>
      </div>
      <div class="top-actions">
        <button class="ghost" onclick="toggleSetup()">⚙️ Settings</button>
        <button class="ghost" onclick="toggleCompact()">▦ Density</button>
        <button class="ghost" onclick="toggleTheme()">🌓 Theme</button>
        <button class="secondary" onclick="exportReport()">⬇ Report</button>
      </div>
    </div>
    <div class="status-strip">
      <div class="pill"><span id="runDot" class="dot pending"></span> Run <strong id="runState">Idle</strong></div>
      <div class="pill">🌿 Branch <strong id="branchPill">-</strong></div>
      <div class="pill">🔗 Remote <strong id="remotePill">-</strong></div>
      <div class="pill">🧩 Mode <strong id="modePill">full</strong></div>
      <div class="pill">⏱ ETA <strong id="etaPill">-</strong></div>
      <div class="pill">📦 Current <strong id="currentPill">-</strong></div>
      <div class="pill">🧠 LFS <strong id="lfsPill">-</strong></div>
    </div>
    <details id="setupBox" class="setup" open>
      <summary><span>📁</span> Workspace, Paths & Processing</summary>
      <div class="setup-grid">
        <div class="field span-6"><label>git_chunks.log</label><div class="browse-row"><input id="logFile" placeholder="Paste / drag-drop git_chunks.log path"><button onclick="browsePath('log')">Browse</button></div></div>
        <div class="field span-6"><label>Git repo folder</label><div class="browse-row"><input id="repoPath" placeholder="Paste / drag-drop Git repository folder path"><button onclick="browsePath('repo')">Browse</button></div></div>
        <div class="field span-6"><label>processed_chunks.json</label><div class="browse-row"><input id="processedPath" placeholder="Select processed_chunks.json location"><button onclick="browsePath('processed')">Browse</button></div></div>
        <div class="field span-6"><label>Logs folder</label><div class="browse-row"><input id="logsDir" placeholder="Optional logs folder"><button onclick="browsePath('logs_dir')">Browse</button></div></div>
        <div class="field span-5"><label>GitHub URL</label><input id="githubUrl" placeholder="https://github.com/owner/repo"></div>
        <div class="field span-2"><label>Start chunk</label><input id="startChunk" type="number" min="1" placeholder="Optional"></div>
        <div class="field span-2"><label>End chunk</label><input id="endChunk" type="number" min="1" placeholder="Optional"></div>
        <div class="field span-3"><label>Pause between chunks</label><input id="pauseBetweenChunks" type="number" min="0" step="0.1" placeholder="Seconds"></div>
        <div class="field span-4"><label>Mode</label><select id="mode" onchange="togglePushEveryN()"><option value="full">Full: Add → Commit → Push each chunk</option><option value="commit_only">Commit Only</option><option value="commit_n_then_push">Batch: Commit N → Push N</option><option value="pipeline">Pipeline: Commit Next While Previous Pushes</option><option value="push_only">Push Only</option><option value="dry_run">Dry Run</option></select></div>
        <div class="field span-2" id="pushEveryNRow" style="display:none"><label>Push every N</label><input id="pushEveryN" type="number" min="1" step="1" value="2"></div>
        <div class="field span-6"><label>Actions</label><div class="toolbar"><button class="secondary" onclick="visualizeOnly()">👁 Visualize Only</button><button class="success" id="startBtn" onclick="startProcessing()">▶ Start</button><button class="warning" id="pauseBtn" onclick="togglePauseProcessing()">⏸ Pause After Current</button><button class="danger" id="stopBtn" onclick="stopProcessing()">■ Stop + Shutdown Safe</button><button class="success" onclick="generateFromGitHub()">📥 Import GitHub</button></div></div>
      </div>
      <div id="actionFeedback" class="feedback">Ready. Pick your repo and visualize chunks before processing.</div>
    </details>
  </header>

  <main class="main">
    <section class="panel">
      <div class="panel-head"><h2><span class="head-icon">📊</span> Command Center</h2><button class="ghost" onclick="openStateJson()">State JSON</button></div>
      <div class="panel-body">
        <div class="stats-grid">
          <div class="stat"><div class="n" id="total">0</div><div class="t">Total Chunks</div></div>
          <div class="stat"><div class="n" id="completed">0</div><div class="t">Completed</div></div>
          <div class="stat"><div class="n" id="processing">0</div><div class="t">Processing</div></div>
          <div class="stat"><div class="n" id="pending">0</div><div class="t">Pending</div></div>
          <div class="stat"><div class="n" id="failed">0</div><div class="t">Failed</div></div>
          <div class="stat"><div class="n" id="skipped">0</div><div class="t">Skipped</div></div>
          <div class="stat wide"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><strong>Overall Progress</strong><span id="overallText">0%</span></div><div class="progress-wrap"><div class="progress-bar" id="overallBar"></div></div></div>
          <div class="stat wide"><div class="t">Estimated Finish</div><div class="n" id="eta">-</div></div>
        </div>
        <div class="section-label"><span>⚡</span> Speed History</div>
        <div class="mini-chart" id="speedChart"></div>
        <div class="section-label"><span>🚦</span> Next Queue</div>
        <div class="queue-list" id="queueList"></div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2><span class="head-icon">🧩</span> Chunk Board</h2><div class="head-actions"><button class="ghost" onclick="showActiveChunk()">🎯 Active</button><button class="ghost" onclick="toggleRefresh()" id="refreshBtn">Pause Refresh</button></div></div>
      <div class="panel-body">
        <div class="filterbar"><input id="chunkSearch" placeholder="Search chunk, status, stage, file path..."><select id="statusFilter"><option value="all">All</option><option value="pending">Pending</option><option value="processing">Processing</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="skipped">Skipped</option></select></div>
        <div id="chunkGrid" class="chunk-grid"></div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2><span class="head-icon">🔎</span> Inspector</h2><button class="ghost" onclick="copySelectedFiles()">Copy Files</button></div>
      <div id="detail" class="panel-body">Select a chunk.</div>
    </section>
  </main>

  <footer class="footer">
    <section class="panel console-wrap modern-console" style="box-shadow:none">
      <div class="panel-head"><div class="console-tabs"><button class="tab active" data-tab="all" onclick="setConsoleTab('all')">All</button><button class="tab" data-tab="git" onclick="setConsoleTab('git')">Git</button><button class="tab" data-tab="error" onclick="setConsoleTab('error')">Errors</button><button class="tab" data-tab="warning" onclick="setConsoleTab('warning')">Warnings</button><button class="tab" data-tab="success" onclick="setConsoleTab('success')">Success</button></div><div style="display:flex;gap:8px"><input id="consoleSearch" style="width:180px" placeholder="Search console"><button class="ghost" onclick="copyConsole()">Copy</button><button class="ghost" onclick="clearConsoleView()">Clear View</button></div></div>
      <div id="console" class="console"></div>
    </section>
    <small class="footer-tip">💡 Closing this browser tab/window sends a shutdown signal and terminates the local dashboard/Git child processes.</small>
  </footer>
</div>
<script>
let state=null, selectedChunk=null, autoRefreshEnabled=true, userInteracting=false;
let chunkCellMap=new Map(), lastConsoleLength=0, lastChunkSignature="", searchText="", statusFilter="all", consoleTab="all", consoleFilter="";
let speedSamples=[]; let _stateVersion="";
function cleanPath(v){v=(v||"").trim();if((v.startsWith('"')&&v.endsWith('"'))||(v.startsWith("'")&&v.endsWith("'")))v=v.substring(1,v.length-1);return v.trim();}
function escapeHtml(str){return String(str??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}
async function api(path,data=null){const opts=data?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}:{};const res=await fetch(path,opts);if(res.status===304)return null;return await res.json();}
function setFeedback(message,level="info"){const box=document.getElementById("actionFeedback");box.textContent=message;box.className="feedback"+(level==="info"?"":" "+level);}
function toggleSetup(){const d=document.getElementById("setupBox");d.open=!d.open;localStorage.setItem("setupOpen",d.open?"1":"0");}
function toggleCompact(){document.body.classList.toggle("compact");localStorage.setItem("compact",document.body.classList.contains("compact")?"1":"0");rebuildChunkGrid();}
function toggleTheme(){document.body.classList.toggle("light");localStorage.setItem("theme",document.body.classList.contains("light")?"light":"dark");}
function initPrefs(){if(localStorage.getItem("compact")==="1")document.body.classList.add("compact");if(localStorage.getItem("theme")==="light")document.body.classList.add("light");if(localStorage.getItem("setupOpen")==="0")document.getElementById("setupBox").open=false;}
["logFile","repoPath","processedPath","logsDir"].forEach(id=>{const el=document.getElementById(id);el.addEventListener("drop",e=>{e.preventDefault();const text=e.dataTransfer.getData("text/plain");if(text){el.value=cleanPath(text);if(id==="repoPath")applyRepoDefaults(el.value);}});el.addEventListener("dragover",e=>e.preventDefault());});
document.addEventListener("mousedown",()=>{userInteracting=true;});document.addEventListener("mouseup",()=>{setTimeout(()=>{if(!window.getSelection().toString())userInteracting=false;},900);});document.addEventListener("selectionchange",()=>{if(window.getSelection().toString())userInteracting=true;});document.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="c")setTimeout(()=>{userInteracting=false;},650);});
document.getElementById("chunkSearch").addEventListener("input",e=>{searchText=e.target.value.toLowerCase().trim();rebuildChunkGrid();});document.getElementById("statusFilter").addEventListener("change",e=>{statusFilter=e.target.value||"all";rebuildChunkGrid();});document.getElementById("consoleSearch").addEventListener("input",e=>{consoleFilter=e.target.value.toLowerCase().trim();renderConsoleAll();});
function toggleRefresh(){autoRefreshEnabled=!autoRefreshEnabled;document.getElementById("refreshBtn").textContent=autoRefreshEnabled?"Pause Refresh":"Resume Refresh";}
async function applyRepoDefaults(repoPath){const repo=cleanPath(repoPath);if(!repo)return;const res=await api("/repo_defaults",{repo_path:repo});if(!res||!res.ok){setFeedback((res&&res.message)||"Could not create repo-local dashboard paths.","error");return;}document.getElementById("repoPath").value=res.paths.repo_path;document.getElementById("logFile").value=res.paths.log_file;document.getElementById("processedPath").value=res.paths.processed_chunks_file;document.getElementById("logsDir").value=res.paths.logs_dir;setFeedback("Repository selected. Dashboard paths updated automatically.","success");}
document.getElementById("repoPath").addEventListener("change",e=>applyRepoDefaults(e.target.value));document.getElementById("repoPath").addEventListener("blur",e=>applyRepoDefaults(e.target.value));
async function browsePath(kind){const res=await api("/browse?kind="+encodeURIComponent(kind));if(!res||!res.path)return;if(kind==="log")document.getElementById("logFile").value=res.path;if(kind==="repo"){document.getElementById("repoPath").value=res.path;await applyRepoDefaults(res.path);}if(kind==="processed")document.getElementById("processedPath").value=res.path;if(kind==="logs_dir")document.getElementById("logsDir").value=res.path;}
function payloadBase(){return{log_file:cleanPath(document.getElementById("logFile").value),repo_path:cleanPath(document.getElementById("repoPath").value),processed_chunks_file:cleanPath(document.getElementById("processedPath").value),logs_dir:cleanPath(document.getElementById("logsDir").value),start_chunk:cleanPath(document.getElementById("startChunk").value),end_chunk:cleanPath(document.getElementById("endChunk").value)}}
async function visualizeOnly(){setFeedback("Visualizing chunks without Git commands...","info");const res=await api("/visualize",payloadBase());if(!res.ok){setFeedback(res.message||"Failed to visualize chunks","error");alert(res.message||"Failed to visualize chunks");}else setFeedback("Chunks visualized. No Git command was executed.","success");await refreshState(true);}
async function startProcessing(){const btn=document.getElementById("startBtn");btn.disabled=true;btn.textContent="Starting…";setFeedback("Starting chunk processing…","info");const payload={...payloadBase(),mode:document.getElementById("mode").value,pause_between_chunks:cleanPath(document.getElementById("pauseBetweenChunks").value),push_every_n:cleanPath(document.getElementById("pushEveryN").value)||"1"};try{const res=await api("/start",payload);if(!res.ok){setFeedback(res.message||"Failed to start","error");alert(res.message||"Failed to start");}else{setFeedback("Processing started. Live dashboard is tracking chunks.","success");document.getElementById("setupBox").open=false;}await refreshState(true);}finally{btn.disabled=false;btn.textContent="▶ Start";}}
async function stopProcessing(){const btn=document.getElementById("stopBtn");btn.disabled=true;btn.textContent="Stopping…";setFeedback("Stop requested. Active chunk will stop safely if possible.","warning");try{await api("/stop",{});await refreshState(true);}finally{setTimeout(()=>{btn.disabled=false;btn.textContent="■ Stop + Shutdown Safe";},900);}}
async function togglePauseProcessing(){await stopProcessing();}
async function generateFromGitHub(){const githubUrl=document.getElementById("githubUrl").value.trim();if(!githubUrl){setFeedback("Please enter a GitHub repository URL","error");return;}setFeedback("Fetching commits from GitHub...","info");try{const res=await api("/generate-from-github",{github_url:githubUrl});if(res.ok){setFeedback("✅ "+res.message,"success");alert(`Success! Generated ${res.count} chunks from GitHub commits.`);}else setFeedback("❌ "+res.message,"error");}catch(e){setFeedback("Error: "+e.message,"error");}}
function togglePushEveryN(){const mode=document.getElementById("mode").value;document.getElementById("pushEveryNRow").style.display=(mode==="commit_n_then_push")?"flex":"none";}
async function openProcessed(){const res=await api("/open",{path:cleanPath(document.getElementById("processedPath").value)});if(!res.ok)alert(res.message||"Could not open path");}
async function openStateJson(){const res=await api("/open_state",{});if(!res.ok)alert(res.message||"Could not open state JSON");}
function setConsoleTab(tab){consoleTab=tab;document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));renderConsoleAll();}
function consoleLineVisible(l){if(consoleTab!=="all"&&l.level!==consoleTab)return false;if(consoleFilter&&!(`[${l.time}] ${l.message}`.toLowerCase().includes(consoleFilter)))return false;return true;}
function renderConsoleAll(){const con=document.getElementById("console");con.innerHTML="";(state?.console||[]).filter(consoleLineVisible).forEach(l=>{const div=document.createElement("div");div.className=l.level||"info";div.textContent=`[${l.time}] ${l.message}`;con.appendChild(div);});con.scrollTop=con.scrollHeight;lastConsoleLength=(state?.console||[]).length;}
function clearConsoleView(){document.getElementById("console").innerHTML="";lastConsoleLength=(state?.console||[]).length;}
async function copyConsole(){const text=(state?.console||[]).filter(consoleLineVisible).map(l=>`[${l.time}] ${l.level}: ${l.message}`).join("\n");await navigator.clipboard.writeText(text);setFeedback("Console copied to clipboard.","success");}
async function copySelectedFiles(){const c=selectedChunkData();if(!c)return;const text=(c.files||[]).map(f=>f.path).join("\n");await navigator.clipboard.writeText(text);setFeedback(`Copied ${c.files?.length||0} files from Chunk #${c.number}.`,"success");}
function exportReport(){if(!state){alert("No state loaded yet.");return;}const s=state.stats||{};const lines=["Git Chunk Processor Report",`Generated: ${new Date().toLocaleString()}`,`Mode: ${state.mode}`,`Repo: ${state.repo_path}`,`Branch: ${state.git?.branch||"-"}`,`Remote: ${state.git?.remote||"-"}`,`Total: ${s.total||0}`,`Completed: ${s.completed||0}`,`Pending: ${s.pending||0}`,`Failed: ${s.failed||0}`,`Skipped: ${s.skipped||0}`,`ETA: ${state.eta||"-"}`,"", "Failed Chunks:"];(state.chunks||[]).filter(c=>c.status==="failed").forEach(c=>lines.push(`#${c.number} ${c.stage} ${c.error||""}`));const blob=new Blob([lines.join("\n")],{type:"text/plain"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download="git_chunk_processor_report.txt";a.click();URL.revokeObjectURL(url);}
async function refreshState(force=false){if(!force&&(!autoRefreshEnabled||userInteracting))return;try{const headers={};if(_stateVersion&&!force)headers["X-State-Version"]=_stateVersion;const res=await fetch("/state",{headers});if(res.status===304)return;const newState=await res.json();if(newState._version!==undefined)_stateVersion=String(newState._version);const first=!state;state=newState;if(first){document.getElementById("logFile").value=state.log_file||"";document.getElementById("repoPath").value=state.repo_path||"";document.getElementById("processedPath").value=state.processed_chunks_file||"";document.getElementById("logsDir").value=state.logs_dir||"";document.getElementById("startChunk").value=state.start_chunk||"";document.getElementById("endChunk").value=state.end_chunk||"";document.getElementById("pauseBetweenChunks").value=state.pause_between_chunks||"";document.getElementById("mode").value=state.mode||"full";if(state.push_every_n)document.getElementById("pushEveryN").value=state.push_every_n;togglePushEveryN();}updateTopStatus();updateStats();updateQueue();updateSpeedChart();updateChunkGridStable();updateDetailStable();updateConsoleStable();}catch(e){console.error("Refresh error",e);}}
function updateTopStatus(){const g=state.git||{};const running=!!state.running;document.getElementById("runState").textContent=running?"Running":(state.stop_requested?"Stopped":"Idle");const dot=document.getElementById("runDot");dot.className="dot "+(running?"processing":(state.last_error?"failed":"completed"));document.getElementById("branchPill").textContent=g.branch||"-";document.getElementById("remotePill").textContent=(g.remote||"-").split(/\s+/)[0];document.getElementById("modePill").textContent=state.mode||"-";document.getElementById("etaPill").textContent=state.eta||"-";document.getElementById("currentPill").textContent=state.current_chunk?`#${state.current_chunk}`:"-";document.getElementById("lfsPill").textContent=g.lfs||"-";}
function updateStats(){const s=state.stats||{};["total","completed","processing","pending","failed","skipped"].forEach(k=>document.getElementById(k).textContent=s[k]??0);document.getElementById("eta").textContent=state.eta||"-";document.getElementById("overallBar").style.width=(state.overall_progress||0)+"%";document.getElementById("overallText").textContent=(state.overall_progress||0)+"%";}
function updateQueue(){const box=document.getElementById("queueList");const chunks=state.chunks||[];let items=[];if(state.current_chunk){const cur=chunks.find(c=>c.number===state.current_chunk);if(cur)items.push(cur);}items=items.concat(chunks.filter(c=>c.status==="pending").slice(0,5));if(!items.length){box.innerHTML='<div class="queue-item"><strong>No queued chunks</strong><span class="queue-stage">Idle</span></div>';return;}box.innerHTML=items.map(c=>`<div class="queue-item"><strong>#${c.number}</strong><span class="queue-stage">${escapeHtml(c.stage||c.status||"pending")}</span></div>`).join("");}
function updateSpeedChart(){const done=(state.stats?.completed||0)+(state.stats?.skipped||0);const last=speedSamples.length?speedSamples[speedSamples.length-1].done:null;if(last!==done)speedSamples.push({t:Date.now(),done});speedSamples=speedSamples.slice(-24);const max=Math.max(1,...speedSamples.map((p,i)=>i?Math.max(0,p.done-speedSamples[i-1].done):0));document.getElementById("speedChart").innerHTML=speedSamples.map((p,i)=>{const d=i?Math.max(0,p.done-speedSamples[i-1].done):0;const h=8+(d/max)*58;return `<div class="bar" title="+${d} chunks" style="height:${h}px"></div>`;}).join("");}
function chunkVisible(c){if(statusFilter!=="all"&&String(c.status||"pending")!==statusFilter)return false;if(!searchText)return true;if(String(c.number).includes(searchText))return true;if(String(c.status||"").toLowerCase().includes(searchText))return true;if(String(c.stage||"").toLowerCase().includes(searchText))return true;return(c.files||[]).some(f=>(f.path||"").toLowerCase().includes(searchText));}
function chunkSignature(){return(state.chunks||[]).filter(chunkVisible).map(c=>`${c.number}:${c.status}:${c.progress}:${c.stage}:${state.current_chunk===c.number}:${selectedChunk===c.number}`).join("|");}
function rebuildChunkGrid(){const grid=document.getElementById("chunkGrid");grid.innerHTML="";chunkCellMap.clear();(state?.chunks||[]).filter(chunkVisible).forEach(c=>{const cell=document.createElement("div");cell.className="chunk-cell";cell.dataset.chunk=c.number;cell.innerHTML=`<div class="chunk-icon">${statusIcon(c.status)}</div><div class="chunk-no">#${String(c.number).padStart(3,"0")}</div><div class="chunk-meta"><span>${c.file_count||c.files?.length||0} files</span><span>${Number(c.size_mb||0).toFixed(1)} MB</span><span>${escapeHtml(c.stage||c.status||"")}</span></div><div class="mini"></div>`;cell.addEventListener("click",()=>{selectedChunk=c.number;updateChunkGridStable(true);updateDetailStable(true);});grid.appendChild(cell);chunkCellMap.set(c.number,cell);});lastChunkSignature="";updateChunkGridStable(true);}
function statusIcon(s){return s==="completed"?"✅":s==="failed"?"❌":s==="processing"?"🟣":s==="skipped"?"⏭️":"🟡";}
function updateChunkGridStable(force=false){const sig=chunkSignature();if(!force&&sig===lastChunkSignature)return;const visible=(state.chunks||[]).filter(chunkVisible);if(visible.length!==chunkCellMap.size){rebuildChunkGrid();return;}visible.forEach(c=>{const cell=chunkCellMap.get(c.number);if(!cell){rebuildChunkGrid();return;}cell.className="chunk-cell";if(c.status)cell.classList.add(c.status);if(state.current_chunk===c.number)cell.classList.add("active");if(selectedChunk===c.number)cell.classList.add("selected");cell.title=`Chunk #${c.number} - ${c.status} - ${c.stage}`;const mini=cell.querySelector(".mini");if(mini)mini.style.width=(c.progress||0)+"%";});lastChunkSignature=sig;if(!selectedChunk&&state.current_chunk)selectedChunk=state.current_chunk;if(!selectedChunk&&visible.length)selectedChunk=visible[0].number;}
function selectedChunkData(){const chunks=state.chunks||[];if(!selectedChunk&&state.current_chunk)selectedChunk=state.current_chunk;if(!selectedChunk&&chunks.length)selectedChunk=chunks[0].number;return chunks.find(c=>c.number===selectedChunk)||null;}
let lastDetailSig="";function updateDetailStable(force=false){const c=selectedChunkData(),detail=document.getElementById("detail");if(!c){detail.textContent="No chunk selected.";return;}const sig=`${c.number}:${c.status}:${c.stage}:${c.progress}:${c.error}:${c.files?.length}:${c.push_output?.length}`;if(!force&&sig===lastDetailSig)return;lastDetailSig=sig;const files=c.files||[];const tree=folderTree(files);const rows=files.slice(0,450).map((f,i)=>`<div class="file-row"><div>${i+1}</div><div title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div><div title="${escapeHtml(f.folder)}">${escapeHtml(f.folder)}</div><div>${Number(f.size_mb||0).toFixed(2)} MB</div><div class="file-status">${escapeHtml(f.status||"pending")}</div></div>`).join("");detail.innerHTML=`<div class="detail-card"><div class="chunk-title"><div><h2 style="margin:0">Chunk #${c.number}</h2><div style="color:var(--muted);font-size:12px;margin-top:4px">Stage: <strong>${escapeHtml(c.stage||"-")}</strong></div></div><div class="badge ${escapeHtml(c.status)}">${statusIcon(c.status)} ${escapeHtml(c.status||"pending")}</div></div><div class="meta"><div><small>Files</small><strong>${files.length}</strong></div><div><small>Size</small><strong>${Number(c.size_mb||0).toFixed(2)} MB</strong></div><div><small>Duration</small><strong>${c.duration_seconds?c.duration_seconds+"s":"-"}</strong></div></div><div class="progress-wrap"><div class="progress-bar" style="width:${c.progress||0}%"></div></div>${c.error?`<div class="errorbox">${escapeHtml(c.error)}</div>`:""}<div class="quick-actions"><button class="ghost" onclick="copySelectedFiles()">Copy File List</button><button class="ghost" onclick="showFileTable()">Table</button><button class="ghost" onclick="showFileTree()">Tree</button><button class="ghost" onclick="openProcessed()">Open Processed JSON</button></div><div id="fileTable" class="files"><div class="file-head"><div>#</div><div>File</div><div>Folder</div><div>Size</div><div>Status</div></div>${rows}${files.length>450?`<div class="file-row"><div>…</div><div>Showing first 450 files</div><div></div><div></div><div></div></div>`:""}</div><div id="fileTree" class="tree">${tree}</div>${c.push_output?`<h3>Push Output</h3><pre class="errorbox">${escapeHtml(c.push_output)}</pre>`:""}</div>`;}
function folderTree(files){const map=new Map();files.forEach(f=>{const folder=f.folder||".";map.set(folder,(map.get(folder)||0)+1);});return Array.from(map.entries()).sort((a,b)=>b[1]-a[1]).slice(0,120).map(([folder,count])=>`<div class="tree-row">📁 ${escapeHtml(folder)} <strong style="float:right">${count}</strong></div>`).join("")||'<div class="tree-row">No files</div>';}
function showFileTree(){const t=document.getElementById("fileTree"),f=document.getElementById("fileTable");if(t&&f){t.style.display="block";f.style.display="none";}}
function showFileTable(){const t=document.getElementById("fileTree"),f=document.getElementById("fileTable");if(t&&f){t.style.display="none";f.style.display="block";}}
function updateConsoleStable(){const lines=state.console||[];if(lines.length===lastConsoleLength)return;renderConsoleAll();}
function showActiveChunk(){if(!state||!state.current_chunk){setFeedback("No active chunk right now.","warning");return;}selectedChunk=state.current_chunk;document.getElementById("chunkSearch").value="";searchText="";document.getElementById("statusFilter").value="all";statusFilter="all";rebuildChunkGrid();updateChunkGridStable(true);updateDetailStable(true);const cell=chunkCellMap.get(selectedChunk);if(cell)cell.scrollIntoView({behavior:"smooth",block:"center",inline:"center"});}
let shutdownSent=false;function requestShutdown(){if(shutdownSent)return;shutdownSent=true;try{navigator.sendBeacon("/shutdown","");}catch(e){fetch("/shutdown",{method:"POST",keepalive:true}).catch(()=>{});}}
window.addEventListener("pagehide",requestShutdown);window.addEventListener("beforeunload",requestShutdown);
initPrefs();setInterval(()=>refreshState(false),1500);refreshState(true);
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

        if parsed.path == "/generate-from-github":
            payload = self._read_json()
            repo_url = clean_pasted_path(payload.get("github_url", "")).strip()
            if not repo_url:
                return self._send_json({"ok": False, "message": "GitHub URL required."})
            
            result = generate_processed_chunks_from_github(repo_url)
            if result["success"]:
                return self._send_json({
                    "ok": True,
                    "message": f"Generated from {result['commits_found']} commits → {result['count']} chunks",
                    "chunks": result["data"],
                    "count": result["count"]
                })
            else:
                return self._send_json({"ok": False, "message": result["data"]})

        if parsed.path == "/start":
            payload = self._read_json()
            ok, msg = start_processing(payload)
            return self._send_json({"ok": ok, "message": msg})

        if parsed.path == "/stop":
            stop_processing()
            return self._send_json({"ok": True})

        if parsed.path == "/pause":
            ok, msg = toggle_pause_processing()
            return self._send_json({"ok": ok, "message": msg})

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
            self._send_json({"ok": True, "message": "Dashboard shutdown requested."})
            # Browser close should terminate the local Python/exe process, not only stop processing.
            threading.Thread(target=cleanup_and_shutdown, kwargs={"force_exit": True}, daemon=False).start()
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