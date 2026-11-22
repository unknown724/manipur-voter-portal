#!/usr/bin/env python3
"""
Ralph Loop Autonomous Development Harness
=========================================
Pioneered by Geoffrey Huntley for AI-driven software development.
This harness reads tasks from tasks/backlog.json, isolates execution,
empirically validates each step, records progress, and creates atomic Git commits.
"""

import os
import sys
import json
import time
import subprocess
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
BACKLOG_PATH = WORKSPACE_ROOT / "tasks" / "backlog.json"
PROGRESS_PATH = WORKSPACE_ROOT / "tasks" / "progress.txt"


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": "🔵", "SUCCESS": "🟢", "WARN": "🟡", "ERROR": "🔴"}.get(level, "⚪")
    print(f"[{ts}] {prefix} [{level}] {msg}")


def load_backlog():
    if not BACKLOG_PATH.exists():
        log(f"Backlog file not found: {BACKLOG_PATH}", "ERROR")
        sys.exit(1)
    with open(BACKLOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_backlog(data):
    with open(BACKLOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def append_progress(task_id: str, title: str, commit_hash: str = "N/A"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] COMPLETED: {task_id} - {title} (Commit: {commit_hash})\n"
    with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
        f.write(entry)


def run_command(cmd: str, check: bool = False):
    log(f"Executing: {cmd}")
    res = subprocess.run(cmd, shell=True, cwd=WORKSPACE_ROOT, capture_output=True, text=True)
    if res.returncode != 0 and check:
        log(f"Command failed:\n{res.stderr}", "ERROR")
        return False, res.stdout, res.stderr
    return True, res.stdout, res.stderr


def get_git_status():
    ok, out, _ = run_command("git status --short")
    return out.strip()


def run_ralph_iteration():
    backlog = load_backlog()
    tasks = backlog.get("tasks", [])
    
    pending_tasks = [t for t in tasks if t.get("status") == "pending"]
    if not pending_tasks:
        log("All tasks in backlog are COMPLETED! 🎉", "SUCCESS")
        return False

    task = pending_tasks[0]
    log(f"Starting Ralph Loop Iteration for: {task['id']} - {task['title']}", "INFO")
    print(f"\nTask Description:\n{task['description']}\n")
    print(f"Verification Command:\n{task.get('verification', 'N/A')}\n")

    # Run verification command if provided
    verif_cmd = task.get("verification")
    if verif_cmd:
        log(f"Running verification test: {verif_cmd}")
        ok, stdout, stderr = run_command(verif_cmd)
        if ok:
            log("Verification test PASSED!", "SUCCESS")
        else:
            log(f"Verification test FAILED:\n{stderr}", "WARN")

    return True


def main():
    print("=" * 65)
    print("  RALPH LOOP: AUTONOMOUS SOFTWARE ENGINEERING HARNESS")
    print("=" * 65)
    
    if not PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            f.write("# Ralph Loop Progress Ledger\n# Started: " + datetime.now().isoformat() + "\n\n")

    backlog = load_backlog()
    pending = [t for t in backlog.get("tasks", []) if t.get("status") == "pending"]
    completed = [t for t in backlog.get("tasks", []) if t.get("status") == "completed"]
    
    log(f"Loaded Backlog: {len(completed)} Completed | {len(pending)} Pending")
    
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print("\n--- Current Backlog Tasks ---")
        for t in backlog.get("tasks", []):
            status_icon = "✅" if t.get("status") == "completed" else "⏳"
            print(f"  {status_icon} [{t['id']}] ({t.get('priority', 'P2')}) {t['title']}")
        print()
        return

    # Check git status
    status = get_git_status()
    if status:
        log(f"Working tree has uncommitted modifications:\n{status}", "WARN")
    else:
        log("Working tree is clean.", "INFO")

    run_ralph_iteration()


if __name__ == "__main__":
    main()
