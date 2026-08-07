# Modular Prompt Architecture Plan

## 1. Current State Summary

### 1.1 Repository Structure

The project has two parallel approaches to accessibility auditing, at different stages of completion:

```
va-dat/
│
├── scripts/
│   └── run_factorial_test.py          # Approach A: Monolithic (BROKEN — imports missing modules)
│
├── processing_scripts/
│   ├── llm_preprocessing/             # Approach B: Element-specific extraction (COMPLETE)
│   │   ├── semantic_checklist_01.py   #   CL01 extractor → ~17.6k tokens
│   │   ├── forms_checklist_02.py      #   CL02 extractor → ~2.5k tokens
│   │   ├── nontext_checklist_03.py    #   CL03 extractor → ~19.2k tokens
│   │   ├── docs/pipeline.md           #   Architecture documentation
│   │   └── semantic_preprocessing_walkthrough.ipynb
│   │
│   ├── llm/                           # Approach B: Element-specific prompts (COMPLETE)
│   │   ├── semantic_checklist_01.txt  #   7 prompts with {payload} placeholders
│   │   ├── forms_checklist_02.txt     #   6 prompts
│   │   └── nontext_checklist_03.txt   #   8 prompts
│   │
│   └── programmatic/
│       └── semantic_checklist_01.py   # Rule-based checker (standalone, not integrated)
│
├── prompts/                           # EMPTY — was a directory with 3 Python modules, deleted in cleanup
│
├── test_files/
│   ├── home.html                      # 1.9 MB (~487k tokens) — visionaid.org homepage
│   └── dat_visionaid_home.html        # 143 KB trimmed variant
│
├── vision_aid/ingestion/
│   └── pull_html.py                   # download_html(url, filename) — fetches raw HTML
│
├── entry_points/
│   └── get_visionaid_home.py          # Downloads visionaid.org homepage
│
├── semantic_checklist/                # Source of truth: 3 Deque WCAG PDFs
│   ├── 01-semantic-checklist.pdf
│   ├── 02-forms-checklist.pdf
│   └── 03-nontext-checklist.pdf
│
└── test_results/
    └── factorial_prompt_results/      # 18 directories from dry-run; 0 successful API calls
        └── run_summary.json           # Model: claude-opus-4-20250514, html_length: 1,947,379
```

### 1.2 Approach A — Monolithic Factorial Testing (Broken)

**File:** `scripts/run_factorial_test.py` (460 lines)

This script generates 18 prompt combinations (6 task instruction styles × 3 WCAG guideline levels × 1 report format) and sends each as a single monolithic API call containing the entire raw HTML file.

**Key functions:**

| Function | Purpose |
|----------|---------|
| `build_prompt(task_key, wcag_key, format_key, html_content)` | Concatenates task instruction + WCAG guidelines + report format + raw HTML |
| `call_claude_api(prompt, api_key, model, max_tokens)` | Calls Anthropic API; uses streaming for inputs >400k tokens |
| `save_result(output_dir, task_key, wcag_key, format_key, prompt, result)` | Saves prompt, result JSON, response text, and CSV to disk |
| `run_factorial_test(html_content, output_dir, api_key, dry_run, model)` | Iterates all 18 combinations with 1-second delay between calls |

**Status:** Broken. The script imports from three Python modules (`task_instructions`, `guideline_variations`, `report_formatting`) that were deleted from the `prompts/` directory during a repo cleanup (commit `28732bd`). The dry-run produced 18 prompt files but 0 successful API calls.

**Problems with this approach:**
- Each request sends ~487k input tokens (raw HTML + WCAG guidelines)
- At Opus pricing ($15/MTok input), each request costs ~$7.30 input alone
- 18 combinations = ~$131 input cost per test run
- Streaming required due to >10-minute processing time
- LLM must parse raw HTML layout noise (divs, styles, scripts) to find semantic content

### 1.3 Approach B — Element-Specific Pipeline (Implemented, Not Connected)

Documented in `processing_scripts/llm_preprocessing/docs/pipeline.md` and demonstrated in the walkthrough notebook. This is a three-step architecture:

**Step 1 — Extract.** Three Python extractors parse raw HTML with BeautifulSoup and return structured JSON payloads:

| Extractor | Function | Output Keys | Tokens |
|-----------|----------|-------------|--------|
| `semantic_checklist_01.py` | `extract(file_path) → dict` | language, page_title, headings, images, flagged_links, forms, buttons, landmarks, tables, iframes | ~17,600 |
| `forms_checklist_02.py` | `extract(file_path) → dict` | forms[].fields (with label_source enum, effective_label, instructions), forms[].groups, orphan_labels | ~2,500 |
| `nontext_checklist_03.py` | `extract(file_path) → dict` | images (informative/decorative/actionable/complex), svgs, icon_fonts (with sole_content flag), media | ~19,200 |

