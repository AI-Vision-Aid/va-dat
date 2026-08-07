# Accessibility Audit Pipeline

## Overview

A raw HTML file is too large and too noisy to pass directly to an LLM for
accessibility evaluation. The audit runs in **two sequential passes**:

1. **Pass 1 — Programmatic** — binary rule checks run directly against the HTML.
   Objectively verifiable failures (missing alt attributes, duplicate IDs, broken
   ARIA references) are reported immediately. These findings also act as a **filter**:
   items that already failed a binary check are excluded from LLM evaluation — there
   is no point asking the LLM to judge the quality of something that doesn't exist.

2. **Pass 2 — LLM** — the HTML is pre-processed into compact JSON payloads. Items
   excluded by Pass 1 are removed before sending. The LLM evaluates only what passed
   the binary checks: is the alt text (which is present) meaningful? is the heading
   (which exists) logically placed? does the label (which is associated) describe the
   field clearly?

The result is **43 programmatic rules** + **up to 21 LLM calls** (some skipped if
all items in a slice were filtered out) covering the full WCAG evaluation defined by
the three Deque checklist documents in `semantic_checklist/`.

---

## Full Flow

```
home.html (1.9 MB, ~487k tokens)
    │
    │  PASS 1 — Programmatic Binary Checks
    │
    ├── programmatic/semantic_checklist_01.py  ──▶  29 rules  ──▶  pass/fail findings
    ├── programmatic/forms_checklist_02.py     ──▶   7 rules  ──▶  pass/fail findings
    └── programmatic/nontext_checklist_03.py   ──▶   7 rules  ──▶  pass/fail findings
                                                     43 rules total
                                                           │
                                                           │  Filter: items that failed a binary check
                                                           │  are excluded from LLM quality evaluation.
                                                           │  (No point judging the quality of something
                                                           │  that doesn't exist.)
                                                           ▼
    │  PASS 2 — LLM Semantic Quality Checks (on filtered payloads)
    │
    │  Step 1 — Extract + Filter
    │
    ├── llm_preprocessing/semantic_checklist_01.py ──▶ cl01_payload.json  (filtered)
    ├── llm_preprocessing/forms_checklist_02.py    ──▶ cl02_payload.json  (filtered)
    └── llm_preprocessing/nontext_checklist_03.py  ──▶ cl03_payload.json  (filtered)
    │
    │  Step 2 — Slice & Call
    │  (prompts with empty slices after filtering are skipped)
    │
    ├── CL01 payload  ──▶  up to 7 prompt slices  ──▶  up to 7 LLM calls
    ├── CL02 payload  ──▶  up to 6 prompt slices  ──▶  up to 6 LLM calls
    └── CL03 payload  ──▶  up to 8 prompt slices  ──▶  up to 8 LLM calls
    │
    │  Step 3 — Merge
    │
    └── programmatic findings + LLM responses ──▶ aggregate ──▶ accessibility report
```

---

## Filtering Logic — What Pass 1 Excludes from Pass 2

When a programmatic rule fires, it indicates a *structural* failure (something
missing or broken). There is no value in asking the LLM to judge the quality of
something that doesn't exist. The table below defines what gets filtered.

Items **not** listed here should still go to the LLM — the programmatic finding
and the LLM quality judgment are complementary (e.g. `FORM_LABEL_003` fires to
flag placeholder-as-label, but the LLM still evaluates severity and remediation).

| Programmatic rule that fires | Item excluded from LLM | Why |
|---|---|---|
| `PAGE_TITLE_001` (missing title) | CL01 Prompt 1 skipped entirely | No title text to evaluate quality of |
| `PAGE_TITLE_003` (empty title) | CL01 Prompt 1 skipped entirely | Empty string has no quality to judge |
| `IFRAME_001` / `IFRAME_002` (missing/empty title) | That iframe filtered from CL01 Prompt 5 | No title text to evaluate quality of |
| `HEAD_004` (empty heading) | That heading filtered from CL01 Prompt 2 | Empty heading has no content to assess |
| `FORM_LABEL_001` (no label) | That field filtered from CL02 Prompt 1 | No label exists — quality is moot |
| `FORM_INSTR_001` (broken `aria-describedby`) | That field filtered from CL02 Prompt 5 | Reference is broken; no instructions are delivered |
| `NON_TEXT_001` (missing alt on `<img>`) | That image filtered from CL03 Prompts 1 & 2 | No alt attribute — nothing to evaluate |
| `NON_TEXT_002` (actionable image, no/empty alt) | That image filtered from CL03 Prompt 3 | No alt text to evaluate quality of |

