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
