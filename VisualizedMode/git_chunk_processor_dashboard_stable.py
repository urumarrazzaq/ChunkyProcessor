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
import webbrowser
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_NAME = "Git Chunk Processor Dashboard"
HOST = "127.0.0.1"
PORT = 8765

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = SCRIPT_DIR / "chunk_processor_state.json"
DEFAULT_PROCESSED_PATH = SCRIPT_DIR / "processed_chunks.json"
DEFAULT_LOGS_DIR = SCRIPT_DIR / "logs"

STATE_LOCK = threading.Lock()
PROCESS_THREAD = None
STOP_REQUESTED = False
CONSOLE_MAX_LINES = 800


def now_text():
    return datetime.now().strftime("%H:%M:%S")


def clean_pasted_path(value: str) -> str:
    if value is None:
        return ""
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value.strip()


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


def save_state():
    with STATE_LOCK:
        DEFAULT_STATE_PATH.write_text(json.dumps(STATE, indent=2), encoding="utf-8")


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


def get_state_copy():
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def load_processed_chunks(path):
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {int(x) for x in data}
    except Exception:
        return set()


def save_processed_chunk(path, chunk_number):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    processed = load_processed_chunks(p)
    processed.add(int(chunk_number))
    p.write_text(json.dumps(sorted(processed), indent=2), encoding="utf-8")


def parse_chunks(log_file_path):
    chunks = []
    current = None

    chunk_re = re.compile(r"Chunk\s+#(\d+)\s+\((\d+)\s+files?,\s+([\d.]+)MB\):", re.IGNORECASE)
    file_re = re.compile(r"^\s*-\s+(.+?)(?:\s+\(([\d.]+)MB\))?\s*$")

    with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")
            cm = chunk_re.search(line)
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
                fm = file_re.match(line)
                if fm:
                    file_path = fm.group(1).strip()
                    size_mb = float(fm.group(2)) if fm.group(2) else 0.0
                    current["files"].append({
                        "path": file_path,
                        "name": Path(file_path).name,
                        "folder": str(Path(file_path).parent),
                        "size_mb": size_mb,
                        "status": "pending"
                    })

    if current:
        chunks.append(current)

    return chunks


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
        for c in STATE["chunks"]:
            if c["number"] == chunk_number:
                c.update(updates)
                break
        recalc_stats_unlocked()
    save_state()


def set_file_status(chunk_number, file_path, status):
    with STATE_LOCK:
        for c in STATE["chunks"]:
            if c["number"] == chunk_number:
                for f in c["files"]:
                    if f["path"] == file_path:
                        f["status"] = status
                        break
                break
    save_state()