### What the LLM still gets even when programmatic fires

| Programmatic rule | Why LLM still runs |
|---|---|
| `FORM_LABEL_003` (placeholder-only) | LLM evaluates severity and suggests a proper visible label |
| `FORM_GROUP_001` (fieldset without legend) | LLM can note severity and recommended fix |
| `HEAD_001` (skipped heading) | LLM evaluates quality of headings that *are* present |
| `LINK_001` (no accessible name on link) | These links are already absent from `flagged_links` (no text to flag) — no LLM overlap |
| `NAV_001`–`NAV_003` (skip link issues) | Skip links not covered by any LLM prompt — programmatic only |
| `PARSE_001` (duplicate IDs) | Not covered by any LLM prompt — programmatic only |

---

## Pipeline A — Programmatic Binary Checks

Each programmatic checker parses the raw HTML with BeautifulSoup and applies
rule-based tests that yield definitive pass/fail results. No LLM interpretation
is required — these findings are reported directly.

### `programmatic/semantic_checklist_01.py` — 29 rules

| Rule ID | Rule | Trigger |
|---|---|---|
| `PAGE_TITLE_001` | Missing `<title>` | No title element found |
| `PAGE_TITLE_002` | Multiple `<title>` elements | More than one title |
| `PAGE_TITLE_003` | Empty `<title>` | Title present but contains no text |
| `LANG_001` | Missing primary language | No `lang` attribute on `<html>` |
| `LANG_002` | Invalid primary language code | `lang` value fails BCP 47 pattern |
| `LANG_003` | Invalid inline language code | `lang` on inner element fails BCP 47 |
| `LAND_001` | Missing main landmark | No `<main>` or `role="main"` found |
| `LAND_002` | Multiple main landmarks | More than one main landmark |
| `LAND_003` | Multiple banner landmarks | More than one header/banner |
| `LAND_004` | Multiple contentinfo landmarks | More than one footer/contentinfo |
| `LAND_005` | Duplicate landmark without label | Multiple same-type landmarks, none aria-labelled |
| `LAND_006` | Content outside landmark regions | Text-bearing direct child of `<body>` outside all landmarks |
| `HEAD_001` | Skipped heading level | e.g., `<h2>` immediately followed by `<h4>` |
| `HEAD_002` | Multiple `<h1>` elements | More than one h1 on the page |
| `HEAD_003` | Missing `<h1>` | No h1 element found |
| `HEAD_004` | Empty heading | Heading element contains no text |
| `LINK_001` | Link without accessible name | `<a>` with no text content or `aria-label` |
| `LINK_002` | Anchor without href | `<a>` missing `href` attribute |
| `NAV_001` | Skip link not present | No skip navigation link detected |
| `NAV_002` | Skip link target missing | Skip link `#id` points to non-existent element |
| `NAV_003` | Skip link not first focusable | First focusable element on page is not the skip link |
| `FOCUS_001` | Positive tabindex | `tabindex` value greater than 0 |
| `TABLE_001` | Missing table caption | `<table>` without `<caption>` element |
| `TABLE_002` | Missing table headers | `<table>` without `<th>` elements |
| `IFRAME_001` | Missing iframe title | `<iframe>` without `title` attribute |
| `IFRAME_002` | Empty iframe title | `<iframe>` with blank `title` attribute |
| `PARSE_001` | Duplicate ID | Same `id` value appears more than once in the document |

### `programmatic/forms_checklist_02.py` — 7 rules

