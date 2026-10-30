"""Repeatable report jobs.

Jobs are plain JSON records under ``~/.reportgen`` (override with the
``REPORTGEN_HOME`` environment variable). A job run executes the CLI in
a subprocess, which gives real isolation, a hard timeout, and a
per-run log file — no always-running daemon is required. Operating-
system schedulers (cron, Task Scheduler, launchd) invoke
``reportgen jobs run <name>``; see docs/automation.md.

Concurrency model:

- *Job names* are validated identifiers, so lock/log paths and the
  generated scheduler snippets can never be tricked into traversal or
  command injection.
- A short-lived *store lock* serialises every read-modify-write of
  ``jobs.json``/``history.jsonl`` and every lock-stealing decision.
- A per-job *run lock* records ``{pid, host, token, started}`` and is
  heartbeated while the job runs. A lock is only considered stale when
  its heartbeat has stopped **and** its owning process is gone — a
  long-running job's lock is never removed merely because it is old.
- Retention runs while the job lock is still held, and output paths
  belonging to retained runs are never deleted (so a repeated output
  filename cannot cause the newest report to be cleaned up).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reportgen.errors import JobError
from reportgen.logging_config import get_logger

logger = get_logger("reportgen.automation")

_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_HEARTBEAT_INTERVAL_SECONDS = 20.0
_HEARTBEAT_STALE_SECONDS = 120.0
_STORE_LOCK_TIMEOUT_SECONDS = 10.0
_STORE_LOCK_STALE_SECONDS = 30.0


def validate_job_name(name: str) -> str:
    """Job names become filenames and appear inside scheduler commands,
    so they must be plain identifiers: no separators, quotes, spaces,
    or shell metacharacters."""
    candidate = str(name or "")
    if (
        not _JOB_NAME_RE.match(candidate)
        or candidate in {".", ".."}
        or candidate.upper().split(".")[0] in _WINDOWS_RESERVED
    ):
        raise JobError(
            f"Invalid job name {name!r}. Use 1-63 characters: letters, digits, "
            "'.', '_' or '-', starting with a letter or digit (e.g. 'monthly-sales')."
        )
    return candidate


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return ctypes.GetLastError() == 5  # access denied: process exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _contained_file(base: Path, filename: str) -> Path:
    """Resolve ``base/filename`` and verify it stays directly inside base."""
    base_resolved = base.resolve()
    candidate = (base_resolved / filename).resolve()
    if candidate.parent != base_resolved:
        raise JobError(f"Refusing to use a path outside {base_resolved}: {candidate}")
    return candidate


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
    # --param overrides for the report's `parameters:` block.
    parameters: dict[str, str] = field(default_factory=dict)
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
        self._store_lock_file = self.home / "store.lock"
        self._lock_tokens: dict[Path, str] = {}

    def _ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # -- store lock (serialises read-modify-write cycles) -------------------
    @contextmanager
    def _store_lock(self):
        self._ensure()
        token = secrets.token_hex(8)
        deadline = time.monotonic() + _STORE_LOCK_TIMEOUT_SECONDS
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(self._store_lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._reclaim_stale_store_lock(token):
                    continue  # a stale lock was atomically claimed; retry
                if time.monotonic() > deadline:
                    raise JobError(
                        "The job store is busy (another reportgen process is "
                        "updating it). Try again in a moment."
                    ) from None
                time.sleep(0.02)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "token": token}))
            yield
        finally:
            try:
                info = json.loads(self._store_lock_file.read_text(encoding="utf-8"))
                if info.get("token") == token:
                    self._store_lock_file.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                pass  # someone else already recovered it; never delete blindly

    def _reclaim_stale_store_lock(self, token: str) -> bool:
        """Atomically claim the store lock iff it is stale; return True if
        the caller should retry creation.

        The staleness decision and the removal must not be separate
        steps (check-then-unlink can delete a *new* owner's lock), so:
        read payload -> decide -> rename the file to a unique claim name
        (exactly one contender wins the rename) -> verify the renamed
        payload is the one judged stale; if it changed underneath us,
        link it back untouched."""
        lock = self._store_lock_file
        try:
            age = time.time() - lock.stat().st_mtime
            payload_text = lock.read_text(encoding="utf-8")
        except OSError:
            return not lock.exists()  # vanished: retry creation
        try:
            info = json.loads(payload_text)
            if _pid_alive(int(info.get("pid") or 0)):
                stale = age > _STORE_LOCK_STALE_SECONDS * 10  # wedged owner
            else:
                stale = age > 0.5
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            # A *young* unreadable lock is a live owner between O_EXCL
            # creation and writing its payload — never reclaim it.
            stale = age > 1.0
        if not stale:
            return False
        claim = lock.with_name(f".store-lock-claim-{token}")
        try:
            os.rename(lock, claim)
        except OSError:
            return True  # someone else claimed/released it; retry
        try:
            if claim.read_text(encoding="utf-8") != payload_text:
                # We displaced a different (newer) lock: restore it.
                try:
                    os.link(claim, lock)
                except OSError:
                    pass  # an even newer lock exists; the owner re-checks
                return False
            return True
        finally:
            claim.unlink(missing_ok=True)

    # -- job CRUD ---------------------------------------------------------
    def _list_jobs_unlocked(self) -> list[Job]:
        if not self.jobs_file.exists():
            return []
        try:
            data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"Cannot read jobs file {self.jobs_file}: {exc}") from exc
        return [Job.from_dict(item) for item in data.get("jobs", [])]

    def list_jobs(self) -> list[Job]:
        return self._list_jobs_unlocked()

    def get_job(self, name: str) -> Job:
        validate_job_name(name)
        for job in self.list_jobs():
            if job.name == name:
                return job
        raise JobError(f"No job named '{name}'. Run 'reportgen jobs list' to see saved jobs.")

    def add_job(self, job: Job, replace: bool = False) -> Job:
        validate_job_name(job.name)
        with self._store_lock():
            jobs = self._list_jobs_unlocked()
            if any(j.name == job.name for j in jobs) and not replace:
                raise JobError(
                    f"A job named '{job.name}' already exists. "
                    "Remove it first or use a different name."
                )
            jobs = [j for j in jobs if j.name != job.name]
            if not job.created_at:
                job.created_at = datetime.now(UTC).isoformat()
            jobs.append(job)
            self._save_unlocked(jobs)
        return job

    def remove_job(self, name: str) -> None:
        validate_job_name(name)
        with self._store_lock():
            jobs = self._list_jobs_unlocked()
            remaining = [j for j in jobs if j.name != name]
            if len(remaining) == len(jobs):
                raise JobError(f"No job named '{name}'.")
            self._save_unlocked(remaining)

    def _save_unlocked(self, jobs: list[Job]) -> None:
        payload = {"version": 1, "jobs": [asdict(j) for j in jobs]}
        fd, tmp_name = tempfile.mkstemp(prefix=".jobs-", suffix=".json.tmp", dir=str(self.home))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2))
            os.replace(tmp_name, self.jobs_file)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    # -- history ----------------------------------------------------------
    def record_history(self, entry: dict) -> None:
        with self._store_lock():
            with self.history_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    def history(self, job: str | None = None, limit: int = 20) -> list[dict]:
        if job is not None:
            validate_job_name(job)
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

    # -- per-job run locking ------------------------------------------------
    def _lock_path(self, name: str) -> Path:
        return _contained_file(self.locks_dir, f"{validate_job_name(name)}.lock")

    def log_path_for(self, name: str, started: datetime) -> Path:
        filename = f"{validate_job_name(name)}-{started.strftime('%Y%m%d-%H%M%S')}.log"
        return _contained_file(self.logs_dir, filename)

    @staticmethod
    def _read_lock_info(lock: Path) -> dict:
        try:
            text = lock.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"pid=(\d+)", text)  # legacy plaintext locks
            return {"pid": int(match.group(1))} if match else {}

    def _lock_is_stale(self, lock: Path) -> bool:
        """Stale = heartbeat stopped AND owner process gone (or another
        host, where liveness cannot be checked and the heartbeat rule
        alone decides after the grace period). Age alone never suffices
        while heartbeats continue."""
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return True
        if age < _HEARTBEAT_STALE_SECONDS:
            return False
        info = self._read_lock_info(lock)
        if info.get("host") in (None, socket.gethostname()):
            return not _pid_alive(int(info.get("pid") or 0))
        return True

    def acquire_lock(self, name: str) -> Path:
        lock = self._lock_path(name)
        token = secrets.token_hex(8)
        with self._store_lock():  # serialise the check-and-create decision
            if lock.exists():
                if not self._lock_is_stale(lock):
                    info = self._read_lock_info(lock)
                    raise JobError(
                        f"Job '{name}' appears to be running already "
                        f"(pid {info.get('pid', '?')}, lock: {lock}). "
                        "If no run is active, delete the lock file."
                    )
                logger.warning(
                    "Removing stale lock for job '%s' (owner process gone, heartbeat stopped)",
                    name,
                )
                lock.unlink(missing_ok=True)
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                raise JobError(f"Job '{name}' is already running.") from exc
            with os.fdopen(fd, "w") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "host": socket.gethostname(),
                            "token": token,
                            "started": datetime.now(UTC).isoformat(),
                        }
                    )
                )
        self._lock_tokens[lock] = token
        return lock

    def release_lock(self, lock: Path) -> None:
        """Remove the lock only if it is still ours (token match)."""
        token = self._lock_tokens.pop(lock, None)
        if token is not None:
            info = self._read_lock_info(lock)
            if info.get("token") not in (None, token):
                return  # a recovered/newer run owns it now; leave it alone
        lock.unlink(missing_ok=True)


def _apply_retention(store: JobStore, job: Job, current_entry: dict | None = None) -> list[str]:
    """Delete outputs of older successful runs beyond retention_keep.

    Only files recorded in this job's own history are ever removed, and
    any path that also belongs to a *retained* run is protected — so a
    repeated output filename (e.g. with --overwrite) can never cause
    the newest report to delete itself.
    """
    if job.retention_keep is None:
        return []
    successful = [
        e for e in store.history(job=job.name, limit=10_000) if e.get("status") == "success"
    ]
    if current_entry is not None:
        successful = [current_entry, *successful]

    def _key(output: str) -> str:
        return str(Path(output).resolve())

    retained: set[str] = set()
    for entry in successful[: job.retention_keep]:
        retained.update(_key(o) for o in entry.get("outputs", []))
    removed: list[str] = []
    already: set[str] = set()
    for entry in successful[job.retention_keep :]:
        for output in entry.get("outputs", []):
            key = _key(output)
            if key in retained or key in already:
                continue
            already.add(key)
            path = Path(output)
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:
                logger.warning("Retention could not remove %s: %s", path, exc)
    return removed


def _parse_run_output(stdout: str) -> dict:
    """Extract the CLI's JSON result object (success or failure).

    Success output is one compact line, but error output is Rich
    pretty-printed JSON spanning several lines — so scan for the last
    top-level object that decodes and looks like a run result."""
    decoder = json.JSONDecoder()
    starts = [
        index
        for index, char in enumerate(stdout)
        if char == "{" and (index == 0 or stdout[index - 1] == "\n")
    ]
    for index in reversed(starts):
        try:
            payload, _end = decoder.raw_decode(stdout[index:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and ("outputs" in payload or "error" in payload):
            return payload
    return {}


def _heartbeat(lock: Path, stop: threading.Event) -> None:
    while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
        try:
            os.utime(lock, None)
        except OSError:
            return


def run_job(store: JobStore, name: str) -> dict:
    """Run one job in a subprocess; record history; return the entry.

    The job lock is released even if writing history fails, and
    retention cleanup happens while the lock is still held.
    """
    job = store.get_job(name)
    config_path = Path(job.config)
    if not config_path.exists():
        raise JobError(f"Job '{name}' points at a missing config: {config_path}")
    lock = store.acquire_lock(name)
    started = datetime.now(UTC)
    log_path = store.log_path_for(name, started)
    manifest_path = log_path.with_suffix(".manifest.json")
    entry: dict = {
        "job": name,
        "started_at": started.isoformat(),
        "status": "failed",
        "outputs": [],
        "error": None,
        "log": str(log_path),
        "manifest": str(manifest_path),
        "duration_seconds": None,
    }
    command = [
        sys.executable,
        "-m",
        "reportgen",
        "--quiet",
        "generate",
        str(config_path),
        "--json",
        "--manifest",
        str(manifest_path),
    ]
    for fmt in job.formats:
        command += ["--format", fmt]
    if job.output_dir:
        command += ["--output-dir", job.output_dir]
    if job.overwrite:
        command += ["--overwrite"]
    for key, value in sorted((job.parameters or {}).items()):
        command += ["--param", f"{key}={value}"]

    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(lock, heartbeat_stop), daemon=True)
    heartbeat.start()
    history_error: Exception | None = None
    try:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=job.timeout_seconds,
                check=False,
            )
            log_path.write_text(
                f"$ {' '.join(command)}\n\nSTDOUT:\n{completed.stdout}\n\n"
                f"STDERR:\n{completed.stderr}",
                encoding="utf-8",
            )
            payload = _parse_run_output(completed.stdout)
            outputs = [str(p) for p in payload.get("outputs", [])]
            if completed.returncode == 0:
                entry.update(status="success", outputs=outputs)
            else:
                entry["outputs"] = outputs  # partial outputs survive for retry/cleanup
                if payload.get("error"):
                    entry["error"] = str(payload["error"])
                else:
                    detail = (completed.stderr or completed.stdout).strip().splitlines()
                    entry["error"] = detail[-1] if detail else f"exit code {completed.returncode}"
        except subprocess.TimeoutExpired:
            entry.update(
                status="timeout", error=f"Exceeded timeout of {job.timeout_seconds} seconds."
            )
            log_path.write_text(
                f"Timed out after {job.timeout_seconds}s: {' '.join(command)}\n",
                encoding="utf-8",
            )
        finished = datetime.now(UTC)
        entry["finished_at"] = finished.isoformat()
        entry["duration_seconds"] = round((finished - started).total_seconds(), 2)
        if entry["status"] == "success":
            removed = _apply_retention(store, job, current_entry=entry)
            if removed:
                entry["retention_removed"] = removed
        try:
            store.record_history(entry)
        except Exception as exc:  # surfaced after the lock is released
            history_error = exc
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        store.release_lock(lock)
    if history_error is not None:
        raise JobError(
            f"Job '{name}' finished with status '{entry['status']}' but its "
            f"history could not be recorded: {history_error} See log: {log_path}"
        )
    if entry["status"] != "success":
        raise JobError(
            f"Job '{name}' failed ({entry['status']}): {entry['error']} See log: {log_path}"
        )
    return entry


def schedule_examples(job: Job, cron: str = "0 6 1 * *") -> dict[str, str]:
    """Return ready-to-paste scheduler snippets for every platform.

    The job name is validated first because it is embedded in shell
    commands below."""
    name = validate_job_name(job.name)
    command = f"reportgen jobs run {name}"
    return {
        "cron (Linux/macOS)": f"{cron} {command} >> ~/.reportgen/logs/{name}-cron.log 2>&1",
        "Windows Task Scheduler": (
            f'schtasks /Create /TN "reportgen-{name}" /SC MONTHLY /D 1 /ST 06:00 /TR "{command}"'
        ),
        "launchd (macOS)": (
            "Create ~/Library/LaunchAgents/com.reportgen." + name + ".plist with "
            f'ProgramArguments = ["reportgen", "jobs", "run", "{name}"] and a '
            "StartCalendarInterval; see docs/automation.md for a full example."
        ),
    }
