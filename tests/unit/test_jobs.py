"""Job store, locking, subprocess runs, history, retention."""

from __future__ import annotations

import pytest

from reportgen.automation.jobs import Job, JobStore, run_job, schedule_examples
from reportgen.errors import JobError


def _job(name="j", config="/tmp/x.yaml", **kwargs):
    return Job(name=name, config=config, **kwargs)


def test_job_crud(tmp_path):
    store = JobStore(home=tmp_path / "home")
    store.add_job(_job())
    assert [j.name for j in store.list_jobs()] == ["j"]
    with pytest.raises(JobError, match="already exists"):
        store.add_job(_job())
    with pytest.raises(JobError, match="No job named"):
        store.get_job("ghost")
    store.remove_job("j")
    assert store.list_jobs() == []


def test_lock_prevents_overlap(tmp_path):
    store = JobStore(home=tmp_path / "home")
    lock = store.acquire_lock("j")
    with pytest.raises(JobError, match="already"):
        store.acquire_lock("j")
    store.release_lock(lock)
    store.acquire_lock("j")  # works again after release


def test_run_job_success_and_history(tmp_path, minimal_sales_config):
    store = JobStore(home=tmp_path / "home")
    store.add_job(
        Job(name="demo", config=str(minimal_sales_config), formats=["html"], overwrite=True)
    )
    entry = run_job(store, "demo")
    assert entry["status"] == "success"
    assert entry["outputs"] and entry["outputs"][0].endswith(".html")
    history = store.history(job="demo")
    assert len(history) == 1
    assert history[0]["duration_seconds"] is not None
    # Safe rerun: second run also succeeds (overwrite=True on the job).
    run_job(store, "demo")
    assert len(store.history(job="demo")) == 2


def test_run_job_failure_is_recorded(tmp_path, write_config):
    bad = write_config("""
version: 1
report: {title: Broken, template: general}
sources:
  - {name: d, type: csv, path: definitely-missing.csv}
""")
    store = JobStore(home=tmp_path / "home")
    store.add_job(Job(name="broken", config=str(bad), formats=["html"]))
    with pytest.raises(JobError, match="failed"):
        run_job(store, "broken")
    history = store.history(job="broken")
    assert history[0]["status"] == "failed"
    assert history[0]["error"]


def test_retention_removes_only_old_job_outputs(tmp_path, minimal_sales_config):
    store = JobStore(home=tmp_path / "home")
    store.add_job(
        Job(
            name="demo",
            config=str(minimal_sales_config),
            formats=["html"],
            overwrite=True,
            retention_keep=1,
        )
    )
    first = run_job(store, "demo")
    first_output = first["outputs"][0]
    # Simulate an older run writing a different file, then run again.
    older = tmp_path / "older.html"
    older.write_text("old", encoding="utf-8")
    store.record_history(
        {
            "job": "demo",
            "status": "success",
            "outputs": [str(older)],
            "started_at": "2025-01-01T00:00:00+00:00",
        }
    )
    unrelated = tmp_path / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    run_job(store, "demo")
    assert not older.exists()  # pruned by retention
    assert unrelated.exists()  # unrelated files are never touched
    assert first_output.endswith(".html")


def test_schedule_examples_cover_platforms():
    examples = schedule_examples(_job(name="monthly"))
    joined = " ".join(examples.values())
    assert "cron" in " ".join(examples).lower()
    assert "schtasks" in joined
    assert "jobs run monthly" in joined


# ------------------------------------------------------- regression suite (#1/#3/#5/#17)


def test_retention_never_deletes_newest_with_repeated_filename(tmp_path, write_config):
    """#1: same output name every run + retention must keep the newest file."""
    csv = tmp_path / "d.csv"
    csv.write_text("region,revenue\nAlpha,10\n", encoding="utf-8")
    out = tmp_path / "out"
    config = write_config(
        f"""
version: 1
report: {{title: Rep, template: general}}
sources: [{{name: d, type: csv, path: {csv}}}]
exports: {{output_dir: {out}, filename: report, overwrite: true}}
"""
    )
    store = JobStore(home=tmp_path / "home")
    store.add_job(
        Job(
            name="rep",
            config=str(config),
            formats=["html"],
            overwrite=True,
            retention_keep=1,
        )
    )
    run_job(store, "rep")
    target = out / "report.html"
    assert target.exists()
    csv.write_text("region,revenue\nZetaland,99\n", encoding="utf-8")  # new content
    run_job(store, "rep")
    assert target.exists(), "retention deleted the newest report"
    assert "Zetaland" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "x; rm -rf /", "", "..", "CON", "a b"])