| Rule ID | Rule | Trigger |
|---|---|---|
| `FORM_LABEL_001` | Form control missing programmatic label | No `<label for>`, wrapping label, `aria-label`, or `aria-labelledby` |
| `FORM_LABEL_003` | Placeholder used as only label | `placeholder` present but no programmatic label found |
| `FORM_GROUP_001` | Fieldset missing legend | `<fieldset>` without a `<legend>` child element |
| `FORM_REQUIRED_001` | Required field not programmatically designated | Label contains `*` but control lacks `required` attribute |
| `FORM_INSTR_001` | `aria-describedby` reference not found | Referenced ID does not exist in the document |
| `FORM_ERROR_001` | Error message not programmatically associated | `aria-invalid="true"` without `aria-describedby` linking to error |
| `FORM_CUSTOM_001` | Custom interactive element missing role | Element with `onclick` but no semantic `role` |

### `programmatic/nontext_checklist_03.py` — 7 rules

| Rule ID | Rule | Trigger |
|---|---|---|
| `NON_TEXT_001` | Image missing alt attribute | `<img>` without any `alt` attribute |
| `NON_TEXT_002` | Actionable image missing alt text | `<img>` inside `<a>` with missing or empty `alt` |
| `NON_TEXT_003` | Image input missing alt text | `<input type="image">` with missing or empty `alt` |
| `NON_TEXT_004` | Image map area missing alt text | `<area>` with missing or empty `alt` |
| `NON_TEXT_005` | SVG embedded via object or iframe | `<object>` or `<iframe>` with `src` ending in `.svg` |
| `NON_TEXT_006` | Canvas missing fallback text | `<canvas>` element with no inner text content |
| `NON_TEXT_007` | Object missing alternative text | `<object>` element with no inner text content |

---

## Pipeline B, Step 1 — Extractors (`llm_preprocessing/`)

Each extractor parses the raw HTML with BeautifulSoup and returns a structured
JSON payload. All CSS, JavaScript, layout divs, inline styles, analytics, and
image URLs are discarded.

### `semantic_checklist_01.py`

| Key | What it contains |
|---|---|
| `language` | `lang` attribute from `<html>` tag |
| `page_title` | `<title>` text + `<h1>` text |
| `headings` | All `<h1>`-`<h6>` elements with level and text |
| `flagged_links` | Links with no text, generic terms, or 2-word-or-fewer labels |
| `images` | Alt text presence categorised: missing / empty / has_alt |
| `forms` | Basic label presence per field |
| `buttons` | Unique button text / aria-label |
| `landmarks` | Semantic landmark elements + role= attributes |
| `tables` | Caption and header text (sampled) |
| `iframes` | Title attribute per iframe |

### `forms_checklist_02.py`

| Key | What it contains |
|---|---|
| `forms[].fields` | Per field: type, label text, label source, aria-label, aria-labelledby (resolved to text), title, placeholder, instructions (aria-describedby resolved to text), required flag, group label |
| `forms[].groups` | Fieldset/legend groups with legend text and input types inside |
| `orphan_labels` | Labels whose `for` attribute points to an element outside a form |

`label_source` is an enum that tells the LLM exactly how the field gets its accessible name:

| Value | Meaning |
|---|---|
| `label_for` | `<label for="id">` correctly linked |
| `wrapping_label` | `<label>` wraps the input directly |
| `aria_labelledby` | `aria-labelledby` points to another element |
| `aria_label` | `aria-label` attribute on the input |
| `title` | `title` attribute (last resort, not recommended) |
| `placeholder_only` | Only accessible name is placeholder — disappears on input |
| `none` | No accessible name found |

### `nontext_checklist_03.py`

| Key | What it contains |
|---|---|
| `images.informative` | Images with non-empty alt text + pre-detected alt_flags |
| `images.decorative` | Images with `alt=""` + surrounding text context |
| `images.actionable` | Images inside `<a>` or `<button>` + parent link/button context |
| `images.complex` | Images with chart/diagram/graph hints in filename or class |
| `svgs` | Non-hidden SVGs with role, title, desc, aria-label, aria-labelledby |
| `icon_fonts` | Font Awesome / Elementor / Dashicons icons with aria-hidden status, sibling text, and `sole_content` flag |
| `media` | Video and audio elements with track info |

