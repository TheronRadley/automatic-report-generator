# Development

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres]"
pytest
```

## Architecture

Local-first, modular, one-directional dependencies (CLI → builder → sources/processing/charts → exporters; exporters depend only on the document model):

```
src/reportgen/
├── cli/app.py            Typer CLI; thin: parse, call, present
├── config/               Pydantic schema (models.py) + YAML/TOML/JSON loader (safe_load only)
├── sources/              Adapters behind factory.load_dataset(); shared validation in base.py
├── processing/           Declarative step engine (AST-whitelist calculations) + aggregation/validation helpers
├── charts/engine.py      Matplotlib renderers; themes; missing-data accounting
├── reports/
│   ├── model.py          Format-neutral ReportDocument
│   ├── templates.py      Five TemplateDefs + default pipelines/metrics/sections + example YAML
│   └── builder.py        Load → defaults → pipelines → metrics → sections/charts; run_report() orchestrates exports
├── exporters/            base.py = atomic no-clobber publish; pdf/html/excel writers; bundled Jinja template
├── automation/jobs.py    JobStore (JSON), locks, history, subprocess runner, retention, scheduler snippets
└── utils/                security.py (paths, SQL validation, masking, formula-injection guard) + formatting
```

Design rules: data processing is separated from presentation (one `ReportDocument` feeds all exporters, so formats cannot diverge); expected failures raise typed `ReportGenError` subclasses with exit codes, and the CLI renders them without stack traces; no new abstraction without a second user; public interfaces are typed.

### Adding things

- **Source adapter:** implement `load_*(...) -> LoadedDataset` in `sources/`, finish with `finalise_frame`, register in `factory.py` + `SourceType`, add adapter tests.
- **Processing step:** add `_step_*` + entry in `_STEPS` in `processing/engine.py`, validate columns via `require_columns`, document in docs/configuration.md, test correctness + error cases.
- **Template:** add a defaults builder + `TemplateDef` (with `example_yaml`) in `reports/templates.py`; add sample data via `examples/generate_demo_data.py` and a config under `examples/configs/`; the test-suite will require it to actually run.
- **Exporter:** implement `write_*(document, path)` and register it in `exporters/base.py` to inherit atomic publication.

## Tests & quality gates

```bash
pytest --cov=src/reportgen        # unit + integration; tests never touch real user state (tmp dirs, REPORTGEN_HOME isolation)
ruff check src tests examples benchmarks
ruff format --check src tests examples benchmarks
REPORTGEN_TEST_PG_URL=postgresql://... pytest -m postgres   # live DB test (CI service container)
python benchmarks/run_benchmarks.py                          # rewrites docs/benchmarks.md
python -m build                                               # sdist + wheel; CI also smoke-installs the wheel
```

Deterministic synthetic fixtures only; end-to-end tests recompute figures independently with pandas and compare against rendered HTML/Excel values.

## CI (`.github/workflows/ci.yml`)

- **lint:** Ruff check + format check.
- **test:** Ubuntu/Windows/macOS × Python 3.11/3.12/3.13, with coverage on the primary leg.
- **postgres:** PostgreSQL 16 service container, runs the `postgres`-marked test.
- **build:** `python -m build`, installs the wheel into a clean venv, runs `reportgen --help` and the demo generation as a smoke test.
- **audit:** `pip-audit` over the installed environment.
- **Dependabot** (`.github/dependabot.yml`) proposes weekly pip + Actions updates.

Recommended branch protection for `main` (repository settings, cannot be committed): require PR reviews, require the `lint`, `test`, and `build` status checks, require branches to be up to date, and disable force pushes.

## Versioning & releases

Semantic versioning; the version lives in `pyproject.toml` and `src/reportgen/__init__.py` (keep them in sync). Update `CHANGELOG.md` per release, tag `vX.Y.Z`, and publish the artifacts from `python -m build`. Report-definition schema changes require a `version` bump strategy in `config/models.py` rather than silent breakage.
