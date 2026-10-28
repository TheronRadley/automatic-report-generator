"""The ``reportgen`` command-line interface.

Conventions: ordinary user errors print one clear line to stderr and
exit non-zero (no stack traces); ``--json`` switches supported commands
to machine-readable output on stdout; ``--verbose``/``--quiet`` control
diagnostic logging on stderr.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from reportgen import __version__
from reportgen.automation.jobs import Job, JobStore, run_job, schedule_examples
from reportgen.config.loader import load_config
from reportgen.errors import ConfigError, ReportGenError
from reportgen.logging_config import configure_logging
from reportgen.reports.builder import run_report
from reportgen.reports.templates import TEMPLATES, get_template
from reportgen.sources.base import inspect_dataframe
from reportgen.sources.factory import load_from_path

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="reportgen",
    help="Automatic Report Generator — turn CSV, Excel, JSON, and database "
    "data into PDF, HTML, and Excel reports.",
    no_args_is_help=True,
)
sources_app = typer.Typer(help="Inspect and explore data sources.", no_args_is_help=True)
templates_app = typer.Typer(help="Browse the built-in report templates.", no_args_is_help=True)
jobs_app = typer.Typer(help="Saved, repeatable report jobs.", no_args_is_help=True)
history_app = typer.Typer(help="Report run history.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(templates_app, name="templates")
app.add_typer(jobs_app, name="jobs")
app.add_typer(history_app, name="history")

_STATE: dict[str, object] = {"verbose": False, "quiet": False}


class ExportFormatOption(StrEnum):
    pdf = "pdf"
    html = "html"
    xlsx = "xlsx"


@contextmanager
def _guard(json_mode: bool = False):
    """Turn expected errors into a clean message + non-zero exit."""
    try:
        yield
    except ReportGenError as exc:
        if json_mode:
            console.print_json(json.dumps({"error": str(exc), "ok": False}))
        else:
            err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"reportgen {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging to stderr."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only warnings and errors."),
    _version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    _STATE["verbose"] = verbose
    _STATE["quiet"] = quiet
    configure_logging(verbose=verbose, quiet=quiet)


@app.command("version")
def version_command(json_output: bool = typer.Option(False, "--json", help="JSON output.")) -> None:
    """Print the reportgen version."""
    if json_output:
        console.print_json(json.dumps({"name": "reportgen", "version": __version__}))
    else:
        console.print(f"reportgen {__version__}")


@app.command("init")
def init_command(
    template: str = typer.Option("sales", "--template", "-t", help="Template to start from."),
    output: Path = typer.Option(Path("report.yaml"), "--output", "-o", help="Where to write."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a starter report definition you can edit and run."""
    with _guard():
        definition = get_template(template)
        if output.exists() and not force:
            raise ConfigError(f"{output} already exists. Re-run with --force or choose --output.")
        output.write_text(definition.example_yaml, encoding="utf-8")
        console.print(f"Wrote {output} from the '{definition.name}' template.")
        console.print(
            "Next steps:\n"
            "  1. Edit the source path(s) to point at your data.\n"
            f"  2. reportgen validate {output}\n"
            f"  3. reportgen generate {output} --all"
        )