`alt_flags` are pre-detected quality signals on informative images:

| Flag | Meaning |
|---|---|
| `looks_like_filename` | alt text ends in `.jpg`, `.png`, etc. |
| `redundant_phrase` | alt starts with "image of", "photo of", etc. |
| `too_long` | alt text exceeds 150 characters |

`sole_content` on icon fonts is `true` when the icon is the only content inside
a link or button — making `aria-hidden="true"` on it a critical failure
(the control has no accessible name).

---

## Pipeline B, Step 2 — Prompt Slices (`llm/`)

Each prompt lives in `processing_scripts/llm/` and contains a `{payload}`
placeholder that is filled with the relevant JSON slice before sending to the LLM.

### Checklist 01 — Semantic Structure (`semantic_checklist_01.txt`)

| # | Prompt | Payload slice | What the LLM judges |
|---|---|---|---|
| 1 | Page Title | `page_title` | Is the title descriptive and distinct? Does it match the H1? |
| 2 | Heading Structure | `page_title` + `headings` | Does the heading hierarchy form a logical content outline? Are headings meaningful? |
| 3 | Link Clarity | `flagged_links` | Is each short/generic link text clear when read out of context? |
| 4 | Table Semantics | `tables` | Is the caption meaningful? Do headers describe the data? Is this a data table or a layout table? |
| 5 | Iframe Titles | `iframes` | Does each iframe title clearly describe the iframe's purpose? |
| 6 | Landmark Structure | `landmarks` | Are multiple navs differentiated with aria-label? Is there a main landmark? Is the structure appropriate? |
| 7 | Combined Summary | full `cl01_payload` | High/medium/low priority issues, overall semantic quality score 0-100 |

### Checklist 02 — Forms (`forms_checklist_02.txt`)

| # | Prompt | Payload slice | What the LLM judges |
|---|---|---|---|
| 1 | Label Quality | All fields with an `effective_label` | Are labels descriptive, plain-language, and self-sufficient? |
| 2 | Placeholder-as-Label | Fields where `label_source = "placeholder_only"` | How severe is the impact? What should the visible label be? |
| 3 | Group Label Quality | All `forms[].groups` (fieldset/legend) | Does the legend provide enough context for radio/checkbox groups? |
| 4 | Required Field Indicators | Fields where `required: true` | Is required status communicated in the label or instructions, not just programmatically? |
| 5 | Form Instructions | Fields where `instructions` is non-null | Are aria-describedby instructions helpful, clear, and non-redundant? |
| 6 | Overall Form Summary | full `cl02_payload` | Patterns across all forms, high/medium/low issues, overall score 0-100 |

### Checklist 03 — Non-text Content (`nontext_checklist_03.txt`)

| # | Prompt | Payload slice | What the LLM judges |
|---|---|---|---|
| 1 | Informative Alt Quality | `images.informative` | Is alt text accurate, concise, and meaningful? Addresses any `alt_flags`. |
| 2 | Decorative Verification | `images.decorative` | Are empty-alt images genuinely decorative, or do they carry informational content? |
| 3 | Actionable Image Alt | `images.actionable` | Does the alt text describe the link destination or button action — not the image appearance? |
| 4 | Complex Descriptions | `images.complex` | Does the alt text or linked long description adequately convey the data in charts/diagrams? |
| 5 | SVG Accessibility | `svgs` | Is `role="img"` set? Is there a `<title>`? Is `aria-labelledby` linking to it? Is the title meaningful? |
| 6 | Icon Font Accessibility | `icon_fonts` | Is each icon correctly decorative (hidden) or informative (labelled)? Flags `sole_content` + `aria-hidden` as unlabeled controls. |
| 7 | Media Captions | `media` | Do videos have caption tracks? Does audio have controls? Are tracks labelled with srclang? |
| 8 | Overall Summary | full `cl03_payload` | Patterns across all non-text content, high/medium/low issues, overall score 0-100 |