Combined: **~39,300 tokens** (92% reduction from 487k).

**Step 2 — Slice & Call.** 21 targeted prompts in `processing_scripts/llm/` — each receives a specific JSON slice and asks the LLM to evaluate one aspect:

- **CL01 — Semantic Structure** (7 prompts): page title, heading structure, link clarity, table semantics, iframe titles, landmark structure, combined summary
- **CL02 — Forms** (6 prompts): label quality, placeholder-as-label, group labels, required field indicators, form instructions, overall summary
- **CL03 — Non-text Content** (8 prompts): informative alt quality, decorative verification, actionable image alt, complex descriptions, SVG accessibility, icon font accessibility, media captions, overall summary

Each prompt uses a `{payload}` placeholder filled with the relevant JSON slice.

**Step 3 — Merge.** 21 JSON responses aggregated into a final accessibility report. This step is **not yet implemented**.

### 1.4 Programmatic Checker

**File:** `processing_scripts/programmatic/semantic_checklist_01.py`

**Function:** `analyze_html(file_path) → list`

Performs rule-based checks that don't require LLM judgment:

| Issue Code | WCAG Criterion | What It Detects |
|------------|---------------|-----------------|
| `MISSING_H1` | 1.3.1 | No `<h1>` tag on page |
| `IMG_MISSING_ALT` | 1.1.1 | `<img>` with no alt attribute at all |
| `IMG_EMPTY_ALT` | 1.1.1 | `<img alt="">` (may be incorrectly decorative) |
| `LINK_NO_TEXT` | 2.4.4 | `<a>` with no text and no aria-label |
| `DUPLICATE_ID` | 4.1.1 | Multiple elements sharing the same ID |
| `IFRAME_NO_TITLE` | 4.1.2 | `<iframe>` missing or empty title |

Output is a list of finding dicts, each with `issue_code`, `element` (tag/id/class/snippet), `checklist_item`, and `wcag` (criterion/name/level). This checker is **standalone and not integrated** with either approach.

### 1.5 Key Observations

1. **Approach B is the clear path forward.** The extractors and prompts are well-designed, reduce token cost by 92%, and target specific WCAG criteria. What's missing is the orchestration layer that ties extraction → prompt filling → API calls → report aggregation.

2. **The `prompts/` directory is dead.** The monolithic prompt modules were deleted. Rather than restoring them, the modular architecture should supersede them entirely.

3. **Prompt format is text, not code.** The 21 prompts in `processing_scripts/llm/` are plain text files with `{payload}` placeholders. The extractors are Python functions returning dicts. There is no code that connects the two.

4. **The programmatic checker overlaps with extractors.** For example, both `semantic_checklist_01.py` (extractor) and `programmatic/semantic_checklist_01.py` detect missing alt text and empty links. The programmatic checker's output can complement the LLM analysis by providing definitive binary findings.

---

## 2. Proposed Element-Specific Prompt Architecture

### 2.1 Design Principles

1. **Preserve what works.** The existing extractors and prompt text files are well-crafted. The new architecture wraps them in a coordination layer — it does not replace them.
2. **Functions over bare strings.** The monolithic system used module-level string variables. The new system uses functions that accept element data and return filled prompts.
3. **Registry-driven.** A central registry maps element types to their extractor function, relevant WCAG criteria, prompt template, and payload slicing logic.
4. **Raw JSON output.** The pipeline outputs raw JSON results from each prompt. Report formatting (CSV or otherwise) is a **separate downstream step** — this pipeline does not own the report format.
5. **Programmatic-first.** Binary issues (missing alt, duplicate IDs) are caught by the programmatic checker before LLM calls, reducing unnecessary API usage.
6. **Summary prompts are optional.** The three summary prompts (CL01-7, CL02-6, CL03-8) are wired up but only run when explicitly requested via `--include-summaries`.

### 2.2 Directory Structure

