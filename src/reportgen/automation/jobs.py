"""Repeatable report jobs.

Jobs are plain JSON records under ``~/.reportgen`` (override with the
``REPORTGEN_HOME`` environment variable). A job run executes the CLI in
a subprocess, which gives real isolation, a hard timeout, and a
per-run log file — no always-running daemon is required. Operating-
system schedulers (cron, Task Scheduler, launchd) invoke
``reportgen jobs run <name>``; see docs/automation.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reportgen.errors import JobError
from reportgen.logging_config import get_logger

logger = get_logger("reportgen.automation")
_LOCK_STALE_SECONDS = 3600


def default_home() -> Path:
    override = os.environ.get("REPORTGEN_HOME")
    return Path(override).expanduser() if override else Path.home() / ".reportgen"


@dataclass
class Job:
    name: str
    config: str  # absolute path to the report definition
    formats: list[str] = field(default_factory=list)
    output_dir: str | None = None
    overwrite: bool = False
    timeout_seconds: int = 600
    # Keep only the most recent N successful runs' files. None = keep all.
    # Nothing is ever deleted unless this is explicitly set.
    retention_keep: int | None = None
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class JobStore:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or default_home()
        self.jobs_file = self.home / "jobs.json"
        self.history_file = self.home / "history.jsonl"
        self.locks_dir = self.home / "locks"
        self.logs_dir = self.home / "logs"

    def _ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # -- job CRUD ---------------------------------------------------------
    def list_jobs(self) -> list[Job]:
        if not self.jobs_file.exists():
            return []
        try:
            data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"Cannot read jobs file {self.jobs_file}: {exc}") from exc
        return [Job.from_dict(item) for item in data.get("jobs", [])]

    def get_job(self, name: str) -> Job:
        for job in self.list_jobs():
            if job.name == name:
                return job
        raise JobError(f"No job named '{name}'. Run 'reportgen jobs list' to see saved jobs.")

    def add_job(self, job: Job, replace: bool = False) -> Job:
        self._ensure()
        jobs = self.list_jobs()
        if any(j.name == job.name for j in jobs) and not replace:
            raise JobError(
                f"A job named '{job.name}' already exists. Remove it first or use a different name."
            )
        jobs = [j for j in jobs if j.name != job.name]
        if not job.created_at:
            job.created_at = datetime.now(UTC).isoformat()
        jobs.append(job)
        self._save(jobs)
        return job

    def remove_job(self, name: str) -> None:
        self._ensure()
        jobs = self.list_jobs()
        remaining = [j for j in jobs if j.name != name]
        if len(remaining) == len(jobs):
            raise JobError(f"No job named '{name}'.")
        self._save(remaining)

    def _save(self, jobs: list[Job]) -> None:
        payload = {"version": 1, "jobs": [asdict(j) for j in jobs]}
        tmp = self.jobs_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.jobs_file)

    # -- history ----------------------------------------------------------
    def record_history(self, entry: dict) -> None:
        self._ensure()
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def history(self, job: str | None = None, limit: int = 20) -> list[dict]:
        if not self.history_file.exists():
            return []
        entries: list[dict] = []
        for line in self.history_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a torn final line after a crash
        if job:
            entries = [e for e in entries if e.get("job") == job]
        return entries[-limit:][::-1]

    # -- locking ------------------------------------------------------------
    def acquire_lock(self, name: str) -> Path:
        self._ensure()
        lock = self.locks_dir / f"{name}.lock"
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < _LOCK_STALE_SECONDS:
                raise JobError(
                    f"Job '{name}' appears to be running already "
                    f"(lock: {lock}). If no run is active, delete the lock file."
                )
            logger.warning("Removing stale lock for job '%s' (age %.0fs)", name, age)
            lock.unlink(missing_ok=True)
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise JobError(f"Job '{name}' is already running.") from exc
        with os.fdopen(fd, "w") as handle:
            handle.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
        return lock

    @staticmethod
    def release_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)


def _apply_retention(store: JobStore, job: Job) -> list[str]:
    """Delete outputs of older successful runs beyond retention_keep.

    Only files recorded in this job's own history are ever removed.
    """
    if job.retention_keep is None:
        return []
    successful = [
        e for e in store.history(job=job.name, limit=10_000) if e.get("status") == "success"
    ]
    removed: list[str] = []
    for entry in successful[job.retention_keep :]:
        for output in entry.get("outputs", []):
            path = Path(output)
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:
                logger.warning("Retention could not remove %s: %s", path, exc)
    return removed


def run_job(store: JobStore, name: str) -> dict:
    """Run one job in a subprocess; record history; return the entry."""
    job = store.get_job(name)
    config_path = Path(job.config)
    if not config_path.exists():
        raise JobError(f"Job '{name}' points at a missing config: {config_path}")
    lock = store.acquire_lock(name)
    started = datetime.now(UTC)
    log_path = store.logs_dir / f"{name}-{started.strftime('%Y%m%d-%H%M%S')}.log"
    entry: dict = {
        "job": name,
        "started_at": started.isoformat(),
        "status": "failed",
        "outputs": [],
        "error": None,
        "log": str(log_path),
        "duration_seconds": None,
    }
    command = [sys.executable, "-m", "reportgen", "--quiet", "generate", str(config_path), "--json"]
    for fmt in job.formats:
        command += ["--format", fmt]
    if job.output_dir:
        command += ["--output-dir", job.output_dir]
    if job.overwrite:
        command += ["--overwrite"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=job.timeout_seconds,
            check=False,
        )
        log_path.write_text(
            f"$ {' '.join(command)}\n\nSTDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}",
            encoding="utf-8",
        )
        if completed.returncode == 0:
            outputs: list[str] = []
            for line in completed.stdout.splitlines()[::-1]:
                try:
                    payload = json.loads(line)
                    outputs = [str(p) for p in payload.get("outputs", [])]
                    break
                except (json.JSONDecodeError, AttributeError):
                    continue
            entry.update(status="success", outputs=outputs)
        else:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            entry["error"] = detail[-1] if detail else f"exit code {completed.returncode}"
    except subprocess.TimeoutExpired:
        entry.update(status="timeout", error=f"Exceeded timeout of {job.timeout_seconds} seconds.")
        log_path.write_text(
            f"Timed out after {job.timeout_seconds}s: {' '.join(command)}\n", encoding="utf-8"
        )
    finally:
        finished = datetime.now(UTC)
        entry["finished_at"] = finished.isoformat()
        entry["duration_seconds"] = round((finished - started).total_seconds(), 2)
        store.record_history(entry)
        store.release_lock(lock)
    if entry["status"] == "success":
        removed = _apply_retention(store, job)
        if removed:
            entry["retention_removed"] = removed
    if entry["status"] != "success":
        raise JobError(
            f"Job '{name}' failed ({entry['status']}): {entry['error']} See log: {log_path}"
        )
    return entry


def schedule_examples(job: Job, cron: str = "0 6 1 * *") -> dict[str, str]:
    """Return ready-to-paste scheduler snippets for every platform."""
    command = f"reportgen jobs run {job.name}"
    return {
        "cron (Linux/macOS)": f"{cron} {command} >> ~/.reportgen/logs/{job.name}-cron.log 2>&1",
        "Windows Task Scheduler": (
            f'schtasks /Create /TN "reportgen-{job.name}" /SC MONTHLY /D 1 '
            f'/ST 06:00 /TR "{command}"'
        ),
        "launchd (macOS)": (
            "Create ~/Library/LaunchAgents/com.reportgen." + job.name + ".plist with "
            f'ProgramArguments = ["reportgen", "jobs", "run", "{job.name}"] and a '
            "StartCalendarInterval; see docs/automation.md for a full example."
        ),
    }
