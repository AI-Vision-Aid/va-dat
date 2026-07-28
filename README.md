# visionaid-a11y-llm-audit

LLM-powered tools that analyze websites for WCAG accessibility issues and generate structured remediation reports. A Computing for Good course project at Georgia Tech (OMSCS) partnered with the Vision Aid Digital Accessibility Testing Team.

## How It Works

The pipeline takes a raw HTML file and produces targeted accessibility findings through four steps:

```
HTML file (e.g. 1.9 MB)
    │
    │  Step 0 — Programmatic Checks (no API cost)
    │  Rule-based detection of missing alt, empty links, duplicate IDs, etc.
    │
    │  Step 1 — Extract
    │  Three extractors parse the HTML into structured JSON payloads,
    │  discarding layout noise (CSS, scripts, divs) and keeping only
    │  semantically relevant content. ~92% token reduction.
    │
    ├── semantic_checklist_01.py  → headings, links, landmarks, tables, iframes
    ├── forms_checklist_02.py    → form fields, labels, groups, instructions
    └── nontext_checklist_03.py  → images, SVGs, icon fonts, media
                                    Combined: ~39k tokens (from ~487k)
    │
    │  Step 2 — Slice & Call
    │  Each payload is sliced into targeted pieces. Each slice is paired
    │  with a focused prompt template and sent to the LLM individually.
    │  Up to 18 element-specific calls + 3 optional summary calls.
    │
    │  Step 3 — Save
    │  Raw JSON results are saved per-prompt for downstream processing.
    │
    │  Step 4 — Report
    │  The report generator reads all saved results, normalizes findings
    │  from both programmatic and LLM sources, and writes a unified CSV.
```

## Repository Structure

```
├── entry_points/                                # Pipeline entry points
│   ├── run_pipeline.py                          # Main entry point — runs the full pipeline
│   ├── generate_report.py                       # Combines findings into unified CSV report
│   └── get_visionaid_home.py                    # Downloads visionaid.org homepage
│
├── processing_scripts/
│   ├── llm/                                     # Modular prompt system + templates
│   │   ├── registry.py                          #   Maps each evaluation task to its template + slicer
│   │   ├── templates.py                         #   Parses .txt prompt files, fills {payload} placeholders
│   │   ├── slicers.py                           #   Extracts targeted JSON slices from extractor payloads
│   │   ├── semantic_checklist_01.txt            #   7 prompts for semantic structure
│   │   ├── forms_checklist_02.txt               #   6 prompts for form accessibility
│   │   └── nontext_checklist_03.txt             #   8 prompts for non-text content
│   │
│   ├── llm_client/                              # Standalone Claude API client (Andrew)
│   │   ├── client.py                            #   API wrapper
│   │   ├── prompt_loader.py                     #   Prompt loading utilities
│   │   └── runner.py                            #   End-to-end audit runner
│   │
│   ├── llm_preprocessing/                       # HTML → structured JSON extractors
│   │   ├── semantic_checklist_01.py             #   Headings, links, landmarks, tables, iframes
│   │   ├── forms_checklist_02.py                #   Form fields, label associations, groups
│   │   └── nontext_checklist_03.py              #   Images, SVGs, icon fonts, media
│   │
│   ├── programmatic/                            # Rule-based checks (no LLM needed)
│   │   ├── semantic_checklist_01.py             #   Semantic structure checks
│   │   ├── forms_checklist_02.py                #   Form accessibility checks
│   │   └── nontext_checklist_03.py              #   Non-text content checks
│   │
│   ├── docs/pipeline.md                         # Pipeline architecture documentation
│   └── accessibility_audit_walkthrough.ipynb    # Andrew's end-to-end audit notebook
│
├── test_files/                                 # EXISTING — HTML files to analyze
│   ├── home.html                               #   visionaid.org homepage (~1.9 MB)
│   └── dat_visionaid_home.html                 #   Smaller trimmed variant (~143 KB)
│
├── semantic_checklist/                         # EXISTING — Source of truth: Deque WCAG checklist PDFs
│   ├── 01-semantic-checklist.pdf
│   ├── 02-forms-checklist.pdf
│   └── 03-nontext-checklist.pdf
│
├── vision_aid/ingestion/                       # EXISTING — HTML download utility
│   └── pull_html.py
│
├── pipeline_walkthrough.ipynb                  # Colab-compatible step-by-step pipeline notebook
│
├── docs/                                       # Architecture documentation
│   └── modular-prompts-plan.md                 #   Full architectural plan
│
├── reports/                                    # LLM audit result files
│   └── audit_*.json                            #   Raw JSON output from llm_client runs
│
├── test_results/
│   ├── chatgpt/                                # Legacy ChatGPT testing results
│   └── claude/                                 # Pipeline-generated CSV reports
│       └── report_YYYY-MM-DD.csv
│
└── output/                                     # Generated at runtime (not committed)
    ├── manifest.json
    ├── programmatic_findings.json
    ├── payloads/
    └── prompts/
```