```
va-dat/
├── processing_scripts/
│   ├── llm/                       # Modular prompt system + prompt text files
│   │   ├── __init__.py
│   │   ├── registry.py            # Element type → WCAG criteria → prompt template mapping
│   │   ├── templates.py           # Functions that load and fill prompt templates
│   │   ├── slicers.py             # Functions that extract payload slices for each prompt
│   │   ├── semantic_checklist_01.txt
│   │   ├── forms_checklist_02.txt
│   │   └── nontext_checklist_03.txt
│   ├── llm_preprocessing/         # UNCHANGED — existing extractors
│   └── programmatic/              # UNCHANGED — existing rule-based checker
│
├── entry_points/
│   ├── run_pipeline.py            # Orchestrates the full element-specific pipeline
│   ├── generate_report.py         # JSON → CSV report generator
│   └── get_visionaid_home.py      # HTML downloader
│
└── docs/
    └── modular-prompts-plan.md    # THIS FILE
```

### 2.3 Registry Design

The registry is the central data structure that maps each evaluation task to its inputs, prompt, and output handling.

```python
# processing_scripts/llm/registry.py

from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class PromptSpec:
    """Defines one element-specific evaluation task."""
    name: str                          # e.g. "link_clarity"
    checklist: str                     # "CL01", "CL02", or "CL03"
    prompt_file: str                   # relative path to .txt template
    payload_slicer: str                # function name in slicers.py
    wcag_criteria: list[str]           # e.g. ["2.4.4"]
    element_types: list[str]           # e.g. ["a"]
    output_type: str                   # "array", "object", or "summary"
    is_summary: bool = False           # True for summary prompts (only run with --include-summaries)
    skip_if_empty: bool = True         # skip API call if payload slice is empty

PROMPT_REGISTRY: list[PromptSpec] = [
    # ── CL01: Semantic Structure ──────────────────────────────────────
    PromptSpec(
        name="page_title",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_page_title",
        wcag_criteria=["2.4.2"],
        element_types=["title", "h1"],
        output_type="object",
    ),
    PromptSpec(
        name="heading_structure",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_headings",
        wcag_criteria=["1.3.1", "2.4.6"],
        element_types=["h1", "h2", "h3", "h4", "h5", "h6"],
        output_type="object",
    ),
    PromptSpec(
        name="link_clarity",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_flagged_links",
        wcag_criteria=["2.4.4"],
        element_types=["a"],
        output_type="array",
    ),
    PromptSpec(
        name="table_semantics",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_tables",
        wcag_criteria=["1.3.1"],
        element_types=["table", "th", "caption"],
        output_type="array",
    ),
    PromptSpec(
        name="iframe_titles",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_iframes",
        wcag_criteria=["4.1.2"],
        element_types=["iframe"],
        output_type="array",
    ),
    PromptSpec(
        name="landmark_structure",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_landmarks",
        wcag_criteria=["1.3.1"],
        element_types=["main", "nav", "header", "footer", "aside"],
        output_type="object",
    ),
    PromptSpec(
        name="semantic_summary",
        checklist="CL01",
        prompt_file="processing_scripts/llm/semantic_checklist_01.txt",
        payload_slicer="slice_cl01_full",
        wcag_criteria=["1.3.1", "2.4.2", "2.4.4", "2.4.6", "3.1.1", "4.1.2"],
        element_types=[],
        output_type="summary",
        is_summary=True,
    ),
    # ── CL02: Forms ───────────────────────────────────────────────────
    PromptSpec(
        name="label_quality",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_fields_with_labels",
        wcag_criteria=["1.3.1", "2.4.6"],
        element_types=["input", "select", "textarea", "label"],
        output_type="array",
    ),
    PromptSpec(
        name="placeholder_as_label",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_placeholder_only_fields",
        wcag_criteria=["1.3.1"],
        element_types=["input", "select", "textarea"],
        output_type="array",
    ),
    PromptSpec(
        name="group_labels",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_form_groups",
        wcag_criteria=["1.3.1"],
        element_types=["fieldset", "legend"],
        output_type="array",
    ),
    PromptSpec(
        name="required_field_indicators",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_required_fields",
        wcag_criteria=["3.3.2"],
        element_types=["input", "select", "textarea"],
        output_type="array",
    ),
    PromptSpec(
        name="form_instructions",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_fields_with_instructions",
        wcag_criteria=["3.3.2"],
        element_types=["input", "select", "textarea"],
        output_type="array",
    ),
    PromptSpec(
        name="form_summary",
        checklist="CL02",
        prompt_file="processing_scripts/llm/forms_checklist_02.txt",
        payload_slicer="slice_cl02_full",
        wcag_criteria=["1.3.1", "2.4.6", "3.3.2"],
        element_types=[],
        output_type="summary",
        is_summary=True,
    ),
    # ── CL03: Non-text Content ────────────────────────────────────────
    PromptSpec(
        name="informative_alt_quality",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_informative_images",
        wcag_criteria=["1.1.1"],
        element_types=["img"],
        output_type="array",
    ),
    PromptSpec(
        name="decorative_verification",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_decorative_images",
        wcag_criteria=["1.1.1"],
        element_types=["img"],
        output_type="array",
    ),
    PromptSpec(
        name="actionable_image_alt",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_actionable_images",
        wcag_criteria=["1.1.1", "2.4.4"],
        element_types=["img", "a", "button"],
        output_type="array",
    ),
    PromptSpec(
        name="complex_descriptions",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_complex_images",
        wcag_criteria=["1.1.1"],
        element_types=["img"],
        output_type="array",
    ),
    PromptSpec(
        name="svg_accessibility",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_svgs",
        wcag_criteria=["1.1.1"],
        element_types=["svg"],
        output_type="array",
    ),
    PromptSpec(
        name="icon_font_accessibility",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_icon_fonts",
        wcag_criteria=["1.1.1"],
        element_types=["i", "span"],
        output_type="array",
    ),
    PromptSpec(
        name="media_captions",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_media",
        wcag_criteria=["1.2.1", "1.2.2", "1.2.3"],
        element_types=["video", "audio"],
        output_type="array",
    ),
    PromptSpec(
        name="nontext_summary",
        checklist="CL03",
        prompt_file="processing_scripts/llm/nontext_checklist_03.txt",
        payload_slicer="slice_cl03_full",
        wcag_criteria=["1.1.1", "1.2.1", "1.2.2", "1.2.3"],
        element_types=[],
        output_type="summary",
        is_summary=True,
    ),
]
```

