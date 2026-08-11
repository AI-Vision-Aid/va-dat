# Project: P5 – Automating Digital Accessibility

## What This Project Does

This project builds LLM-powered tools that analyze websites for WCAG accessibility issues and generate structured remediation reports. It is a Computing for Good course project at Georgia Tech (OMSCS) partnered with the Vision Aid Digital Accessibility Testing Team.

The system has three planned stages:
1. Automated webpage accessibility analysis → structured CSV reports
2. Code remediation suggestions based on those reports
3. (Stretch) Chrome extension for on-the-fly accessibility fixes

## Architecture Overview

```
project-root/
├── index.html                       # Web UI + team site (served by api_server.py)
├── styles.css
├── entry_points/
│   ├── api_server.py                # HTTP server: static site + /api/* audit endpoints
│   ├── run_pipeline.py              # CLI pipeline orchestrator
│   └── generate_report.py           # Pipeline output → unified CSV
├── processing_scripts/
│   ├── llm/                         # Modular prompt system
│   │   ├── registry.py              #   PromptSpec dataclass + PROMPT_REGISTRY
│   │   ├── slicers.py               #   Payload slicer functions (one per prompt)
│   │   ├── filters.py               #   Pass-1 payload filters
│   │   ├── templates.py             #   Prompt .txt parser + {payload} filler
│   │   └── *_checklist_0*.txt       #   Prompt templates (CL01/CL02/CL03)
│   ├── llm_preprocessing/           # HTML → structured JSON extractors
│   ├── programmatic/                # Rule-based checks (no API needed)
│   └── llm_client/                  # Provider client (Anthropic/OpenAI/Gemini)
├── vision_aid/ingestion/
│   ├── file_crawler.py              # fetch_page / fetch_pages_nested (used by the server)
│   └── pull_html.py                 # Standalone HTML download helper
├── Dockerfile                       # Multi-stage uv build → runtime image
├── docker-compose.yml               # Local run; Coolify deploys the image directly
├── DEPLOY.md                        # Coolify deployment notes (proxy settings matter)
├── .github/workflows/
│   ├── ci.yml                       # On PR to main: deps, imports, pipeline, image smoke
│   └── publish.yml                  # On push to main: build → GHCR → trigger Coolify
├── test_files/                      # HTML inputs (may be very large, 500K+ tokens)
└── docs/modular-prompts-plan.md
```

## Environment & Dependencies

This project uses **uv**. There is no `pip install` step and no hand-managed `venv`.

```bash
uv sync                    # create .venv and install from uv.lock
uv run python <script>     # run inside that environment
```

- `pyproject.toml` is the **only** hand-edited dependency list.
- `uv.lock` is committed and pins everything, including transitive deps.
- `requirements.txt` is a **generated export** — never edit it by hand. Regenerate with the command in its header. CI fails if it drifts from the lock.
- `.python-version` pins 3.12 to match the container. `requires-python` is `>=3.11`.

After changing a dependency: edit `pyproject.toml`, run `uv lock`, regenerate `requirements.txt`, and commit all three.

## Pipeline Usage

```bash
# Dry run (no API key needed, no cost) — generates prompts and payloads
uv run python entry_points/run_pipeline.py --html test_files/dat_visionaid_home.html --dry-run

# Full run (spends money — see the rules below)
uv run python entry_points/run_pipeline.py --html test_files/home.html

# CSV report from pipeline output
uv run python entry_points/generate_report.py

# Web UI + API at http://localhost:8000
uv run python entry_points/api_server.py
```

## The Web Server

`entry_points/api_server.py` is the single server — it serves `index.html`/`styles.css` and handles the audit endpoints. There is deliberately no second copy of the audit logic; the project previously carried two and they drifted until one silently skipped LLM deduplication.

The audit endpoints (`/api/audit`, `/api/audit/url`, `/api/audit/url/nested`) return an **NDJSON stream**: progress events one JSON object per line, then a final `{"type":"result"}`. Parsing that body with a single `JSON.parse`/`res.json()` throws "Unexpected non-whitespace character after JSON" — the front end branches on content type to handle it.

## Deployment

Docker image → GHCR (`ghcr.io/c4g/va-dat`) → Coolify at `https://va-dat.c4g.dev`.
`publish.yml` builds and pushes on merge to `main`, then triggers the Coolify deploy. Coolify runs the **image**, not `docker-compose.yml` — the hardening in that compose file (`read_only`, `tmpfs`) applies to local runs only. See `DEPLOY.md`, especially the proxy buffering/timeout section: buffering breaks the progress stream, and short read timeouts cut off long audits.

## Code Patterns

- Prompt templates are `.txt` files with `{payload}` placeholders, parsed by `processing_scripts/llm/templates.py`
- `processing_scripts/llm/registry.py` maps each evaluation task to its prompt file, slicer, and WCAG criteria
- Slicer functions in `processing_scripts/llm/slicers.py` extract targeted JSON slices from extractor payloads
- `entry_points/run_pipeline.py` orchestrates: programmatic checks → extraction → slicing → prompt filling → API calls
- File encoding: use `encoding='utf-8', errors='replace'` when reading HTML files

## Style Rules

- Python: standard PEP 8
- Docstrings for all new functions
- Use `pathlib.Path` for file paths (consistent with existing scripts)
- Commit messages: descriptive, prefixed with area (e.g., "prompts: add element-specific template system")
- `index.html` contains the entire front end inline (HTML + CSS + JS). Keep it dependency-free — no frameworks, no external CDN assets.

## Do NOT

- **Do not call any LLM API or spend money unless explicitly asked to test.** Prefer `--dry-run`.
- Do not install or remove dependencies without team agreement.
- Do not modify `test_files/` or any HTML input files.
- Do not edit `requirements.txt` by hand — regenerate it.
- Do not add `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}` (or similar) to `docker-compose.yml`. Compose auto-loads `./.env` for `${...}` interpolation, so that line silently injects a developer's personal key into the container and bills it for every anonymous audit.

## Common Issues

- **A dry run can still spend money.** `dry_run` is derived from whether a key resolves, and `_resolve_api_key` falls back to the environment — and `api_server.py` calls `load_dotenv()`. Sending `api_key: ""` from a client does **not** force a dry run when `.env` exists. To guarantee no spend, run with no key available at all.
- **Silent dry runs.** A request with no resolvable key returns `200 OK` with no LLM findings and no CSV. The only signal is `summary.dry_run` in the response.
- HTML test files can be enormous (500K+ tokens) — do not read them fully into context.
- Some files may have non-UTF-8 encoding; always use `errors='replace'`.
- Model defaults differ by entry point: `run_pipeline.py` uses `claude-sonnet-5`, `api_server.py` uses `claude-haiku-4-5-20251001`.