## Architecture Deep-Dive

### Extractors (existing code)

Each extractor has an `extract(file_path)` function that parses HTML with BeautifulSoup and returns a structured dict:

| Extractor | Focus | Output tokens (visionaid.org) |
|-----------|-------|-------------------------------|
| `semantic_checklist_01.py` | Page title, headings, links, landmarks, tables, iframes | ~17,600 |
| `forms_checklist_02.py` | Form fields with label source, instructions, required flags | ~2,500 |
| `nontext_checklist_03.py` | Images (4 categories), SVGs, icon fonts, video/audio | ~19,200 |

These files live in `processing_scripts/llm_preprocessing/` and were authored by ahildebrandt3 and Andrew Yin. They should not need modification unless a new checklist (CL04+) is added.

### Prompt Registry Pattern (new)

The core of the modular system is in `processing_scripts/llm/`:

- **`registry.py`** — Defines 21 `PromptSpec` dataclass entries, each linking a prompt name to its template file, slicer function, WCAG criteria, and output type. This is the single source of truth for what the pipeline evaluates.

- **`slicers.py`** — Contains one function per prompt (e.g., `slice_headings()`, `slice_flagged_links()`) that extracts exactly the data that prompt needs from the full extractor payload. This is what achieves the token reduction.

- **`templates.py`** — Parses the `.txt` prompt template files (which contain multiple numbered prompts separated by dashed headers) and fills in the `{payload}` placeholder with the sliced JSON at runtime.

### Pipeline Orchestrator (new)

`entry_points/run_pipeline.py` ties everything together:

1. Runs the three extractors to get structured payloads
2. Runs programmatic checks on the CL01 payload
3. Iterates over the prompt registry, slicing payloads and assembling prompts
4. Calls the Anthropic API for each non-empty prompt (or saves dry-run output)
5. Writes a manifest with token counts, timing, and cost data

### Report Generator (new)

`entry_points/generate_report.py` reads the pipeline output and produces a flat CSV:

1. Loads `manifest.json` for run metadata (date, model)
2. Normalizes `programmatic_findings.json` (59 rule-based issues) into report rows
3. For each `output/prompts/*.json`, applies a prompt-specific normalizer that understands the response schema and extracts issues
4. Assigns sequential IDs and writes to `test_results/claude/report_YYYY-MM-DD.csv`

The normalizer registry mirrors the prompt registry — one normalizer function per prompt type that knows how to detect issues in that prompt's response shape.

## How to Extend

### Adding a new prompt type

1. **Slicer** — Add a function in `processing_scripts/llm/slicers.py` that extracts the relevant data from the extractor payload
2. **Template** — Add a new numbered prompt section to the appropriate `.txt` file in `processing_scripts/llm/`
3. **PromptSpec** — Add an entry in `processing_scripts/llm/registry.py` linking the slicer, template, and WCAG criteria
4. **Normalizer** — Add a normalizer function in `entry_points/generate_report.py` and register it in the `NORMALIZERS` dict

### Adding a new extractor/checklist (CL04+)

1. Create a new extractor in `processing_scripts/llm_preprocessing/` with an `extract(file_path)` function
2. Create corresponding prompt templates in `processing_scripts/llm/`
3. Add slicer functions in `processing_scripts/llm/slicers.py`
4. Register new `PromptSpec` entries in `processing_scripts/llm/registry.py`
5. Add normalizers in `entry_points/generate_report.py`
6. Update `entry_points/run_pipeline.py` to call the new extractor

## Setup