### 2.4 Prompt Template Loading

Each `.txt` file contains multiple prompts separated by dashed headers. The template loader parses these by prompt number and fills the `{payload}` placeholder.

```python
# processing_scripts/llm/templates.py

import re
import json
from pathlib import Path

# Maps (prompt_file, prompt_index) to raw template text.
# Prompt indices follow the numbering in each .txt file (1-based).
PROMPT_INDEX = {
    # CL01
    "page_title": 1,
    "heading_structure": 2,
    "link_clarity": 3,
    "table_semantics": 4,
    "iframe_titles": 5,
    "landmark_structure": 6,
    "semantic_summary": 7,
    # CL02
    "label_quality": 1,
    "placeholder_as_label": 2,
    "group_labels": 3,
    "required_field_indicators": 4,
    "form_instructions": 5,
    "form_summary": 6,
    # CL03
    "informative_alt_quality": 1,
    "decorative_verification": 2,
    "actionable_image_alt": 3,
    "complex_descriptions": 4,
    "svg_accessibility": 5,
    "icon_font_accessibility": 6,
    "media_captions": 7,
    "nontext_summary": 8,
}


def parse_prompt_file(file_path: Path) -> dict[int, str]:
    """Parse a prompt .txt file into a dict of {prompt_number: template_text}.

    Each prompt is delimited by a header line matching the pattern:
      N) PROMPT NAME
    where N is the 1-based prompt number.
    """
    ...


def fill_template(prompt_name: str, payload_slice: dict) -> str:
    """Load the template for prompt_name and replace {payload} with JSON payload."""
    ...
```

### 2.5 Payload Slicers

Slicers extract the relevant subset from an extractor's full payload. Each slicer corresponds to one prompt in the registry.

