#!/usr/bin/env python3
"""
Git Chunk Processor - Web UI Version

Run this script, it opens a browser dashboard.
Enter the git_chunks.log path and repo path inside the UI, then press Start.

Outputs are saved beside this script:
- chunk_processor_dashboard_runtime.json
- processed_chunks.json
- logs/
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_JSON = SCRIPT_DIR / "chunk_processor_dashboard_runtime.json"
PROCESSED_JSON = SCRIPT_DIR / "processed_chunks.json"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

STATUS_IDLE = "idle"
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SKIPPED = "skipped"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry_run"

APP_LOCK = threading.Lock()
PROCESS_THREAD: Optional[threading.Thread] = None
STOP_REQUESTED = False
APP_STATE: Dict = {}


@dataclass
class ChunkFile:
    path: str
    size_mb: Optional[float] = None
    exists: Optional[bool] = None
    add_status: str = "waiting"


@dataclass
class Chunk:
    number: int
    declared_file_count: int
    size_mb: float
    files: List[ChunkFile] = field(default_factory=list)
    status: str = STATUS_PENDING
    message: str = "Waiting"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    added_files: int = 0
    missing_files: int = 0
    commit_hash: Optional[str] = None
    stage: str = "Waiting"
    progress_pct: int = 0
    push_output: str = ""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_path(value: str) -> Path:
    text = (value or "").strip().strip('"').strip("'")
    text = text.replace("file:///", "")
    text = text.replace("file://", "")
    return Path(text)


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"process_chunks_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root.addHandler(console_handler)
    root.addHandler(file_handler)
    logging.info("Logging initialized: %s", log_path)
    return log_path


def parse_chunks(log_file_path: Path) -> List[Chunk]:
    chunks: List[Chunk] = []
    current: Optional[Chunk] = None
    chunk_re = re.compile(r"^Chunk #(\d+)\s+\((\d+)\s+files?,\s+([\d.]+)MB\):")
    file_re = re.compile(r"^\s*-\s+(.+?)(?:\s+\(([\d.]+)MB\))?\s*$")

    with log_file_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")
            cm = chunk_re.search(line.strip())
            if cm:
                if current:
                    chunks.append(current)
                current = Chunk(
                    number=int(cm.group(1)),
                    declared_file_count=int(cm.group(2)),
                    size_mb=float(cm.group(3)),
                )
                continue

            if current:
                fm = file_re.search(line)
                if fm:
                    current.files.append(
                        ChunkFile(
                            path=fm.group(1).strip(),
                            size_mb=float(fm.group(2)) if fm.group(2) else None,
                        )
                    )

    if current:
        chunks.append(current)
    return chunks


def load_processed_chunks() -> set:
    if not PROCESSED_JSON.exists():
        return set()
    try:
        return {int(x) for x in json.loads(PROCESSED_JSON.read_text(encoding="utf-8"))}
    except Exception:
        return set()


def save_processed_chunks(done: set) -> None:
    PROCESSED_JSON.write_text(json.dumps(sorted(done), indent=2), encoding="utf-8")


def check_repo(repo_path: Path) -> None:
    if not repo_path.exists() or not repo_path.is_dir():
        raise FileNotFoundError(f"Repository folder not found: {repo_path}")
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"This folder is not a Git repository: {repo_path}")


def mark_file_existence(chunks: List[Chunk], repo_path: Path) -> None:
    for chunk in chunks:
        missing = 0
        for item in chunk.files:
            exists = (repo_path / item.path).exists()
            item.exists = exists
            item.add_status = "ready" if exists else "missing"
            if not exists:
                missing += 1
        chunk.missing_files = missing
        chunk.added_files = len(chunk.files) - missing


def run_git(repo_path: Path, args: List[str], dry_run: bool = False) -> Tuple[bool, str]:
    if dry_run:
        return True, "dry-run: git " + " ".join(args)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "Git was not found. Install Git or add it to PATH."
    except Exception as e:
        return False, str(e)


def run_git_push_stream(repo_path: Path, dry_run: bool, chunk: Chunk, chunks: List[Chunk], meta: Dict) -> Tuple[bool, str]:
    if dry_run:
        chunk.push_output = "dry-run: git push --progress"
        save_state(chunks, meta)
        return True, chunk.push_output
    try:
        process = subprocess.Popen(
            ["git", "push", "--progress"],
            cwd=str(repo_path),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        lines = []
        if process.stdout:
            for line in process.stdout:
                clean = line.strip()
                if clean:
                    lines.append(clean)
                    chunk.push_output = "\n".join(lines[-80:])
                    # Git push rarely gives reliable percentage, so we animate inside push stage.
                    chunk.progress_pct = max(chunk.progress_pct, min(95, 75 + len(lines)))
                    chunk.message = clean
                    save_state(chunks, meta)
        rc = process.wait()
        out = "\n".join(lines)
        return rc == 0, out
    except FileNotFoundError:
        return False, "Git was not found. Install Git or add it to PATH."
    except Exception as e:
        return False, str(e)


def last_commit_hash(repo_path: Path) -> Optional[str]:
    ok, out = run_git(repo_path, ["rev-parse", "--short", "HEAD"], False)
    return out.strip() if ok and out else None


def build_state(chunks: List[Chunk], meta: Dict) -> Dict:
    counts = {s: 0 for s in [STATUS_PENDING, STATUS_PROCESSING, STATUS_SKIPPED, STATUS_SUCCESS, STATUS_FAILED, STATUS_DRY_RUN]}
    for c in chunks:
        counts[c.status] = counts.get(c.status, 0) + 1
    total_files = sum(len(c.files) for c in chunks)
    total_size = round(sum(c.size_mb for c in chunks), 2)
    completed = counts.get(STATUS_SUCCESS, 0) + counts.get(STATUS_SKIPPED, 0) + counts.get(STATUS_DRY_RUN, 0)
    overall_pct = round((completed / len(chunks)) * 100) if chunks else 0
    active = next((c.number for c in chunks if c.status == STATUS_PROCESSING), None)
    return {
        "app_status": meta.get("app_status", STATUS_IDLE),
        "generated_at": now_text(),
        "repo_path": meta.get("repo_path", ""),
        "log_file": meta.get("log_file", ""),
        "dry_run": bool(meta.get("dry_run", False)),
        "push": bool(meta.get("push", True)),
        "start_chunk": meta.get("start_chunk"),
        "end_chunk": meta.get("end_chunk"),
        "active_chunk": active,
        "message": meta.get("message", "Ready"),
        "log_path": meta.get("log_path", ""),
        "summary": {
            "total_chunks": len(chunks),
            "total_files": total_files,
            "total_size_mb": total_size,
            "counts": counts,
            "overall_pct": overall_pct,
        },
        "chunks": [asdict(c) for c in chunks],
    }


def save_state(chunks: List[Chunk], meta: Dict) -> None:
    global APP_STATE
    state = build_state(chunks, meta)
    with APP_LOCK:
        APP_STATE = state
        STATE_JSON.write_text(json.dumps(state, indent=2), encoding="utf-8")


def set_progress(chunk: Chunk, stage: str, pct: int, message: str, chunks: List[Chunk], meta: Dict) -> None:
    chunk.stage = stage
    chunk.progress_pct = max(0, min(100, int(pct)))
    chunk.message = message
    save_state(chunks, meta)


def process_single_chunk(chunk: Chunk, repo_path: Path, chunks: List[Chunk], meta: Dict, processed: set) -> None:
    global STOP_REQUESTED
    dry_run = bool(meta.get("dry_run"))
    push = bool(meta.get("push"))

    chunk.status = STATUS_PROCESSING
    chunk.started_at = chunk.started_at or now_text()
    set_progress(chunk, "Checking", 5, "Checking files", chunks, meta)

    existing_files = [f.path for f in chunk.files if f.exists]
    if not existing_files:
        chunk.status = STATUS_FAILED
        chunk.finished_at = now_text()
        set_progress(chunk, "Failed", 100, "No existing files found in this chunk", chunks, meta)
        return

    set_progress(chunk, "Adding", 20, f"Adding {len(existing_files)} files to git staging", chunks, meta)
    ok, out = run_git(repo_path, ["add", "--", *existing_files], dry_run=dry_run)
    if not ok:
        chunk.status = STATUS_FAILED
        chunk.finished_at = now_text()
        set_progress(chunk, "Failed", 100, f"git add failed: {out}", chunks, meta)
        return

    if STOP_REQUESTED:
        chunk.status = STATUS_SKIPPED
        chunk.finished_at = now_text()
        set_progress(chunk, "Stopped", 100, "Stopped before commit", chunks, meta)
        return

    if dry_run:
        chunk.status = STATUS_DRY_RUN
        chunk.commit_hash = "dry-run"
        chunk.finished_at = now_text()
        set_progress(chunk, "Dry run complete", 100, f"Would add, commit and {'push' if push else 'not push'} this chunk", chunks, meta)
        return

    set_progress(chunk, "Committing", 50, "Creating git commit", chunks, meta)
    commit_message = f"Chunk #{chunk.number} - {chunk.declared_file_count} files pushed successfully"
    ok, out = run_git(repo_path, ["commit", "-m", commit_message], False)
    if not ok:
        lowered = out.lower()
        if "nothing to commit" in lowered or "no changes added" in lowered:
            chunk.status = STATUS_SKIPPED
            chunk.finished_at = now_text()
            set_progress(chunk, "Skipped", 100, "No changes to commit", chunks, meta)
            return
        chunk.status = STATUS_FAILED
        chunk.finished_at = now_text()
        set_progress(chunk, "Failed", 100, f"git commit failed: {out}", chunks, meta)
        return

    chunk.commit_hash = last_commit_hash(repo_path)

    if push:
        set_progress(chunk, "Pushing", 75, f"Pushing commit {chunk.commit_hash or ''}", chunks, meta)
        ok, out = run_git_push_stream(repo_path, False, chunk, chunks, meta)
        chunk.push_output = out[-6000:] if out else ""
        if not ok:
            chunk.status = STATUS_FAILED
            chunk.finished_at = now_text()
            set_progress(chunk, "Push failed", 100, f"git push failed: {out}", chunks, meta)
            return
    else:
        set_progress(chunk, "Committed", 85, "Committed. Push disabled.", chunks, meta)

    chunk.status = STATUS_SUCCESS
    chunk.finished_at = now_text()
    processed.add(chunk.number)
    save_processed_chunks(processed)
    set_progress(chunk, "Done", 100, f"Chunk processed successfully. Added {len(existing_files)} files. Missing {chunk.missing_files}.", chunks, meta)


def worker_process(config: Dict) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = False
    log_path = setup_logging()
    chunks: List[Chunk] = []
    meta = {
        "app_status": "running",
        "repo_path": config.get("repo_path", ""),
        "log_file": config.get("log_file", ""),
        "dry_run": bool(config.get("dry_run", False)),
        "push": bool(config.get("push", True)),
        "start_chunk": config.get("start_chunk"),
        "end_chunk": config.get("end_chunk"),
        "message": "Starting...",
        "log_path": str(log_path),
    }

    try:
        log_file = clean_path(str(config.get("log_file", "")))
        repo_path = clean_path(str(config.get("repo_path", "")))
        start_chunk = config.get("start_chunk")
        end_chunk = config.get("end_chunk")
        pause = float(config.get("pause", 0) or 0)

        if not log_file.exists() or not log_file.is_file():
            raise FileNotFoundError(f"git_chunks.log file not found: {log_file}")
        check_repo(repo_path)

        meta["message"] = "Parsing chunks"
        chunks = parse_chunks(log_file)
        if not chunks:
            raise RuntimeError("No chunks found in the log file.")
        mark_file_existence(chunks, repo_path)

        processed = load_processed_chunks()
        for chunk in chunks:
            if start_chunk and chunk.number < int(start_chunk):
                chunk.status = STATUS_SKIPPED
                chunk.message = f"Skipped before start chunk {start_chunk}"
                chunk.progress_pct = 100
            elif end_chunk and chunk.number > int(end_chunk):
                chunk.status = STATUS_SKIPPED
                chunk.message = f"Skipped after end chunk {end_chunk}"
                chunk.progress_pct = 100
            elif chunk.number in processed:
                chunk.status = STATUS_SKIPPED
                chunk.message = "Already processed earlier"
                chunk.progress_pct = 100

        meta["message"] = f"Parsed {len(chunks)} chunks"
        save_state(chunks, meta)

        for chunk in chunks:
            if STOP_REQUESTED:
                meta["message"] = "Stop requested. Remaining chunks skipped."
                break
            if chunk.status != STATUS_PENDING:
                save_state(chunks, meta)
                continue
            meta["message"] = f"Processing Chunk #{chunk.number}"
            save_state(chunks, meta)
            process_single_chunk(chunk, repo_path, chunks, meta, processed)
            if pause > 0:
                time.sleep(pause)

        meta["app_status"] = "finished" if not STOP_REQUESTED else "stopped"
        meta["message"] = "Finished processing" if not STOP_REQUESTED else "Stopped by user"
        save_state(chunks, meta)
        logging.info(meta["message"])
    except Exception as exc:
        logging.exception("Fatal error")
        meta["app_status"] = "error"
        meta["message"] = str(exc)
        save_state(chunks, meta)


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Git Chunk Processor Web UI</title>
<style>
:root{
  --bg:#020617;--panel:#0f172a;--panel2:#111827;--soft:#1e293b;--border:#334155;
  --text:#e5e7eb;--muted:#94a3b8;--blue:#60a5fa;--blue2:#2563eb;--green:#34d399;
  --red:#fb7185;--yellow:#fbbf24;--purple:#c084fc;--cyan:#22d3ee;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at top,#1e293b 0,#0f172a 42%,#020617 100%);color:var(--text);font-family:Arial,Helvetica,sans-serif}
.wrap{max-width:1600px;margin:0 auto;padding:22px}
header{position:sticky;top:0;z-index:20;background:rgba(2,6,23,.94);backdrop-filter:blur(14px);border-bottom:1px solid var(--border)}
h1{margin:0;font-size:25px}.sub{color:var(--muted);font-size:13px;margin-top:6px;line-height:1.45}
.panel{background:rgba(15,23,42,.9);border:1px solid var(--border);border-radius:18px;padding:16px;margin-top:16px;box-shadow:0 16px 45px rgba(0,0,0,.22)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.field label{display:block;font-size:12px;color:#bfdbfe;margin-bottom:6px;font-weight:800}.pathRow{display:flex;gap:8px}
.field input,.field select{width:100%;padding:11px 12px;border-radius:11px;border:1px solid var(--border);background:#07111f;color:var(--text);font-size:14px}.field input:focus{outline:1px solid #2563eb}
.hint{font-size:12px;color:var(--muted);margin-top:5px;line-height:1.4}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
button{border:1px solid #2563eb;background:#1d4ed8;color:white;border-radius:11px;padding:11px 14px;font-weight:800;cursor:pointer}button:hover{background:#2563eb}button.secondary{background:#0b1220;border-color:var(--border)}button.danger{background:#9f1239;border-color:#be123c}.mini{white-space:nowrap;padding:11px 12px}
.drop{border:1px dashed #475569;border-radius:12px;padding:10px;color:var(--muted);font-size:12px;background:rgba(30,41,59,.35)}.drop.drag{border-color:#60a5fa;color:#bfdbfe;background:rgba(96,165,250,.1)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-top:16px}.stat{background:#0b1220;border:1px solid var(--border);border-radius:14px;padding:13px}.stat .v{font-size:22px;font-weight:900}.stat .l{font-size:12px;color:var(--muted);margin-top:3px}.bar{height:11px;background:#1e293b;border-radius:999px;overflow:hidden;margin-top:14px}.fill{height:100%;background:linear-gradient(90deg,#2563eb,#22c55e);width:0%}
.filters{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}.filters input,.filters select{padding:10px 12px;border-radius:11px;border:1px solid var(--border);background:#07111f;color:var(--text)}.filters input{min-width:320px;flex:1}
.dashboard{display:grid;grid-template-columns:minmax(470px, 0.95fr) minmax(520px, 1.05fr);gap:16px;align-items:start}.tablePanel,.detailPanel{min-height:420px}
.tableHeader{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:12px}.tableHeader h2,.detailTitle h2{margin:0;font-size:19px}.countText{font-size:12px;color:var(--muted)}
.tableWrap{overflow:auto;border:1px solid var(--border);border-radius:14px;background:#07111f;max-height:72vh}.chunkTable{width:100%;border-collapse:collapse;font-size:13px}.chunkTable th{position:sticky;top:0;background:#0f172a;color:#bfdbfe;text-align:left;padding:11px;border-bottom:1px solid var(--border);z-index:2}.chunkTable td{padding:10px 11px;border-bottom:1px solid #1e293b;vertical-align:middle}.chunkTable tr{cursor:pointer}.chunkTable tr:hover{background:rgba(96,165,250,.08)}.chunkTable tr.selected{background:rgba(96,165,250,.18);outline:1px solid rgba(96,165,250,.45)}.chunkTable tr.active{background:linear-gradient(90deg,rgba(37,99,235,.35),rgba(14,165,233,.12));box-shadow:inset 4px 0 0 var(--cyan)}.chunkTable tr.active.selected{background:linear-gradient(90deg,rgba(37,99,235,.48),rgba(14,165,233,.18))}
.num{font-weight:900;color:#dbeafe}.tiny{font-size:12px;color:var(--muted)}.rowProgress{height:8px;background:#1e293b;border-radius:999px;overflow:hidden;min-width:90px}.rowFill{height:100%;background:linear-gradient(90deg,#60a5fa,#34d399)}
.badge{display:inline-flex;align-items:center;justify-content:center;min-width:82px;padding:5px 9px;border-radius:999px;border:1px solid var(--border);font-size:11px;text-transform:uppercase;font-weight:900;letter-spacing:.35px}.badge.pending{color:#cbd5e1;background:#1e293b}.badge.processing{color:#dbeafe;background:#1d4ed8;border-color:#60a5fa;box-shadow:0 0 18px rgba(96,165,250,.35)}.badge.success{color:#bbf7d0;background:#064e3b;border-color:#10b981}.badge.failed{color:#fecdd3;background:#881337;border-color:#fb7185}.badge.skipped{color:#fde68a;background:#713f12;border-color:#f59e0b}.badge.dry_run{color:#e9d5ff;background:#581c87;border-color:#c084fc}
.detailEmpty{display:grid;place-items:center;min-height:390px;color:var(--muted);border:1px dashed var(--border);border-radius:16px;background:rgba(7,17,31,.6);text-align:center;padding:22px}.chunkCard{border:1px solid var(--border);background:#07111f;border-radius:18px;overflow:hidden}.cardTop{padding:17px;background:linear-gradient(90deg,rgba(37,99,235,.16),rgba(15,23,42,.95));border-bottom:1px solid var(--border)}.cardTop.active{background:linear-gradient(90deg,rgba(37,99,235,.42),rgba(8,47,73,.55));border-color:#38bdf8}.detailTitle{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.detailMeta{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-top:13px}.pill{background:rgba(15,23,42,.9);border:1px solid var(--border);border-radius:12px;padding:10px}.pill .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.35px}.pill .val{font-size:14px;font-weight:800;margin-top:4px}.chunkProg{height:13px;background:#172033;border-radius:999px;overflow:hidden;margin-top:13px}.chunkFill{height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e)}.stages{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px}.stage{text-align:center;border:1px solid var(--border);border-radius:10px;padding:7px 4px;color:var(--muted);background:#0b1220;font-size:12px;font-weight:800}.stage.done{background:#064e3b;color:#bbf7d0;border-color:#10b981}.stage.active{background:#1d4ed8;color:#dbeafe;border-color:#60a5fa;box-shadow:0 0 18px rgba(96,165,250,.25)}.msg{margin-top:12px;color:#dbeafe;font-size:13px;line-height:1.5}.cardBody{padding:16px}.tools{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px;color:#bfdbfe;font-size:13px}.fileSearch{padding:9px 11px;border-radius:10px;border:1px solid var(--border);background:#0b1220;color:var(--text);min-width:240px}.fileTableWrap{max-height:48vh;overflow:auto;border:1px solid var(--border);border-radius:13px}.fileTable{width:100%;border-collapse:collapse;font-size:13px}.fileTable th{position:sticky;top:0;background:#0f172a;color:#bfdbfe;text-align:left;padding:10px;border-bottom:1px solid var(--border)}.fileTable td{padding:9px 10px;border-bottom:1px solid #1e293b;vertical-align:top}.fileName{font-weight:800;color:#f8fafc}.fileDir{font-size:12px;color:var(--muted);word-break:break-all;margin-top:2px}.ok{color:#86efac;font-weight:800}.miss{color:#fb7185;font-weight:800}.size{text-align:right;color:#a7f3d0;white-space:nowrap}.out{margin-top:12px;background:#020617;border:1px solid var(--border);border-radius:12px;padding:12px;color:#cbd5e1;white-space:pre-wrap;font-family:Consolas,monospace;font-size:12px;max-height:240px;overflow:auto}.empty{color:var(--muted);text-align:center;padding:26px}.activeDot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#22d3ee;margin-right:7px;box-shadow:0 0 14px #22d3ee;vertical-align:middle}
@media(max-width:1100px){.dashboard{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.tableWrap{max-height:55vh}.filters input{min-width:100%}}
</style>
</head>
<body>
<header><div class="wrap"><h1>Git Chunk Processor Dashboard</h1><div class="sub">Enter paths in the UI, start processing, then use the chunks table to inspect one chunk card at a time. The active chunk is highlighted automatically.</div></div></header>
<div class="wrap">
<section class="panel">
  <div class="grid">
    <div class="field"><label>git_chunks.log path</label><div class="pathRow"><input id="logFile" placeholder='D:\UE World\Project\git_chunks.log'><button class="secondary mini" onclick="clearField('logFile')">Clear</button></div><div class="hint">Paste path here. Dragging a file from Explorer may paste its path depending on browser/Windows behavior.</div></div>
    <div class="field"><label>Git repository folder path</label><div class="pathRow"><input id="repoPath" placeholder='D:\UE World\Project'><button class="secondary mini" onclick="clearField('repoPath')">Clear</button></div><div class="hint">Use the folder that contains <b>.git</b>. Example: the Unreal project repo root.</div></div>
    <div class="field"><label>Start chunk</label><input id="startChunk" type="number" placeholder="optional"></div>
    <div class="field"><label>End chunk</label><input id="endChunk" type="number" placeholder="optional"></div>
    <div class="field"><label>Pause between chunks / seconds</label><input id="pause" type="number" value="0" step="0.1"></div>
    <div class="field"><label>Mode</label><select id="mode"><option value="push">Commit and push each chunk</option><option value="noPush">Commit only, no push</option><option value="dryRun">Dry run only</option></select></div>
  </div>
  <div id="dropZone" class="drop" style="margin-top:12px">Tip: You can drag/drop copied path text here or directly into the fields. For safety, browsers do not always expose full local folder paths.</div>
  <div class="actions"><button onclick="startProcessing()">Start Processing</button><button class="secondary" onclick="refreshState()">Refresh</button><button class="secondary" onclick="openStateFile()">Open State JSON</button><button class="danger" onclick="stopProcessing()">Stop After Current Chunk</button></div>
  <div class="sub" id="topMessage" style="margin-top:12px">Ready.</div>
</section>
<section class="panel">
  <div class="stats"><div class="stat"><div class="v" id="totalChunks">0</div><div class="l">Total chunks</div></div><div class="stat"><div class="v" id="totalFiles">0</div><div class="l">Total files</div></div><div class="stat"><div class="v" id="doneChunks">0</div><div class="l">Done / skipped</div></div><div class="stat"><div class="v" id="failedChunks">0</div><div class="l">Failed</div></div><div class="stat"><div class="v" id="activeChunk">-</div><div class="l">Active chunk</div></div></div>
  <div class="bar"><div class="fill" id="overallFill"></div></div>
  <div class="filters"><input id="search" placeholder="Search chunk number, status, file name, folder path..."><select id="statusFilter"><option value="all">All statuses</option><option value="pending">Pending</option><option value="processing">Processing</option><option value="success">Success</option><option value="skipped">Skipped</option><option value="failed">Failed</option><option value="dry_run">Dry run</option></select><button class="secondary" onclick="selectActiveChunk()">Show Active</button><button class="secondary" onclick="selectFirstVisible()">Show First Visible</button></div>
</section>
<section class="dashboard">
  <div class="panel tablePanel">
    <div class="tableHeader"><div><h2>Chunks Table</h2><div class="countText" id="shownCount">0 chunks visible</div></div><div class="countText">Click a row to open its card →</div></div>
    <div id="chunkTableRoot" class="tableWrap"></div>
  </div>
  <div class="panel detailPanel">
    <div id="detailRoot" class="detailEmpty">No chunk selected yet.<br>Start processing or click a chunk from the table.</div>
  </div>
</section>
</div>
<script>
let STATE = null;
let SELECTED_CHUNK = null;
let USER_SELECTED = false;
const $ = id => document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function clearField(id){$(id).value='';}
function fileName(p){const parts=String(p||'').split(/[\\/]/);return parts[parts.length-1]||p;}
function fileDir(p){const s=String(p||'');const i=Math.max(s.lastIndexOf('\\'),s.lastIndexOf('/'));return i>=0?s.slice(0,i):'';}
function badge(s){return `<span class="badge ${esc(s)}">${esc(String(s||'').replace('_',' '))}</span>`}
function stageClass(c,p){const pct=Number(c.progress_pct||0); if(pct>=p)return 'done'; if(c.status==='processing'&&pct>p-25)return 'active'; return '';}
function findChunk(num){return (STATE?.chunks||[]).find(c=>Number(c.number)===Number(num));}
function visibleChunks(){if(!STATE)return[]; const q=$('search').value.trim().toLowerCase(); const f=$('statusFilter').value; return (STATE.chunks||[]).filter(c=>{const allText=[`chunk #${c.number}`,c.status,c.message,c.stage,...(c.files||[]).map(x=>x.path)].join(' ').toLowerCase(); if(f!=='all'&&c.status!==f)return false; if(q&&!allText.includes(q))return false; return true;});}
async function api(path, data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});return await r.json();}
async function startProcessing(){const mode=$('mode').value; USER_SELECTED=false; SELECTED_CHUNK=null; const payload={log_file:$('logFile').value,repo_path:$('repoPath').value,start_chunk:$('startChunk').value?Number($('startChunk').value):null,end_chunk:$('endChunk').value?Number($('endChunk').value):null,pause:Number($('pause').value||0),dry_run:mode==='dryRun',push:mode==='push'}; $('topMessage').textContent='Starting...'; const res=await api('/api/start',payload); $('topMessage').textContent=res.message||JSON.stringify(res); refreshState();}
async function stopProcessing(){const res=await api('/api/stop',{}); $('topMessage').textContent=res.message||'Stop requested';}
async function openStateFile(){await api('/api/open_state',{});}
async function refreshState(){try{const r=await fetch('/api/state?ts='+Date.now()); STATE=await r.json(); autoSelect(); render();}catch(e){$('topMessage').textContent='Could not load state: '+e;}}
function autoSelect(){if(!STATE)return; const active=STATE.active_chunk; const chunks=STATE.chunks||[]; if(active && !USER_SELECTED){SELECTED_CHUNK=active; return;} if(SELECTED_CHUNK && findChunk(SELECTED_CHUNK))return; SELECTED_CHUNK=active || (chunks[0]?.number ?? null);}
function selectChunk(num, manual=true){SELECTED_CHUNK=Number(num); USER_SELECTED=manual; render();}
function selectActiveChunk(){if(STATE?.active_chunk){selectChunk(STATE.active_chunk,false);} }
function selectFirstVisible(){const first=visibleChunks()[0]; if(first)selectChunk(first.number,true);}
function render(){if(!STATE)return; const s=STATE.summary||{}; const counts=s.counts||{}; const complete=(counts.success||0)+(counts.skipped||0)+(counts.dry_run||0); $('totalChunks').textContent=s.total_chunks||0; $('totalFiles').textContent=s.total_files||0; $('doneChunks').textContent=complete; $('failedChunks').textContent=counts.failed||0; $('activeChunk').textContent=STATE.active_chunk?('#'+STATE.active_chunk):'-'; $('overallFill').style.width=(s.overall_pct||0)+'%'; $('topMessage').textContent=(STATE.app_status||'idle')+' — '+(STATE.message||'Ready')+' | '+(STATE.repo_path||''); renderTable(); renderDetail();}
function renderTable(){const root=$('chunkTableRoot'); const rows=visibleChunks(); $('shownCount').textContent=`${rows.length} chunks visible`; if(!rows.length){root.innerHTML='<div class="empty">No chunks match the current search/filter.</div>'; return;} root.innerHTML=`<div class="chunkGrid">${rows.map(c=>{const active=Number(STATE.active_chunk)===Number(c.number); const selected=Number(SELECTED_CHUNK)===Number(c.number); const status=String(c.status||'pending'); return `<button type="button" title="Chunk #${esc(c.number)} • ${esc(status)} • ${Number(c.progress_pct||0)}%" class="chunkCell ${active?'active ':''}${selected?'selected':''}" onclick="selectChunk(${Number(c.number)},true)"><div class="cellTop"><span class="cellNum">#${esc(c.number)}</span><span class="miniBadge ${esc(status)}"></span></div><div class="cellFiles">${esc((c.files||[]).length)} files • ${Number(c.size_mb||0).toFixed(2)} MB</div><div class="cellStage">${esc(c.stage||status)}${(c.missing_files||0)?' • missing '+esc(c.missing_files):''}</div><div class="cellProg"><div class="cellFill" style="width:${Number(c.progress_pct||0)}%"></div></div></button>`}).join('')}</div>`;}
function renderDetail(){const root=$('detailRoot'); const c=findChunk(SELECTED_CHUNK); if(!c){root.className='detailEmpty'; root.innerHTML='No chunk selected yet.<br>Click a chunk cell.'; return;} root.className=''; const isActive=Number(STATE.active_chunk)===Number(c.number); const q=($('fileSearch')?.value||'').trim().toLowerCase(); const files=(c.files||[]).filter(file=>!q || String(file.path).toLowerCase().includes(q)); root.innerHTML=`<article class="chunkCard"><div class="cardTop ${isActive?'active':''}"><div class="detailTitle"><div><h2>${isActive?'<span class="activeDot"></span>':''}Chunk #${esc(c.number)}</h2><div class="sub">${esc(c.message||'')}</div></div>${badge(c.status)}</div><div class="detailMeta"><div class="pill"><div class="k">Stage</div><div class="val">${esc(c.stage||'-')}</div></div><div class="pill"><div class="k">Files</div><div class="val">${esc((c.files||[]).length)} parsed / ${esc(c.declared_file_count)} declared</div></div><div class="pill"><div class="k">Ready</div><div class="val">${esc(c.added_files||0)} files</div></div><div class="pill"><div class="k">Missing</div><div class="val">${esc(c.missing_files||0)} files</div></div><div class="pill"><div class="k">Size</div><div class="val">${Number(c.size_mb||0).toFixed(2)} MB</div></div><div class="pill"><div class="k">Commit</div><div class="val">${esc(c.commit_hash||'-')}</div></div></div><div class="chunkProg"><div class="chunkFill" style="width:${Number(c.progress_pct||0)}%"></div></div><div class="stages"><span class="stage ${stageClass(c,5)}">Check</span><span class="stage ${stageClass(c,20)}">Add</span><span class="stage ${stageClass(c,50)}">Commit</span><span class="stage ${stageClass(c,75)}">Push</span><span class="stage ${stageClass(c,100)}">Done</span></div><div class="msg">Started: ${esc(c.started_at||'-')} &nbsp; | &nbsp; Finished: ${esc(c.finished_at||'-')}</div></div><div class="cardBody"><div class="tools"><span>Files in selected chunk</span><input id="fileSearch" class="fileSearch" placeholder="Search files inside this chunk..." value="${esc(q)}"></div><div class="fileTableWrap"><table class="fileTable"><thead><tr><th style="width:70px">#</th><th>File</th><th style="width:120px">Status</th><th style="width:110px;text-align:right">Size</th></tr></thead><tbody>${files.map((file,i)=>`<tr><td>#${i+1}</td><td><div class="fileName">${esc(fileName(file.path))}</div><div class="fileDir">${esc(fileDir(file.path))}</div></td><td class="${file.exists===false?'miss':'ok'}">${file.exists===false?'Missing':'Ready'}</td><td class="size">${file.size_mb==null?'-':Number(file.size_mb).toFixed(2)+' MB'}</td></tr>`).join('')}</tbody></table></div>${c.push_output?`<div class="out">${esc(c.push_output)}</div>`:''}</div></article>`; const fs=$('fileSearch'); if(fs){fs.addEventListener('input',renderDetail); fs.focus({preventScroll:true});}}
$('search').addEventListener('input',()=>{render();}); $('statusFilter').addEventListener('change',()=>{render();});
function setupDrop(el){['dragenter','dragover'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();el.classList.add('drag')}));['dragleave','drop'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();el.classList.remove('drag')}));el.addEventListener('drop',e=>{const text=e.dataTransfer.getData('text/plain')||''; if(text){ if(!$('logFile').value && text.toLowerCase().includes('.log')) $('logFile').value=text.trim(); else if(!$('repoPath').value) $('repoPath').value=text.trim(); } });}
setupDrop($('dropZone')); setupDrop($('logFile')); setupDrop($('repoPath'));
refreshState(); setInterval(refreshState,1500);
</script>
</body>
</html>'''


def empty_state() -> Dict:
    return {
        "app_status": STATUS_IDLE,
        "generated_at": now_text(),
        "repo_path": "",
        "log_file": "",
        "dry_run": False,
        "push": True,
        "active_chunk": None,
        "message": "Ready. Enter paths and press Start Processing.",
        "summary": {"total_chunks": 0, "total_files": 0, "total_size_mb": 0, "counts": {}, "overall_pct": 0},
        "chunks": [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, data: Dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/state":
            with APP_LOCK:
                state = APP_STATE or empty_state()
            self.send_json(state)
        else:
            self.send_json({"ok": False, "message": "Not found"}, 404)

    def do_POST(self):
        global PROCESS_THREAD, STOP_REQUESTED
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                payload = {}
        parsed = urlparse(self.path)
        if parsed.path == "/api/start":
            if PROCESS_THREAD and PROCESS_THREAD.is_alive():
                self.send_json({"ok": False, "message": "Processor is already running."}, 409)
                return
            STOP_REQUESTED = False
            PROCESS_THREAD = threading.Thread(target=worker_process, args=(payload,), daemon=True)
            PROCESS_THREAD.start()
            self.send_json({"ok": True, "message": "Processing started. Dashboard will update automatically."})
        elif parsed.path == "/api/stop":
            STOP_REQUESTED = True
            self.send_json({"ok": True, "message": "Stop requested. It will stop after the current safe point."})
        elif parsed.path == "/api/open_state":
            if STATE_JSON.exists():
                try:
                    os.startfile(str(STATE_JSON))  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.send_json({"ok": True, "message": str(STATE_JSON)})
        else:
            self.send_json({"ok": False, "message": "Not found"}, 404)


def main():
    global APP_STATE
    with APP_LOCK:
        APP_STATE = empty_state()
    STATE_JSON.write_text(json.dumps(APP_STATE, indent=2), encoding="utf-8")
    port = 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("Git Chunk Processor Web UI")
    print(f"Opening: {url}")
    print("Keep this window open while processing.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
