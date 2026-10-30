# Security

reportgen treats every input — data files, report definitions, database rows, filenames — as untrusted. This document lists what is enforced, where, and what remains your responsibility. Passing tests do not make software "secure"; this is a summary of deliberate controls, not a guarantee.

## Controls

| Threat | Control |
|---|---|
| Arbitrary code execution | No `eval`/`exec` anywhere. Calculated columns use an AST-whitelist evaluator (arithmetic between columns/numbers only; calls, attributes, subscripts rejected; huge scalar exponents refused). YAML via `yaml.safe_load` only (tested with a `!!python/object` payload). Jobs run via an argv list, never a shell |
| Unsafe templates | HTML uses a bundled template with Jinja2 `SandboxedEnvironment` + autoescaping; all user values are data, never template source. Custom Jinja files cannot be loaded. PDF text is XML-escaped before ReportLab's markup parser |
| SQL injection / unintended writes | Values are always SQLAlchemy bind parameters (and parameter **values** are suppressed in errors — names only). Queries must be a single `SELECT`/`WITH` with no write keywords or comments; the `table:` shortcut validates and quotes identifiers; PostgreSQL sessions are `READ ONLY` with `statement_timeout`; SQLite opens `mode=ro` for file paths *and* URLs (write modes rejected, missing files never created). Only SQLite/PostgreSQL dialects are accepted at all |
| HTML/script injection | Autoescaping on every interpolated value (tested with a `<script>` title); output is a single self-contained file with inline CSS and base64 chart images — no external requests, no scripts at all |
| Spreadsheet formula injection | String cells beginning with `= + - @` are quote-prefixed in Excel output (tested) |
| Path traversal | Output filenames are sanitised to a single safe component and the resolved path is verified to stay inside the output directory (tested with `../../escape`) |
| Accidental overwrites | Exports write to a hidden temp file in the target directory and publish atomically: `os.replace` only with explicit overwrite, otherwise an atomic hardlink that fails if the target appeared concurrently. Failed exports remove their temp files and never touch earlier reports |
| Unbounded input | Per-file size limit (`options.max_input_mb`, default 200 MB) checked before reading; `max_rows` enforced **during** loading (CSV/Excel/JSONL/DB), bounded Excel decompression (`max_expanded_mb`) and worksheet size (`max_cells`), merge row-growth predicted and capped before joining, chart point caps, job timeouts, DB connect/statement timeouts |
| Secret leakage | Credentials come from environment variables (`url_env`, `${VAR}`); URLs are masked in logs/errors — userinfo **and** sensitive query/fragment parameters (`password`, `token`, `api_key`, ...); error text is scrubbed of URL/key-value secrets; query parameters and dataset contents are never logged; run manifests store hashes, never values; example files contain no credentials |
| Dangerous deletion | Retention is off unless explicitly configured, and then only deletes files recorded in that job's own run history |
| Concurrent corruption | Per-job `O_EXCL` lock files with stale-lock recovery; atomic publishes |

## Your responsibilities

- **Least-privilege database accounts.** Query validation is defence-in-depth (it is intentionally conservative and may reject a keyword inside a string literal). Grant `SELECT` only.
- **Malformed/hostile files.** Size limits and strict parsers reduce risk, but deeply hostile XLSX/CSV content ultimately exercises pandas/OpenPyXL code paths — keep dependencies updated (Dependabot + `pip-audit` in CI help) and prefer trusted data pipelines for sensitive contexts.
- **Input paths** may be absolute by design (shared data folders are legitimate). Anyone who can edit a report definition can read any file the executing account can read — treat definitions as code-adjacent and review them like scripts.
- **Output sensitivity.** Reports are published with 0644 permissions so they can be shared; write to appropriately permissioned directories for sensitive data.
- **The executing account** should have write access only where reports belong.

## Reporting issues

Open a GitHub issue without exploit details for sensitive findings, or contact the maintainer privately first for anything credential- or execution-related.