@app.command("validate")
def validate_command(
    config: Path = typer.Argument(..., help="Report definition (YAML/TOML/JSON)."),
    check_sources: bool = typer.Option(
        True,
        "--check-sources/--no-check-sources",
        help="Also load every source and verify expected columns.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Validate a report definition (and optionally its data sources)."""
    with _guard(json_output):
        parsed, base_dir = load_config(config)
        get_template(parsed.report.template)  # unknown template is an error
        source_info: list[dict] = []
        if check_sources:
            from reportgen.sources.factory import load_dataset

            max_bytes = int(parsed.options.max_input_mb * 1024 * 1024)
            for source in parsed.sources:
                loaded = load_dataset(source, base_dir, max_input_bytes=max_bytes)
                source_info.append(
                    {
                        "name": source.name,
                        "type": source.type,
                        "rows": len(loaded.frame),
                        "columns": [str(c) for c in loaded.frame.columns],
                    }
                )
        result = {
            "ok": True,
            "config": str(config),
            "title": parsed.report.title,
            "template": parsed.report.template,
            "sources": source_info,
        }
        if json_output:
            console.print_json(json.dumps(result))
        else:
            console.print(
                f"[green]Valid:[/green] {config} — “{parsed.report.title}” "
                f"(template: {parsed.report.template})"
            )
            for info in source_info:
                console.print(
                    f"  • {info['name']} ({info['type']}): {info['rows']} rows, "
                    f"columns [{', '.join(info['columns'])}]"
                )


@app.command("generate")
def generate_command(
    config: Path = typer.Argument(..., help="Report definition (YAML/TOML/JSON)."),
    formats: list[ExportFormatOption] = typer.Option(
        None, "--format", "-f", help="Export format (repeatable): pdf, html, xlsx."
    ),
    all_formats: bool = typer.Option(False, "--all", help="Export pdf + html + xlsx."),
    output_dir: Path = typer.Option(None, "--output-dir", "-o", help="Override output dir."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace existing files."),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Generate a report from a report definition."""
    with _guard(json_output):
        chosen = (
            ["pdf", "html", "xlsx"]
            if all_formats
            else ([f.value for f in formats] if formats else None)
        )
        if json_output or _STATE.get("quiet"):
            outputs = run_report(
                config, formats=chosen, output_dir=output_dir, overwrite=overwrite or None
            )
        else:
            with console.status("Generating report…", spinner="dots"):
                outputs = run_report(
                    config, formats=chosen, output_dir=output_dir, overwrite=overwrite or None
                )
        if json_output:
            # Plain print keeps stdout parseable by the job runner.
            print(json.dumps({"ok": True, "outputs": [str(p) for p in outputs]}))
        else:
            for path in outputs:
                console.print(f"[green]✓[/green] {path}")
            console.print(f"Generated {len(outputs)} file(s).")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


@sources_app.command("inspect")
def sources_inspect(
    path: Path = typer.Argument(..., help="CSV, Excel, JSON, JSONL, or SQLite file."),
    table: str = typer.Option(None, "--table", help="Table name (SQLite only)."),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Show rows, columns, types, and basic statistics for a data file."""
    with _guard(json_output):
        loaded = load_from_path(path, table=table)
        info = inspect_dataframe(loaded.frame, name=path.name)
        info["source"] = loaded.description
        if json_output:
            console.print_json(json.dumps(info, default=str))
            return
        console.print(f"[bold]{path.name}[/bold] — {loaded.description}: {info['rows']} rows")
        table_view = Table(show_header=True, header_style="bold")
        for heading in ("Column", "Type", "Nulls", "Unique", "Min / sample", "Max", "Mean"):
            table_view.add_column(heading)
        for col in info["columns"]:
            table_view.add_row(
                str(col["name"]),
                str(col["dtype"]),
                str(col["nulls"]),
                str(col["unique"]),
                str(col.get("min", ", ".join(col.get("sample", [])))),
                str(col.get("max", "")),
                str(round(col["mean"], 3)) if "mean" in col else "",
            )
        console.print(table_view)


@sources_app.command("types")
def sources_types(json_output: bool = typer.Option(False, "--json")) -> None:
    """List supported source types."""
    types = [
        {"type": "csv", "notes": "Delimiter auto-detection, configurable encoding"},
        {"type": "tsv", "notes": "Tab-separated values"},
        {"type": "excel", "notes": ".xlsx workbooks, sheet selection"},
        {"type": "json", "notes": "Arrays/objects, options.record_path for nesting"},
        {"type": "jsonl", "notes": "JSON Lines (one object per line)"},
        {"type": "sqlite", "notes": "Read-only file access, SELECT queries or table"},
        {"type": "postgres", "notes": "Read-only sessions; needs the postgres extra"},
        {"type": "database", "notes": "Any SQLAlchemy URL (read-only enforced)"},
    ]
    if json_output:
        console.print_json(json.dumps({"types": types}))
    else:
        for item in types:
            console.print(f"[bold]{item['type']:9}[/bold] {item['notes']}")


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


@templates_app.command("list")
def templates_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the built-in templates."""
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "templates": [
                        {
                            "name": t.name,
                            "title": t.title,
                            "description": t.description,
                            "required_columns": t.required_columns,
                        }
                        for t in TEMPLATES.values()
                    ]
                }
            )
        )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Template")
    table.add_column("Description")
    table.add_column("Required columns")
    for template in TEMPLATES.values():
        table.add_row(
            template.name, template.description, ", ".join(template.required_columns) or "(any)"
        )
    console.print(table)


@templates_app.command("show")
def templates_show(
    name: str = typer.Argument(..., help="Template name, e.g. sales."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one template's schema and its example configuration."""
    with _guard(json_output):
        template = get_template(name)
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "name": template.name,
                        "title": template.title,
                        "description": template.description,
                        "required_columns": template.required_columns,
                        "schema_notes": template.schema_notes,
                        "themes": template.themes,
                        "example_yaml": template.example_yaml,
                    }
                )
            )
            return
        console.print(f"[bold]{template.title}[/bold] (template: {template.name})")
        console.print(template.description)
        console.print(f"Required columns: {', '.join(template.required_columns) or '(any)'}")
        console.print(f"Schema notes: {template.schema_notes}")
        console.print(f"Themes: {', '.join(template.themes)}")
        console.print(
            "\n[bold]Example configuration[/bold] "
            "(save with: reportgen init --template "
            f"{template.name})\n"
        )
        console.print(template.example_yaml)


# ---------------------------------------------------------------------------
# jobs & history
# ---------------------------------------------------------------------------


@jobs_app.command("add")
def jobs_add(
    name: str = typer.Argument(..., help="Job name, e.g. monthly-sales."),
    config: Path = typer.Argument(..., help="Report definition to run."),
    formats: list[ExportFormatOption] = typer.Option(None, "--format", "-f"),
    output_dir: Path = typer.Option(None, "--output-dir", "-o"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    timeout: int = typer.Option(600, "--timeout", help="Timeout in seconds."),
    retention: int = typer.Option(
        None,
        "--retention",
        help="Keep only the most recent N runs' files (default: keep all).",
    ),
) -> None:
    """Save a repeatable report job."""
    with _guard():
        load_config(config)  # fail fast on an invalid definition
        job = Job(
            name=name,
            config=str(config.resolve()),
            formats=[f.value for f in formats] if formats else [],
            output_dir=str(output_dir) if output_dir else None,
            overwrite=overwrite,
            timeout_seconds=timeout,
            retention_keep=retention,
        )
        JobStore().add_job(job)
        console.print(f"Saved job '{name}' → {job.config}")
        console.print(f"Run it with: reportgen jobs run {name}")


@jobs_app.command("list")
def jobs_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List saved jobs."""
    with _guard(json_output):
        jobs = JobStore().list_jobs()
        if json_output:
            console.print_json(json.dumps({"jobs": [j.__dict__ for j in jobs]}))
            return
        if not jobs:
            console.print("No jobs saved yet. Add one: reportgen jobs add NAME report.yaml")
            return
        table = Table(show_header=True, header_style="bold")
        for heading in ("Job", "Config", "Formats", "Timeout"):
            table.add_column(heading)
        for job in jobs:
            table.add_row(
                job.name,
                job.config,
                ", ".join(job.formats) or "(from config)",
                f"{job.timeout_seconds}s",
            )
        console.print(table)


@jobs_app.command("run")
def jobs_run(name: str = typer.Argument(..., help="Job name.")) -> None:
    """Run a saved job now (used by cron / Task Scheduler)."""
    with _guard():
        entry = run_job(JobStore(), name)
        for output in entry.get("outputs", []):
            console.print(f"[green]✓[/green] {output}")
        console.print(
            f"Job '{name}' completed in {entry['duration_seconds']}s. Log: {entry['log']}"
        )


@jobs_app.command("remove")
def jobs_remove(name: str = typer.Argument(..., help="Job name.")) -> None:
    """Delete a saved job (generated reports are kept)."""
    with _guard():
        JobStore().remove_job(name)
        console.print(f"Removed job '{name}'.")


@jobs_app.command("schedule")
def jobs_schedule(
    name: str = typer.Argument(..., help="Job name."),
    cron: str = typer.Option(None, "--cron", help="Cron expression override."),
) -> None:
    """Print ready-to-paste OS scheduler configuration for a job."""
    with _guard():
        store = JobStore()
        job = store.get_job(name)
        expression = cron
        if expression is None:
            cfg, _ = load_config(job.config)
            if cfg.schedule and cfg.schedule.cron:
                expression = cfg.schedule.cron
        examples = schedule_examples(job, cron=expression or "0 6 1 * *")
        console.print(f"[bold]Schedule job '{name}'[/bold] — add ONE of these:\n")
        for platform, snippet in examples.items():
            console.print(f"[bold]{platform}[/bold]\n  {snippet}\n")
        console.print("Full instructions: docs/automation.md")


@history_app.command("list")
def history_list(
    job: str = typer.Option(None, "--job", help="Only this job."),
    limit: int = typer.Option(20, "--limit", help="Most recent N runs."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent report runs (jobs)."""
    with _guard(json_output):
        entries = JobStore().history(job=job, limit=limit)
        if json_output:
            console.print_json(json.dumps({"history": entries}))
            return
        if not entries:
            console.print("No runs recorded yet. Run a job: reportgen jobs run NAME")
            return
        table = Table(show_header=True, header_style="bold")
        for heading in ("Job", "Started (UTC)", "Status", "Outputs / error"):
            table.add_column(heading)
        for entry in entries:
            detail = (
                ", ".join(entry.get("outputs", []))
                if entry.get("status") == "success"
                else str(entry.get("error") or "")
            )
            table.add_row(
                str(entry.get("job")),
                str(entry.get("started_at")),
                str(entry.get("status")),
                detail[:90],
            )
        console.print(table)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