```python
# processing_scripts/llm/slicers.py

"""Functions that extract the right JSON slice for each prompt from the full extractor payload."""

import json


# ── CL01 Slicers ─────────────────────────────────────────────────────────────

def slice_page_title(cl01_payload: dict) -> str:
    """Return just the page_title object as JSON."""
    return json.dumps(cl01_payload["page_title"], indent=2)


def slice_headings(cl01_payload: dict) -> str:
    """Return page_title + headings for hierarchy evaluation."""
    return json.dumps({
        "page_title": cl01_payload["page_title"],
        "headings": cl01_payload["headings"],
    }, indent=2)


def slice_flagged_links(cl01_payload: dict) -> str:
    """Return only the flagged links."""
    return json.dumps(cl01_payload["flagged_links"], indent=2)


def slice_tables(cl01_payload: dict) -> str:
    return json.dumps(cl01_payload["tables"], indent=2)


def slice_iframes(cl01_payload: dict) -> str:
    return json.dumps(cl01_payload["iframes"], indent=2)


def slice_landmarks(cl01_payload: dict) -> str:
    return json.dumps(cl01_payload["landmarks"], indent=2)


def slice_cl01_full(cl01_payload: dict) -> str:
    return json.dumps(cl01_payload, indent=2)


# ── CL02 Slicers ─────────────────────────────────────────────────────────────

def slice_fields_with_labels(cl02_payload: dict) -> str:
    """Return all fields that have an effective_label."""
    fields = [
        f for form in cl02_payload["forms"]
        for f in form["fields"]
        if f.get("effective_label")
    ]
    return json.dumps(fields, indent=2)


def slice_placeholder_only_fields(cl02_payload: dict) -> str:
    """Return fields where label_source is 'placeholder_only'."""
    fields = [
        f for form in cl02_payload["forms"]
        for f in form["fields"]
        if f.get("label_source") == "placeholder_only"
    ]
    return json.dumps(fields, indent=2)


def slice_form_groups(cl02_payload: dict) -> str:
    """Return all fieldset/legend groups."""
    groups = [
        g for form in cl02_payload["forms"]
        for g in form["groups"]
    ]
    return json.dumps(groups, indent=2)


def slice_required_fields(cl02_payload: dict) -> str:
    """Return fields where required is True."""
    fields = [
        f for form in cl02_payload["forms"]
        for f in form["fields"]
        if f.get("required")
    ]
    return json.dumps(fields, indent=2)


def slice_fields_with_instructions(cl02_payload: dict) -> str:
    """Return fields that have instructions (non-null aria-describedby text)."""
    fields = [
        f for form in cl02_payload["forms"]
        for f in form["fields"]
        if f.get("instructions")
    ]
    return json.dumps(fields, indent=2)


def slice_cl02_full(cl02_payload: dict) -> str:
    return json.dumps(cl02_payload, indent=2)


# ── CL03 Slicers ─────────────────────────────────────────────────────────────

def slice_informative_images(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["images"]["informative"], indent=2)


def slice_decorative_images(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["images"]["decorative"], indent=2)


def slice_actionable_images(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["images"]["actionable"], indent=2)


def slice_complex_images(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["images"]["complex"], indent=2)


def slice_svgs(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["svgs"], indent=2)


def slice_icon_fonts(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["icon_fonts"], indent=2)


def slice_media(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload["media"], indent=2)


def slice_cl03_full(cl03_payload: dict) -> str:
    return json.dumps(cl03_payload, indent=2)
```

### 2.6 Pipeline Output Format

The pipeline outputs **raw JSON** — one file per prompt that was executed, plus a manifest. Report formatting (CSV, PDF, etc.) is a **separate downstream concern** handled by a different step. This keeps the pipeline focused on extraction and evaluation.

```
output_dir/
├── manifest.json                    # Run metadata + list of results
├── programmatic_findings.json       # Rule-based checker output
├── prompts/
│   ├── page_title.json              # {prompt_name, checklist, wcag_criteria, prompt_text, payload_slice, response}
│   ├── heading_structure.json
│   ├── link_clarity.json
│   ├── ...
│   └── nontext_summary.json         # Only present if --include-summaries was used
└── payloads/
    ├── cl01_payload.json             # Full CL01 extractor output (for debugging/inspection)
    ├── cl02_payload.json
    └── cl03_payload.json
```

### 2.7 Pipeline Orchestrator

The new `run_pipeline.py` replaces the monolithic test runner with the three-step flow.

```python
# entry_points/run_pipeline.py — high-level pseudocode

def run_pipeline(html_path, output_dir, api_key, model, dry_run, include_summaries):
    """Execute the full element-specific accessibility audit pipeline."""

    # Step 0 — Programmatic checks (free, no API calls)
    programmatic_findings = analyze_html(html_path)
    save_json(programmatic_findings, output_dir / "programmatic_findings.json")

    # Step 1 — Extract structured payloads
    cl01_payload = cl01_extract(html_path)
    cl02_payload = cl02_extract(html_path)
    cl03_payload = cl03_extract(html_path)
    payloads = {"CL01": cl01_payload, "CL02": cl02_payload, "CL03": cl03_payload}

    # Save payloads for inspection
    for name, payload in payloads.items():
        save_json(payload, output_dir / "payloads" / f"{name.lower()}_payload.json")

    # Step 2 — For each prompt in registry, slice payload and call LLM
    results = []
    for spec in PROMPT_REGISTRY:
        if spec.is_summary and not include_summaries:
            continue  # skip unless --include-summaries

        payload_slice = get_slicer(spec.payload_slicer)(payloads[spec.checklist])

        if spec.skip_if_empty and is_empty_slice(payload_slice):
            continue

        prompt_text = fill_template(spec.name, payload_slice)

        if dry_run:
            save_prompt_only(spec, prompt_text, output_dir)
        else:
            response = call_api(prompt_text, api_key, model)
            save_result(spec, prompt_text, payload_slice, response, output_dir)
            results.append((spec, response))

    # Step 3 — Write manifest
    save_manifest(output_dir, results, model, html_path)
```

