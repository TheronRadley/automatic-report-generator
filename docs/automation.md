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

- **No overlap:** a lock file (`O_EXCL` create) refuses a second concurrent run of the same job. Locks older than one hour are treated as stale (e.g. after a power loss), removed with a warning, and the run proceeds.
- **Timeout:** `--timeout` seconds (default 600); an overrun kills the subprocess and records `status: timeout`.
- **Date-based names:** `{date}`/`{datetime}` in `exports.filename` keep scheduled outputs distinct without `overwrite`.
- **Failure reporting:** every run appends to `history.jsonl` (`success|failed|timeout`, outputs, error, duration) and writes full stdout/stderr to `logs/`. `reportgen history list` (or `--json` for monitoring scripts) surfaces failures. Exit codes are non-zero on failure, so schedulers and CI notice.
- **Safe reruns:** exports are atomic and no-clobber by default (see docs/security.md); re-running a date-named job the same day needs the job's `--overwrite` flag if you really want replacement.
- **Retention (opt-in only):** `jobs add --retention N` keeps only the most recent N successful runs' files. Deletion is limited to files recorded in that job's own history — nothing is ever deleted by default, and unrelated files are never touched.

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