def run_cmd(cmd, cwd=None, stream=False):
    add_console(f"$ {' '.join(cmd)}", "cmd")

    if stream:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False
        )
        output_lines = []
        while True:
            line = proc.stdout.readline()
            if line:
                text = line.rstrip()
                output_lines.append(text)
                add_console(text, "git")
            if line == "" and proc.poll() is not None:
                break
        return proc.returncode, "\n".join(output_lines)

    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False
    )
    out = (result.stdout or "") + (result.stderr or "")
    if out.strip():
        for line in out.splitlines():
            add_console(line, "git")
    return result.returncode, out


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

    if mode not in {"full", "commit_only", "push_only", "dry_run"}:
        return None, "Invalid processing mode."

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
        "mode": mode
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
        current_chunk=None,
        last_error=""
    )

    add_console("Processing started.", "info")
    add_console(f"Mode: {config['mode']}", "info")
    add_console(f"Repository: {config['repo_path']}", "info")

    try:
        inspect_git(config["repo_path"])

        if config["mode"] == "push_only":
            add_console("Push-only mode selected. Running git push once.", "info")
            with STATE_LOCK:
                STATE["chunks"] = []
                recalc_stats_unlocked()
            save_state()
            code, _ = run_cmd(["git", "push"], cwd=Path(config["repo_path"]), stream=True)
            if code == 0:
                add_console("Push completed successfully.", "success")
            else:
                add_console("Push failed.", "error")
                update_state(last_error="Push failed.")
            return

        chunks = parse_chunks(config["log_file"])
        processed = load_processed_chunks(config["processed_chunks_file"])

        for c in chunks:
            if c["number"] in processed:
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
            recalc_stats_unlocked()
        save_state()

        add_console(f"Parsed {len(chunks)} chunks.", "success")

        repo = Path(config["repo_path"])
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
            for f in chunk["files"]:
                full = repo / f["path"]
                if full.exists():
                    set_file_status(n, f["path"], "found")
                else:
                    set_file_status(n, f["path"], "missing")
                    missing.append(f["path"])

            if missing:
                msg = f"Chunk #{n} failed: {len(missing)} missing files."
                set_chunk_status(n, status="failed", stage="missing_files", progress=100, error=msg)
                add_console(msg, "error")
                continue

            if config["mode"] == "dry_run":
                set_chunk_status(n, status="completed", stage="dry_run_done", progress=100)
                add_console(f"[DRY RUN] Chunk #{n}: would add, commit, and maybe push depending on mode.", "success")
                save_processed_chunk(config["processed_chunks_file"], n)
                continue

            set_chunk_status(n, stage="adding", progress=20)
            files = [f["path"] for f in chunk["files"]]
            batch_size = 50
            total_files = len(files)
            added_count = 0

            for i in range(0, total_files, batch_size):
                if STOP_REQUESTED:
                    break
                batch = files[i:i + batch_size]
                code, _ = run_cmd(["git", "add", "--"] + batch, cwd=repo)
                if code != 0:
                    msg = f"git add failed in Chunk #{n}."
                    set_chunk_status(n, status="failed", stage="add_failed", progress=100, error=msg)
                    add_console(msg, "error")
                    break
                added_count += len(batch)
                for fp in batch:
                    set_file_status(n, fp, "added")
                pct = 20 + int((added_count / max(total_files, 1)) * 35)
                set_chunk_status(n, progress=min(pct, 55))

            current = get_state_copy()
            chunk_state = next((c for c in current["chunks"] if c["number"] == n), None)
            if chunk_state and chunk_state.get("status") == "failed":
                continue

            if STOP_REQUESTED:
                add_console("Stop requested after git add.", "warning")
                break

            set_chunk_status(n, stage="committing", progress=65)
            commit_message = f"Chunk #{n} - {len(files)} files"
            code, out = run_cmd(["git", "commit", "-m", commit_message], cwd=repo)

            if code != 0:
                lower = out.lower()
                if "nothing to commit" in lower or "no changes added" in lower:
                    set_chunk_status(n, status="skipped", stage="nothing_to_commit", progress=100)
                    save_processed_chunk(config["processed_chunks_file"], n)
                    add_console(f"Chunk #{n}: nothing to commit. Marked processed.", "warning")
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
                continue

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
                avg = sum(completed_durations) / len(completed_durations) if completed_durations else 0
                STATE["average_chunk_seconds"] = round(avg, 2)
                eta_seconds = int(avg * remaining)
                STATE["eta"] = format_seconds(eta_seconds) if eta_seconds > 0 else "-"
            save_state()

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
    update_state(stop_requested=True)
    add_console("Stop requested by user.", "warning")
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
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
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
button{background:#2563eb;color:white;border:0;border-radius:8px;padding:9px 12px;font-weight:700;cursor:pointer}
button:hover{background:#1d4ed8}
button.secondary{background:#334155}
button.secondary:hover{background:#475569}
button.danger{background:#dc2626}
button.success{background:#16a34a}
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
.console{height:300px;overflow:auto;background:#050b14;color:#dbeafe;font-family:Consolas,monospace;font-size:12px;padding:12px;line-height:1.45;user-select:text}
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

  <div class="toolbar">
    <select id="mode">
      <option value="full">Full Process: Add → Commit → Push</option>
      <option value="commit_only">Commit Only: Add → Commit</option>
      <option value="push_only">Push Only</option>
      <option value="dry_run">Dry Run: Simulation Only</option>
    </select>
    <button class="success" onclick="startProcessing()">Start Processing</button>
    <button class="danger" onclick="stopProcessing()">Stop</button>
    <button class="secondary" id="refreshBtn" onclick="toggleRefresh()">Pause Auto Refresh</button>
    <button class="secondary" onclick="openProcessed()">Open Processed JSON</button>
  </div>

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
    </div>
    <div id="chunkGrid" class="chunk-grid"></div>
  </section>

  <section class="panel">
    <h2>Selected Chunk Details</h2>
    <div id="detail" class="detail">Select a chunk.</div>
    <h2>Live Console</h2>
    <div id="console" class="console"></div>
  </section>
</div>

<script>
let state = null;
let selectedChunk = null;
let autoRefreshEnabled = true;
let userInteracting = false;
let chunkCellMap = new Map();
let lastConsoleLength = 0;
let lastChunkSignature = "";
let searchText = "";

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
    if (text) el.value = cleanPath(text);
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

function toggleRefresh(){
  autoRefreshEnabled = !autoRefreshEnabled;
  document.getElementById("refreshBtn").textContent = autoRefreshEnabled ? "Pause Auto Refresh" : "Resume Auto Refresh";
}

async function api(path, data=null){
  const opts = data ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)} : {};
  const res = await fetch(path, opts);
  return await res.json();
}

async function browsePath(kind){
  const res = await api("/browse?kind=" + encodeURIComponent(kind));
  if (!res.path) return;
  if (kind === "log") document.getElementById("logFile").value = res.path;
  if (kind === "repo") document.getElementById("repoPath").value = res.path;
  if (kind === "processed") document.getElementById("processedPath").value = res.path;
  if (kind === "logs_dir") document.getElementById("logsDir").value = res.path;
}

async function startProcessing(){
  const payload = {
    log_file: cleanPath(document.getElementById("logFile").value),
    repo_path: cleanPath(document.getElementById("repoPath").value),
    processed_chunks_file: cleanPath(document.getElementById("processedPath").value),
    logs_dir: cleanPath(document.getElementById("logsDir").value),
    mode: document.getElementById("mode").value
  };
  const res = await api("/start", payload);
  if (!res.ok) alert(res.message || "Failed to start");
  await refreshState(true);
}

async function stopProcessing(){
  await api("/stop", {});
}

async function openProcessed(){
  const path = cleanPath(document.getElementById("processedPath").value);
  const res = await api("/open", {path});
  if (!res.ok) alert(res.message || "Could not open path");
}

async function refreshState(force=false){
  if (!force && (!autoRefreshEnabled || userInteracting)) return;
  const newState = await api("/state");
  const first = !state;
  state = newState;

  if (first) {
    document.getElementById("logFile").value = state.log_file || "";
    document.getElementById("repoPath").value = state.repo_path || "";
    document.getElementById("processedPath").value = state.processed_chunks_file || "";
    document.getElementById("logsDir").value = state.logs_dir || "";
    document.getElementById("mode").value = state.mode || "full";
  }

  updateStats();
  updateChunkGridStable();
  updateDetailStable();
  updateConsoleStable();
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
  if (!searchText) return true;
  if (String(c.number).includes(searchText)) return true;
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
            return self._send_json(get_state_copy())

        if parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            kind = qs.get("kind", [""])[0]
            path = browse_dialog(kind)
            return self._send_json({"path": path})

        return self._send_json({"ok": False, "message": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)

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

        return self._send_json({"ok": False, "message": "Not found"}, 404)


def main():
    DEFAULT_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not DEFAULT_STATE_PATH.exists():
        save_state()

    url = f"http://{HOST}:{PORT}"
    print(APP_NAME)
    print(f"Opening dashboard: {url}")
    print("Press Ctrl+C to stop server.")

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def open_browser():
        time.sleep(0.7)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