---

## 3. Integration Points

### 3.1 Extraction → Prompt Filling

| What changes | File | Specific function/location |
|-------------|------|---------------------------|
| **Import extractors into pipeline** | `entry_points/run_pipeline.py` | Imports `extract()` from each of the three `processing_scripts/llm_preprocessing/*.py` files |
| **No changes to extractors** | `processing_scripts/llm_preprocessing/*.py` | These are **unchanged** — they already return the right data structures |
| **Slicer functions consume extractor output** | `processing_scripts/llm/slicers.py` | Each slicer function takes one extractor's output dict and returns a JSON string for the `{payload}` placeholder |
| **Template loader reads existing .txt files** | `processing_scripts/llm/templates.py` | Parses `processing_scripts/llm/*.txt` — these files are **unchanged** |

### 3.2 API Calling

| What changes | File | Specific function/location |
|-------------|------|---------------------------|
| **New API call function** | `entry_points/run_pipeline.py` | Fresh implementation; streaming is unnecessary for modular prompts (all <20k tokens input) |
| **JSON mode** | `entry_points/run_pipeline.py` | Set `response_format={"type": "json_object"}` in API calls as recommended by pipeline.md |
| **Temperature 0.1** | `entry_points/run_pipeline.py` | Set `temperature=0.1` as recommended by pipeline.md |

### 3.3 Output

| What changes | File | Specific function/location |
|-------------|------|---------------------------|
| **Raw JSON output** | `entry_points/run_pipeline.py` | Saves each prompt's response as a standalone JSON file; report formatting is a separate downstream step |
| **Programmatic findings** | `entry_points/run_pipeline.py` | Calls `analyze_html()` and saves output as `programmatic_findings.json` |
| **No changes to programmatic checker** | `processing_scripts/programmatic/semantic_checklist_01.py` | Already returns the right structure |

### 3.4 Files That Need to Change

| File | Change Type | Description |
|------|-------------|-------------|
| `processing_scripts/llm/__init__.py` | **CREATED** | Package init |
| `processing_scripts/llm/registry.py` | **CREATED** | PromptSpec dataclass + PROMPT_REGISTRY list |
| `processing_scripts/llm/templates.py` | **CREATED** | Template parser + filler |
| `processing_scripts/llm/slicers.py` | **CREATED** | 18 slicer functions (one per prompt) |
| `entry_points/run_pipeline.py` | **CREATED** | Pipeline orchestrator |
| `entry_points/generate_report.py` | **CREATED** | JSON → CSV report generator |
| `processing_scripts/llm_preprocessing/*.py` | **NO CHANGE** | Extractors are complete |
| `processing_scripts/llm/*.txt` | **NO CHANGE** | Prompt templates are complete |
| `processing_scripts/programmatic/*.py` | **NO CHANGE** | Rule-based checker is complete |

---

## 4. Cost / Token Analysis

### 4.1 Monolithic Approach (Current — Broken)

| Metric | Per Request | 18 Combinations |
|--------|------------|-----------------|
| Input tokens | ~487,000 | ~8,766,000 |
| Output tokens (estimated) | ~4,000 | ~72,000 |
| Input cost (Sonnet @ $3/MTok) | $1.46 | $26.30 |
| Input cost (Opus @ $15/MTok) | $7.31 | $131.49 |
| Output cost (Sonnet @ $15/MTok) | $0.06 | $1.08 |
| Output cost (Opus @ $75/MTok) | $0.30 | $5.40 |
| **Total cost (Sonnet)** | **$1.52** | **$27.38** |
| **Total cost (Opus)** | **$7.61** | **$136.89** |
| API calls | 1 | 18 |
| Streaming required | Yes (>400k tokens) | Yes |

### 4.2 Element-Specific Approach (Proposed)

Input tokens per prompt vary by slice size. The following estimates are based on the visionaid.org homepage (walkthrough notebook data).