Requires Python 3.11+.

```bash
python -m venv venv
source venv/bin/activate        # Linux/macOS
# or: venv\Scripts\activate     # Windows

pip install -e .
```

Create a `.env` file with your provider API key(s) (only needed for live runs, not dry-run):

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
```

## Running the Pipeline

### Dry run (no API calls, no cost)

Generates all prompts and saves them as JSON files so you can inspect them before spending money:

```bash
python entry_points/run_pipeline.py --html test_files/dat_visionaid_home.html --dry-run
```

### Live run

Sends prompts to the LLM and saves responses:

```bash
python entry_points/run_pipeline.py --html test_files/home.html
```

### Generate report

After a live run, combine all findings into a single CSV:

```bash
python entry_points/generate_report.py
python entry_points/generate_report.py --output-dir ./output --report-dir ./test_results/claude/
```

### Pipeline options

| Flag | Default | Description |
|------|---------|-------------|
| `--html` | (required) | Path to the HTML file to analyze |
| `--output-dir` | `./output` | Directory for results |
| `--model` | `claude-sonnet-4-20250514` | Anthropic model to use |
| `--dry-run` | off | Generate prompts without calling the API |
| `--include-summaries` | off | Include the 3 cross-cutting summary prompts |
| `--show-cost` | off | Print estimated dollar cost of the run based on model pricing |
| `--env-file` | `.env` | Path to environment file |

### Report generator options

| Flag | Default | Description |
|------|---------|-------------|
| `--output-dir` | `./output` | Directory containing pipeline output |
| `--report-dir` | `./test_results/claude/` | Directory to write the CSV report |

## Output Structure

### Pipeline output (`output/`)

```
output/
├── manifest.json                 # Run metadata, token counts, prompt status
├── programmatic_findings.json    # Rule-based checker results (free)
├── payloads/                     # Raw extractor output (for inspection)
│   ├── cl01_payload.json
│   ├── cl02_payload.json
│   └── cl03_payload.json
└── prompts/                      # One file per prompt
    ├── page_title.json           # Contains prompt text, payload slice, and API response
    ├── heading_structure.json
    ├── link_clarity.json
    └── ...
```

### Report output (`test_results/claude/`)

The report CSV has 13 columns matching the Vision Aid team's standard format:

| Column | Description |
|--------|-------------|
| `ID` | Sequential row number |
| `element_name` | HTML element (e.g. `<img class="...">`, `<a> "link text"`) |
| `browser_combination` | Always `N/A` (static HTML analysis) |
| `page_title` | Page title from the analyzed HTML |
| `issue_title` | Short issue description |
| `steps_to_reproduce` | Element snippet or inspection steps |
| `actual_result` | What was found |
| `expected_result` | What WCAG requires |
| `recommendation` | Suggested fix |
| `wcag_sc` | WCAG success criterion (e.g. `1.1.1`) |
| `category` | Issue category (e.g. `Programmatic / Non-text Content`) |
| `log_date` | Date of the pipeline run |
| `reported_by` | `Programmatic` or the LLM model string |

## Cost Estimate

For visionaid.org homepage (using Claude Sonnet):

| Approach | Input tokens | Cost |
|----------|-------------|------|
| Monolithic (entire HTML) | ~487,000 | ~$1.52 |
| Element-specific pipeline | ~18,000 | ~$0.32 |

The pipeline skips prompts with empty payloads (e.g., no forms on the page = no form prompts), so actual cost varies by page content.

## Attribution

| Contributor | What they own | Key files |
|---|---|---|
| ahildebrandt3 | Extractors, programmatic checkers (CL01–CL03), CL01 prompts | `processing_scripts/llm_preprocessing/`, `processing_scripts/programmatic/` |
| Andrew Yin | CL02 + CL03 extractors, CL02 + CL03 prompts, LLM client, pipeline docs | `processing_scripts/llm_preprocessing/`, `processing_scripts/llm_client/`, `processing_scripts/llm/*.txt` |
| nfulton99 | HTML ingestion, packaging | `vision_aid/ingestion/pull_html.py`, `pyproject.toml` |
| ColeANiblett | Pipeline orchestration, prompt system, report generator | `processing_scripts/llm/{registry,slicers,templates}.py`, `entry_points/`, `docs/` |