---

## Pipeline B, Step 3 — Output Format

Every prompt returns structured JSON. Example responses:

**Prompt 3 — Link Clarity:**
```json
[
  {
    "text": "here",
    "is_clear": false,
    "reason": "Generic term provides no destination context for screen reader users",
    "suggested_improvement": "Replace with destination page name, e.g. 'Learn about our vision programs'"
  }
]
```

**Prompt 6 — Icon Font Accessibility:**
```json
[
  {
    "classes": "fas fa-search",
    "aria_hidden": true,
    "sole_content": true,
    "pattern": "unlabeled_control",
    "issues": ["Search button has no accessible name — aria-hidden hides the only content"],
    "recommendation": "Add aria-label='Search' to the parent button element"
  }
]
```

**Summary prompts (CL01-7, CL02-6, CL03-8):**
```json
{
  "high_priority_issues": ["..."],
  "moderate_issues": ["..."],
  "low_priority_issues": ["..."],
  "patterns_observed": ["..."],
  "overall_score": 62
}
```

---

## File Map

```
va-dat/
│
├── semantic_checklist/               <- Deque WCAG reference documents (source of truth)
│   ├── 01-semantic-checklist.pdf
│   ├── 02-forms-checklist.pdf
│   └── 03-nontext-checklist.pdf
│
├── test_files/
│   └── home.html                     <- Raw HTML input (1.9 MB)
│
├── processing_scripts/
│   │
│   ├── programmatic/                 <- Pipeline A: binary rule checks
│   │   ├── semantic_checklist_01.py  <- 29 rules (title, lang, landmarks, headings, links...)
│   │   ├── forms_checklist_02.py     <- 7 rules (labels, fieldsets, aria-describedby...)
│   │   └── nontext_checklist_03.py   <- 7 rules (alt text, canvas, SVG embedding...)
│   │
│   ├── llm_preprocessing/            <- Pipeline B, Step 1: Extract structured payloads
│   │   ├── semantic_checklist_01.py  <- CL01 extractor (headings, links, landmarks...)
│   │   ├── forms_checklist_02.py     <- CL02 extractor (labels, groups, instructions...)
│   │   └── nontext_checklist_03.py   <- CL03 extractor (images, SVG, icons, media...)
│   │
│   └── llm/                          <- Pipeline B, Step 2: Prompt library
│       ├── semantic_checklist_01.txt <- 7 prompts for CL01
│       ├── forms_checklist_02.txt    <- 6 prompts for CL02
│       └── nontext_checklist_03.txt  <- 8 prompts for CL03
│
├── accessibility_audit_walkthrough.ipynb  <- end-to-end notebook (both passes)
│
└── docs/
    └── pipeline.md                        <- this file
```

---

## Key Numbers (visionaid.org homepage)

### Pipeline A — Programmatic

| Checker | Rules | Findings on home.html |
|---|---|---|
| `programmatic/semantic_checklist_01.py` | 29 | Run checker to see |
| `programmatic/forms_checklist_02.py` | 7 | Run checker to see |
| `programmatic/nontext_checklist_03.py` | 7 | Run checker to see |
| **Total** | **43** | — |

### Pipeline B — LLM

| | Raw HTML | CL01 | CL02 | CL03 | All 3 |
|---|---|---|---|---|---|
| Tokens | ~487,000 | ~17,600 | ~2,500 | ~19,200 | ~39,300 |
| Reduction | baseline | 96% | 99% | 96% | 92% |
| LLM calls | — | 7 | 6 | 8 | **21** |

---

## LLM Settings

All prompts are designed to be run with:

- **Temperature**: 0.1 — low randomness for consistent, deterministic output
- **JSON mode**: on — enforce structured responses
- **Model**: Any capable instruction-following model (Claude Sonnet 4.6, GPT-4o, etc.)