| Prompt | Input Tokens (est.) |
|--------|-------------------|
| CL01-1: Page Title | ~200 |
| CL01-2: Heading Structure | ~3,500 |
| CL01-3: Link Clarity | ~2,000 |
| CL01-4: Table Semantics | ~800 |
| CL01-5: Iframe Titles | ~300 |
| CL01-6: Landmark Structure | ~2,000 |
| CL01-7: Semantic Summary | ~17,600 |
| CL02-1: Label Quality | ~1,200 |
| CL02-2: Placeholder-as-Label | ~0 (skip — no matches on test page) |
| CL02-3: Group Labels | ~0 (skip — no fieldset groups on test page) |
| CL02-4: Required Field Indicators | ~600 |
| CL02-5: Form Instructions | ~0 (skip — no aria-describedby on test page) |
| CL02-6: Form Summary | ~2,500 |
| CL03-1: Informative Alt Quality | ~3,000 |
| CL03-2: Decorative Verification | ~500 |
| CL03-3: Actionable Image Alt | ~12,000 |
| CL03-4: Complex Descriptions | ~0 (skip — no complex images on test page) |
| CL03-5: SVG Accessibility | ~300 |
| CL03-6: Icon Font Accessibility | ~800 |
| CL03-7: Media Captions | ~0 (skip — no media on test page) |
| CL03-8: Non-text Summary | ~19,200 |

**Prompt template overhead:** Each prompt includes ~200-400 tokens of instruction text on top of the payload.

| Metric | Element-Specific (Total) |
|--------|-------------------------|
| Input tokens (sum of all prompts) | ~67,000 (including prompt instruction overhead) |
| Output tokens (estimated, 21 calls) | ~8,000 |
| Prompts skipped (empty payload) | 4 (of 21) |
| API calls actually made | **17** |
| Input cost (Sonnet @ $3/MTok) | **$0.20** |
| Input cost (Opus @ $15/MTok) | **$1.01** |
| Output cost (Sonnet @ $15/MTok) | **$0.12** |
| Output cost (Opus @ $75/MTok) | **$0.60** |
| **Total cost (Sonnet)** | **$0.32** |
| **Total cost (Opus)** | **$1.61** |
| Streaming required | **No** (all calls <20k tokens) |

### 4.3 Comparison

| | Monolithic (1 call) | Element-Specific (17 calls) | Savings |
|---|---|---|---|
| Input tokens | 487,000 | 67,000 | **86% reduction** |
| API calls | 1 | 17 | More calls, but parallelizable |
| Sonnet cost | $1.52 | $0.32 | **79% cheaper** |
| Opus cost | $7.61 | $1.61 | **79% cheaper** |
| Streaming needed | Yes | No | Simpler code, no timeout risk |
| Per-element diagnostics | Weak (LLM must self-organize) | Strong (each response targets one aspect) | Better report quality |
| Parallelization | Not possible | All 17 calls can run concurrently | Much faster wall-clock time |

**Note:** The monolithic approach sends the entire 487k-token HTML file for each of its 18 factorial combinations. A fairer comparison is one monolithic call vs. 17 element-specific calls. The element-specific approach is ~7x cheaper per evaluation run.

### 4.4 Scaling Considerations

For different websites, token counts will vary proportionally to content volume. The extraction step runs locally (no API cost), so the cost savings scale linearly. Websites with fewer forms or no media will skip more prompts, reducing cost further.

---

## 5. Recommended Implementation Order

### Phase 1: Foundation (Priority: Critical)

**Goal:** Build the orchestration layer so that a single `run_pipeline.py` command runs the full pipeline end-to-end.

| Step | What to Build | Depends On |
|------|--------------|------------|
| 1.1 | `processing_scripts/llm/__init__.py` + `processing_scripts/llm/registry.py` — Define the `PromptSpec` dataclass and `PROMPT_REGISTRY` | Nothing |
| 1.2 | `processing_scripts/llm/templates.py` — Parse `.txt` files, extract individual prompts, fill `{payload}` | registry.py |
| 1.3 | `processing_scripts/llm/slicers.py` — Implement all 18 slicer functions | registry.py |
| 1.4 | `entry_points/run_pipeline.py` — Minimal orchestrator: extract → slice → fill → call API → save raw JSON | registry, templates, slicers |
| 1.5 | Test on `test_files/dat_visionaid_home.html` (smaller file, cheaper) with `--dry-run` first | run_pipeline.py |

**Element types modularized in this phase:** All 21 prompts are wired up, but testing focuses on three priority element types first.

### Phase 2: Priority Element Types (Priority: High)

These three element types should be tested and validated first because they have the clearest WCAG criteria mappings, the highest impact on real-world accessibility, and the most mature extraction code.

**1. Images / Alt Text (CL03 prompts 1-4)**
- **Why first:** WCAG 1.1.1 is the single most commonly failed criterion. The extractor already categorizes images into four distinct groups (informative, decorative, actionable, complex) with pre-detected quality flags. Each category has a different evaluation prompt with clear pass/fail criteria.
- **Prompts involved:** 4 (informative_alt_quality, decorative_verification, actionable_image_alt, complex_descriptions)
- **Expected findings on test page:** ~306 images across categories

