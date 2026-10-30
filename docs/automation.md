# Automation

reportgen has no background daemon. Instead, saved **jobs** are executed by your operating system's scheduler (cron, Windows Task Scheduler, launchd), which is simpler, observable, and survives reboots. Each run executes in a subprocess with a hard timeout and its own log file.

State lives in `~/.reportgen/` (override with the `REPORTGEN_HOME` environment variable — useful for service accounts and tests):

```
~/.reportgen/
├── jobs.json        # saved jobs
├── history.jsonl    # one JSON object per run
├── locks/           # <job>.lock while a run is active
└── logs/            # <job>-<timestamp>.log per run
```

## Jobs

```bash
reportgen jobs add monthly-sales examples/configs/sales-report.yaml --overwrite --timeout 600
reportgen jobs list
reportgen jobs run monthly-sales     # what schedulers invoke
reportgen history list --job monthly-sales
reportgen jobs remove monthly-sales  # generated reports are kept
```

Guarantees:

- **No overlap:** a lock file (`O_EXCL` create, containing PID/host/token) refuses a second concurrent run of the same job. Stale recovery is liveness-based, not age-based: a run heartbeats its lock, and a lock is only reclaimed when it cannot be a live run (dead local PID, heartbeat stopped beyond the job's timeout, or a different host with a dead heartbeat). The lock is released even if recording history fails; job-store writes are serialised so concurrent `jobs add`/`jobs run` cannot corrupt state. Job names are validated (`[A-Za-z0-9._-]`, no path separators) because they name lock/log files and appear in scheduler examples.
- **Timeout:** `--timeout` seconds (default 600); an overrun kills the subprocess and records `status: timeout`.
- **Date-based names:** `{date}`/`{datetime}` in `exports.filename` keep scheduled outputs distinct without `overwrite`.
- **Failure reporting:** every run appends to `history.jsonl` (`success|failed|timeout`, outputs, error, duration) and writes full stdout/stderr to `logs/`. If some formats succeeded before the failure, those files are kept **and recorded in the history entry**, so retries know exactly what exists. Each run also writes a run manifest next to its log (`logs/<job>-<timestamp>.manifest.json`) with input/config fingerprints, row counts, warnings and per-output status. `reportgen history list` (or `--json` for monitoring scripts) surfaces failures. Exit codes are non-zero on failure, so schedulers and CI notice.
- **Safe reruns:** exports are atomic and no-clobber by default (see docs/security.md); re-running a date-named job the same day needs the job's `--overwrite` flag if you really want replacement.
- **Retention (opt-in only):** `jobs add --retention N` keeps only the most recent N successful runs' files. Deletion is limited to files recorded in that job's own history — nothing is ever deleted by default, and unrelated files are never touched. Any file belonging to a *retained* run is protected, so with a repeated output filename (e.g. `--overwrite`) the newest report can never delete itself.
- **Parameters:** `jobs add --param from_date=previous_month_start --param to_date=previous_month_end` passes date-range parameters to every run; tokens resolve at run time, so the same job covers each new period with no edits (see `parameters` in docs/configuration.md).

## Scheduling

`reportgen jobs schedule <name>` prints ready-to-paste snippets (using `schedule.cron` from the report definition when present).

### Linux / macOS cron

```cron
# crontab -e   (06:00 on the 1st of each month)
0 6 1 * * reportgen jobs run monthly-sales >> ~/.reportgen/logs/monthly-sales-cron.log 2>&1
```

Use the absolute path to `reportgen` if it lives in a virtualenv (e.g. `/opt/reportgen/.venv/bin/reportgen`), since cron's `PATH` is minimal. Set database environment variables (`REPORT_DB_URL=...`) in the crontab or a sourced file with restricted permissions — never in the report definition.

### Windows Task Scheduler

```bat
schtasks /Create /TN "reportgen-monthly-sales" /SC MONTHLY /D 1 /ST 06:00 /TR "reportgen jobs run monthly-sales"
```

Or in the Task Scheduler GUI: *Create Task… → Trigger: Monthly → Action: Start a program* with program `reportgen` (full path to `reportgen.exe` inside your venv's `Scripts\` if needed) and arguments `jobs run monthly-sales`. Enable "Run whether user is logged on or not" for unattended runs, and set the account's environment variables for database credentials.

### macOS launchd

Save as `~/Library/LaunchAgents/com.reportgen.monthly-sales.plist`, then `launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.reportgen.monthly-sales</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/.venv/bin/reportgen</string>
    <string>jobs</string><string>run</string><string>monthly-sales</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Day</key><integer>1</integer><key>Hour</key><integer>6</integer></dict>
  <key>StandardOutPath</key><string>/Users/you/.reportgen/logs/launchd-out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/.reportgen/logs/launchd-err.log</string>
</dict>
</plist>
```