def test_job_names_are_validated(bad, tmp_path):
    """#3: names become filenames and scheduler text — separators rejected."""
    from reportgen.automation.jobs import validate_job_name

    with pytest.raises(JobError, match="Invalid job name"):
        validate_job_name(bad)
    store = JobStore(home=tmp_path / "home")
    with pytest.raises(JobError):
        store.add_job(_job(name=bad))


def test_lock_paths_stay_inside_store(tmp_path):
    store = JobStore(home=tmp_path / "home")
    assert store._lock_path("monthly-sales").parent == store.locks_dir


def test_live_lock_is_never_stale_by_age_alone(tmp_path):
    """#5: a heartbeating owner's lock blocks, however old the run is."""
    import json
    import os
    import socket

    store = JobStore(home=tmp_path / "home")
    store.locks_dir.mkdir(parents=True, exist_ok=True)
    lock = store.locks_dir / "j.lock"
    lock.write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "token": "x"}),
        encoding="utf-8",
    )
    old = lock.stat().st_mtime - 10_000  # heartbeat "stopped" long ago...
    os.utime(lock, (old, old))
    # ...but the owner PID (this process) is alive, so it must not reclaim.
    assert store._lock_is_stale(lock) is False
    with pytest.raises(JobError, match="already"):
        store.acquire_lock("j")


def test_dead_owner_lock_is_reclaimed(tmp_path):
    import json
    import os
    import socket

    store = JobStore(home=tmp_path / "home")
    store.locks_dir.mkdir(parents=True, exist_ok=True)
    lock = store.locks_dir / "j.lock"
    lock.write_text(
        json.dumps({"pid": 2_147_483_646, "host": socket.gethostname(), "token": "stale"}),
        encoding="utf-8",
    )
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))
    acquired = store.acquire_lock("j")  # reclaimed instead of raising
    store.release_lock(acquired)
    assert not lock.exists()


def test_lock_released_when_history_recording_fails(tmp_path, minimal_sales_config, monkeypatch):
    """#5: a history-write failure must not strand the job lock."""
    store = JobStore(home=tmp_path / "home")
    store.add_job(
        Job(name="histfail", config=str(minimal_sales_config), formats=["html"], overwrite=True)
    )

    def _boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(store, "record_history", _boom)
    with pytest.raises(JobError, match="history"):
        run_job(store, "histfail")
    assert not list(store.locks_dir.glob("*.lock"))


def test_concurrent_job_additions_are_not_lost(tmp_path):
    """#5: the store lock serialises add_job across threads/processes."""
    import threading

    store = JobStore(home=tmp_path / "home")

    def _add(i):
        store.add_job(_job(name=f"job-{i}"))

    threads = [threading.Thread(target=_add, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(j.name for j in store.list_jobs()) == [f"job-{i}" for i in range(8)]


def test_job_history_records_partial_outputs_on_failure(tmp_path, write_config, sales_csv):
    """#17: a format that fails late still records earlier created files."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "jobbed.pdf").write_text("existing")  # pdf export will refuse
    config = write_config(
        f"""
version: 1
report: {{title: Partial Job, template: general}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
exports: {{output_dir: {out}, filename: jobbed}}
"""
    )
    store = JobStore(home=tmp_path / "home")
    store.add_job(Job(name="jobbed", config=str(config), formats=["html", "pdf"]))
    with pytest.raises(JobError):
        run_job(store, "jobbed")
    history = store.history(job="jobbed")
    assert history[0]["status"] == "failed"
    assert [p.split("/")[-1] for p in history[0]["outputs"]] == ["jobbed.html"]
    assert (out / "jobbed.html").exists()