**2. Forms / Labels (CL02 prompts 1-5)**
- **Why second:** Form accessibility affects task completion for all users. The forms extractor provides the richest per-element metadata of any extractor (label_source enum with 7 values, effective_label, instructions, required flag, group_label). This enables highly targeted evaluation.
- **Prompts involved:** 5 (label_quality, placeholder_as_label, group_labels, required_field_indicators, form_instructions)
- **Expected findings on test page:** ~16 forms, ~16 fields, ~5 required fields

**3. Links / Navigation (CL01 prompt 3)**
- **Why third:** Link clarity failures are high-volume and easy to validate. The extractor pre-filters to only flagged links (generic terms, short labels, missing text), so the LLM evaluates a focused set rather than all links on the page.
- **Prompts involved:** 1 (link_clarity)
- **Expected findings on test page:** ~76 flagged links

### Phase 3: Remaining Element Types (Priority: Medium)

| Element Type | Prompts | Notes |
|-------------|---------|-------|
| Headings | CL01-2 (heading_structure) | Simple evaluation; heading list is already extracted |
| Page Title | CL01-1 (page_title) | Single-element check; trivial to implement |
| Tables | CL01-4 (table_semantics) | Clear criteria; extractor captures caption + headers |
| Iframes | CL01-5 (iframe_titles) | Small scope; title presence + quality |
| Landmarks | CL01-6 (landmark_structure) | Structural evaluation; requires holistic view |
| SVGs | CL03-5 (svg_accessibility) | Pattern-matching (role="img" + title) |
| Icon Fonts | CL03-6 (icon_font_accessibility) | Critical `sole_content` flag detection |
| Media | CL03-7 (media_captions) | Simple track presence check; low volume on most pages |

### Phase 4: Polish (Priority: Medium)

| Step | What to Build |
|------|--------------|
| 4.1 | Summary prompts are already wired up and gated behind `--include-summaries`; validate output quality |
| 4.2 | Add concurrency (asyncio or thread pool) for parallel API calls |
| 4.3 | Add cost estimation before running (based on payload token counts) |
| 4.4 | Build a separate report-formatting step that consumes the pipeline's JSON output |

---

## 6. Open Questions

### 6.1 Resolved

| # | Question | Decision |
|---|----------|----------|
| 1 | Factorial test runner? | **Remove entirely.** Build prompt generation from scratch. |
| 2 | CSV report format? | **Not finalized.** Pipeline outputs raw JSON. Report formatting is a separate downstream step. |
| 3 | Summary prompts? | **Optional.** Wired up but only run with `--include-summaries` flag. |
| 4 | Large image batching (1,000+)? | **Deferred.** Leave as open issue for now. |

### 6.2 Remaining Open Questions

1. **What model should the pipeline target?** The test runner used `claude-opus-4-20250514` but pipeline.md says "any capable instruction-following model." For cost optimization, `claude-sonnet-4-20250514` at $3/$15 per MTok is recommended for testing. The pipeline supports a `--model` flag.

2. **Prompt file format: keep .txt or move to structured format?** The current `.txt` files contain multiple prompts separated by dashed headers. Parsing them requires regex-based splitting. An alternative is to store each prompt as a separate file (e.g., `semantic_01_page_title.txt`) or use a structured format like YAML. The current format works and is human-readable — recommend keeping it unless prompt count grows significantly.

3. **Should the programmatic checker expand to cover CL02 and CL03?** Currently it only covers CL01-equivalent checks (missing H1, alt text, links, duplicate IDs, iframe titles). Programmatic checks for forms (e.g., `label_source == "none"`) and non-text content (e.g., `sole_content && aria_hidden`) could eliminate LLM calls for clear-cut failures. This would further reduce API cost.

4. **Async vs. sync API calls?** 17 independent API calls can be parallelized. Using `asyncio` with the Anthropic async client or a `ThreadPoolExecutor` could reduce wall-clock time from ~17 × T to ~T (where T is the slowest single call). Recommend adding in Phase 4.

5. **How to version prompt templates?** As prompts are refined, the team may want to track which prompt version produced which result. Options: (a) git history, (b) a version string in each `.txt` file, (c) hash the prompt text and store it with each result. Recommend starting with git history.

6. **Large image batching.** The visionaid.org homepage has 236 actionable images (~12k tokens). If another site has 1,000+ images, the payload may exceed reasonable prompt sizes. Deferred for now — will revisit when testing on more sites.
