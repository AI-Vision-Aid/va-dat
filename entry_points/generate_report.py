"""Combine programmatic + LLM findings into a single flat CSV report.

Reads output/manifest.json, output/programmatic_findings.json, and
output/prompts/*.json, normalizes all findings into flat CSV rows, and
writes to test_results/claude/report_YYYY-MM-DD.csv.

Includes built-in false positive filtering that automatically suppresses
common false positives based on HTML context analysis.

Usage:
    python entry_points/generate_report.py
    python entry_points/generate_report.py --output-dir ./output --report-dir ./test_results/claude/
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from bs4 import BeautifulSoup

# ── Deduplication prompt ─────────────────────────────────────────────────────

_DEDUP_PROMPT = """\
You are reviewing an accessibility audit report for duplicate findings.

Below is a numbered list of findings. Each entry includes an index (IDX), \
category, element, issue title, and a short description. Identify groups of \
findings that report the SAME issue on the SAME element or page area. Within \
each duplicate group, keep the FIRST occurrence and mark the rest for removal.

Only flag true duplicates — findings that are substantively identical in both \
the problem they describe and the element they affect. Do NOT flag findings \
that are merely similar or cover related WCAG criteria on different elements.

Return a JSON array of IDX values to REMOVE. If there are no duplicates, \
return an empty array.

Findings:
{findings}

Respond with ONLY a JSON array, e.g.: [3, 7, 12]
"""


# ── CSV schema ──────────────────────────────────────────────────────────────

@dataclass
class ReportRow:
    """One row in the final CSV report (14 columns)."""

    ID: int = 0
    element_name: str = ""
    browser_combination: str = "N/A"
    page_title: str = ""
    issue_title: str = ""
    steps_to_reproduce: str = ""
    actual_result: str = ""
    expected_result: str = ""
    recommendation: str = ""
    wcag_sc: str = ""
    category: str = ""
    impact: str = ""
    log_date: str = ""
    reported_by: str = ""


CSV_COLUMNS = [f.name for f in fields(ReportRow)]


# ── Helpers ─────────────────────────────────────────────────────────────────

def strip_code_fence(text: str) -> str:
    """Remove ```json ... ``` wrappers from an LLM response string."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _repair_json(text: str) -> str:
    """Attempt to repair common LLM JSON malformations.

    Handles the pattern where the model writes alternatives inline:
      "value": "some text" or similar alternative text
    by truncating at the first unquoted `or` after a closing quote.
    """
    # Fix: "string value" or alternative text  →  "string value"
    # This matches a quoted string followed by unquoted text before a comma/bracket.
    text = re.sub(
        r'("(?:[^"\\]|\\.)*")\s+or\s+[^,\]\}]+',
        r'\1',
        text,
    )
    return text


def safe_parse_json(text: str):
    """Parse JSON from an LLM response, stripping code fences first.

    Falls back to a repair pass if initial parsing fails due to common
    LLM malformations like inline alternative text.
    """
    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        repaired = _repair_json(cleaned)
        return json.loads(repaired)


def load_prompt_file(path: Path) -> dict | None:
    """Load a prompt output JSON file and return the parsed structure."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.load(f)


def extract_page_title_from_payload(prompt_data: dict) -> str:
    """Pull the page title string from the page_title prompt's payload_slice."""
    try:
        payload = json.loads(prompt_data["payload_slice"])
        return payload.get("title", "")
    except (json.JSONDecodeError, KeyError):
        return ""


def _get_recommendation(item: dict) -> str:
    """Extract the best fix recommendation from an LLM finding.

    Checks recommended_fix first (the new field from updated prompts),
    then falls back to existing field names for backwards compatibility.
    """
    return (
        item.get("recommended_fix")
        or item.get("suggested_improvement")
        or item.get("recommendation")
        or item.get("improved_example")
        or item.get("fix")
        or ""
    )

_GENERIC_FIXES: dict[str, str] = {
    # Page Title
    "PAGE_TITLE_001": "Add a <title> element inside <head> that describes the page content, e.g. <title>About Us - Company Name</title>.",
    "PAGE_TITLE_002": "Remove duplicate <title> elements so only one remains inside <head>.",
    "PAGE_TITLE_003": "Add descriptive text inside the existing <title> element, e.g. <title>Home - Company Name</title>.",

    # Language
    "LANG_001": "Add a lang attribute to the <html> element, e.g. <html lang=\"en\">.",
    "LANG_002": "Replace the invalid lang value with a valid BCP 47 language tag, e.g. lang=\"en\" or lang=\"es\".",
    "LANG_003": "Replace the invalid lang attribute with a valid BCP 47 language tag, e.g. lang=\"fr\".",

    # Landmarks
    "LAND_001": "Wrap the main page content in a <main> element.",
    "LAND_002": "Keep only one <main> landmark per page. Use <section> or <div> for other content areas.",
    "LAND_003": "Keep only one <header> (banner) landmark at the top level. Nest others inside <section> or <article>.",
    "LAND_004": "Keep only one <footer> (contentinfo) landmark at the top level. Nest others inside <section> or <article>.",
    "LAND_005": "Add a unique aria-label to each landmark of the same type, e.g. <nav aria-label=\"Primary navigation\"> and <nav aria-label=\"Footer navigation\">.",
    "LAND_006": "Move this content inside an appropriate landmark element (<main>, <header>, <nav>, or <footer>).",

    # Headings
    "HEAD_001": "Fix the heading hierarchy so levels are not skipped. For example, an <h2> should follow an <h1>, not an <h4>.",
    "HEAD_002": "Use only one <h1> per page. Change extra <h1> elements to <h2> or another appropriate level.",
    "HEAD_003": "Add an <h1> element that describes the main topic of the page.",
    "HEAD_004": "Add descriptive text inside the empty heading element, or remove it if it serves no purpose.",

    # Links
    "LINK_001": "Add visible text, an aria-label, or an alt attribute on a child image so the link has an accessible name. Example: <a href=\"/about\" aria-label=\"About us\">.",
    "LINK_002": "Add an href attribute to the <a> element, or change it to a <button> if it triggers an action.",

    # Navigation / Skip Links
    "NAV_001": "Add a skip navigation link as the first element in <body>, e.g. <a href=\"#main\" class=\"skip-link\">Skip to main content</a>.",
    "NAV_002": "Ensure the skip link's target ID exists on the page. Add id to the target element, e.g. <main id=\"main\">.",
    "NAV_003": "Move the skip link so it is the first focusable element on the page.",

    # Focus / Tabindex
    "FOCUS_001": "Remove the positive tabindex value or set it to 0. Use DOM order to control focus sequence instead.",

    # Tables
    "TABLE_001": "Add a <caption> element inside the <table> to describe its purpose, e.g. <caption>Quarterly sales data</caption>.",
    "TABLE_002": "Add <th> elements to identify column and/or row headers in the table.",

    # Iframes
    "IFRAME_001": "Add a title attribute to the <iframe> that describes its content, e.g. <iframe title=\"Company location map\">.",
    "IFRAME_002": "Add descriptive text to the empty title attribute on the <iframe>.",

    # Parsing
    "PARSE_001": "Change one of the duplicate id values so every id on the page is unique.",

    # Non-text Content (from other programmatic checkers)
    "NON_TEXT_001": "Add a descriptive alt attribute to the <img> element, e.g. <img alt=\"Photo of company headquarters\">.",
    "NON_TEXT_002": "Add descriptive alt text to the image inside the link or button so it describes the action or destination.",

    # Forms (from forms_checklist_02.py)
    "FORM_LABEL_001": "Add a <label> element with a matching 'for' attribute, wrap the control in a <label>, or add an aria-label attribute. Example: <label for=\"email\">Email address</label> <input id=\"email\">.",
    "FORM_LABEL_003": "Add a proper <label> element instead of relying on placeholder text. Placeholder text disappears when the user starts typing and is not a reliable label.",
    "FORM_GROUP_001": "Add a <legend> element as the first child of the <fieldset> to describe the group of controls. Example: <fieldset><legend>Shipping address</legend>...</fieldset>.",
    "FORM_REQUIRED_001": "Add the 'required' attribute to this form control so assistive technology can announce it as required. Example: <input id=\"name\" required>.",
    "FORM_INSTR_001": "Ensure the ID referenced by aria-describedby exists on the page. Add the missing element or fix the ID to match.",
    "FORM_ERROR_001": "Add aria-describedby to the invalid control pointing to the error message element. Example: <input aria-invalid=\"true\" aria-describedby=\"error-msg\"> <p id=\"error-msg\">Please enter a valid email.</p>.",
    "FORM_CUSTOM_001": "Add an appropriate role attribute to this interactive element. Example: <div onclick=\"...\" role=\"button\" tabindex=\"0\">.",
}


# ══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVE FILTERING
# Runs automatically on every audit to suppress common false positives.
# ══════════════════════════════════════════════════════════════════════════════

# Standard navigation terms that are clear in context — flagging these as
# "unclear links" is a false positive when they appear in nav/header/footer.
_NAV_TERMS = {
    "about", "about us", "home", "contact", "contact us", "products",
    "services", "careers", "media", "blog", "news", "faq", "help",
    "support", "login", "log in", "sign in", "sign up", "register",
    "search", "menu", "close", "back", "next", "previous", "submit",
    "apply", "download", "upload", "share", "print", "save",
    "investors", "investor", "customers", "reports", "reach us",
    "privacy", "terms", "legal", "partners", "team", "our team",
    "privacy policy", "terms of use", "cookie policy",
}


def _load_html_for_filtering(output_dir: Path) -> BeautifulSoup | None:
    """Try to load the audited HTML for false-positive checking.

    Looks for the HTML path in the manifest, then tries to read it.
    Returns None if the HTML is not available (filtering will be skipped).
    """
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        html_path = manifest.get("html_file")
        if not html_path:
            return None
        html_path = Path(html_path)
        if not html_path.exists():
            return None
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            return BeautifulSoup(f.read(), "lxml")
    except Exception:
        return None


def _build_link_nav_index(soup: BeautifulSoup) -> set[str]:
    """Build a set of lowercased link texts that are inside <nav> or <footer>.

    Called once and reused across all link findings to avoid repeated DOM walks.
    """
    nav_link_texts: set[str] = set()
    for a in soup.find_all("a"):
        if a.find_parent("nav") or a.find_parent("footer"):
            text = a.get_text(strip=True).lower()
            if text:
                nav_link_texts.add(text)
    return nav_link_texts


def _is_link_fp(row: ReportRow, nav_link_texts: set[str]) -> bool:
    """Return True if a link clarity finding is a likely false positive."""
    issue = row.issue_title
    match = re.search(r'Unclear link: "(.+?)"', issue)
    if not match:
        return False

    link_text = match.group(1).strip()

    # Standard nav terms are clear in context
    if link_text.lower() in _NAV_TERMS:
        return True

    # Phone numbers with digits are clear (especially with tel: href)
    if re.match(r"^[\d\-\+\(\)\s]+$", link_text):
        return True

    # Links inside <nav> or <footer> are clear in context
    if link_text.lower() in nav_link_texts:
        return True

    return False


def _is_decorative_img_fp(row: ReportRow, soup: BeautifulSoup | None) -> bool:
    """Return True if a 'mis-marked as decorative' finding is a likely false positive."""
    issue = row.issue_title
    match = re.search(r"mis-marked as decorative: (.+)", issue)
    if not match:
        return False

    src = match.group(1).strip()

    # the heading text already conveys the info
    is_icon = src.endswith(".svg") or "icon" in src.lower()

    if is_icon and soup:
        for img in soup.find_all("img"):
            img_src = img.get("src", "")
            # endswith for full-path comparison to avoid partial matches
            # ex: "old-logo.svg" won't match when looking for "logo.svg"
            if img_src.endswith(src) or img_src == src:
                parent = img.parent
                if parent:
                    # Check if there's a heading sibling
                    heading = parent.find(re.compile(r"^h[1-6]$"))
                    if heading and heading.get_text(strip=True):
                        return True
                    # Also check parent's parent
                    if parent.parent:
                        heading = parent.parent.find(re.compile(r"^h[1-6]$"))
                        if heading and heading.get_text(strip=True):
                            return True
                break

    return False


def _is_svg_fp(row: ReportRow, soup: BeautifulSoup | None) -> bool:
    """Return True if an SVG accessibility finding is a likely false positive.

    SVGs inside links or buttons that already have visible text are decorative
    and don't need an accessible name — they just need aria-hidden='true'.
    This is a different fix than what the report suggests.
    """
    if not soup:
        return False

    # Find a specific unlabeled SVG that matches this finding, then check
    # if it's inside a parent element with text. Uses decompose() to consume
    # matched SVGs so each finding maps to one SVG, not all of them.
    for svg in soup.find_all("svg"):
        if svg.get("aria-hidden") == "true":
            continue

        # Skip SVGs that already have an accessible name — they aren't
        # the unlabeled one this finding is about
        has_name = (
            svg.get("aria-label")
            or svg.get("aria-labelledby")
            or svg.find("title")
        )
        if has_name:
            continue

        parent_link = svg.find_parent("a")
        parent_button = svg.find_parent("button")

        if parent_link and parent_link.get_text(strip=True):
            # This SVG is decorative — remove from soup so the next
            # finding doesn't match it again
            svg.decompose()
            return True
        if parent_button and parent_button.get_text(strip=True):
            svg.decompose()
            return True

    return False


def filter_false_positives(
    rows: list[ReportRow],
    soup: BeautifulSoup | None,
) -> tuple[list[ReportRow], list[ReportRow]]:
    """Split rows into kept findings and suppressed false positives.

    Args:
        rows: All report rows (programmatic + LLM).
        soup: Parsed HTML of the audited page, or None if unavailable.

    Returns:
        (kept, suppressed) — two lists of ReportRow.
    """
    kept = []
    suppressed = []

    # Pre-build link index once instead of per-finding
    nav_link_texts: set[str] = set()
    if soup:
        nav_link_texts = _build_link_nav_index(soup)

    for row in rows:
        category = row.category
        is_fp = False

        # Check link clarity false positives
        if "Links" in category and "Unclear link" in row.issue_title:
            is_fp = _is_link_fp(row, nav_link_texts)

        # Check decorative image false positives
        elif "Decorative" in category and "mis-marked" in row.issue_title:
            is_fp = _is_decorative_img_fp(row, soup)

        # Check SVG false positives
        elif "SVG" in category:
            is_fp = _is_svg_fp(row, soup)

        if is_fp:
            suppressed.append(row)
        else:
            kept.append(row)

    return kept, suppressed


# ── Impact helpers ──────────────────────────────────────────────────────────

# Axe-core style impact levels: Critical > Serious > Moderate > Minor
_RULE_IMPACT: dict[str, str] = {
    # Non-text content — blocking for screen reader users
    "missing_alt": "Critical",
    "image_alt": "Critical",
    # Duplicate IDs break AT navigation
    "duplicate_id": "Serious",
    "duplicate_id_aria": "Serious",
    # Form accessibility
    "label": "Critical",
    "label_content_name_mismatch": "Serious",
    # Page/document
    "html_has_lang": "Serious",
    "html_lang_valid": "Serious",
    "document_title": "Serious",
}

_WCAG_CRITERION_IMPACT: dict[str, str] = {
    # Level A — non-text content / alternatives
    "1.1.1": "Critical",
    # Level A — keyboard / focus
    "2.1.1": "Critical",
    "2.1.2": "Critical",
    # Level A — language
    "3.1.1": "Serious",
    # Level A — parsing
    "4.1.1": "Serious",
    "4.1.2": "Serious",
    # Level AA
    "1.4.3": "Serious",
    "1.4.4": "Moderate",
    "2.4.3": "Moderate",
    "2.4.6": "Moderate",
    "3.1.2": "Minor",
}


def _derive_impact(rule_id: str = "", wcag_criterion: str = "") -> str:
    """Return an impact level (Critical/Serious/Moderate/Minor) for a finding.

    Checks rule-specific overrides first, then WCAG criterion level, then
    falls back to 'Serious' for unknown Level A issues.
    """
    if rule_id and rule_id.lower() in _RULE_IMPACT:
        return _RULE_IMPACT[rule_id.lower()]
    # wcag_criterion may be "1.1.1" or "WCAG 1.1.1" — normalise
    criterion = re.sub(r"[^0-9.]", "", wcag_criterion or "").strip(".")
    if criterion in _WCAG_CRITERION_IMPACT:
        return _WCAG_CRITERION_IMPACT[criterion]
    # WCAG A/AA heuristic based on first digit
    if criterion.startswith(("1.", "2.", "3.", "4.")):
        parts = criterion.split(".")
        if len(parts) == 3:
            # Most WCAG A criteria default to Serious; AA to Moderate
            return "Serious"
    return "Moderate"


# ── Programmatic findings normalizer ───────────────────────────────────────

def normalize_programmatic(findings: list[dict], page_title: str,
                           log_date: str) -> list[ReportRow]:
    """Convert programmatic_findings.json entries to ReportRow objects.

    Supports both the new schema (rule_id/rule_name/location/description)
    and the legacy schema (issue_code/checklist_item/element/wcag) for
    backwards compatibility.
    """
    rows = []
    for f in findings:
        # ── Resolve element / location ──────────────────────────────────
        location = f.get("location") or f.get("element") or {}
        tag = location.get("tag", "")
        el_id = location.get("id", "")
        el_classes = location.get("class")
        snippet = location.get("text_preview") or location.get("snippet", "")

        # Pull href/src from explicit fields first, then fall back to attributes dict
        attrs = location.get("attributes") or {}
        href = location.get("href") or attrs.get("href", "")
        raw_src = location.get("src") or attrs.get("src", "")
        src = raw_src.split("/")[-1][:60] if raw_src else ""

        if el_id:
            element_name = f"<{tag} id=\"{el_id}\">"
        elif tag == "a" and href and snippet:
            element_name = f"<a href=\"{href}\">{snippet[:50]}</a>"
        elif tag == "a" and href:
            element_name = f"<a href=\"{href}\">"
        elif tag == "a" and snippet:
            element_name = f"<a>{snippet[:60]}</a>"
        elif tag in ("img", "input") and src:
            element_name = f"<{tag} src=\"{src}\">"
        elif tag == "iframe" and src:
            element_name = f"<iframe src=\"{src}\">"
        elif el_classes:
            class_str = " ".join(el_classes) if isinstance(el_classes, list) else str(el_classes)
            element_name = f"<{tag} class=\"{class_str}\">"
        elif tag:
            element_name = f"<{tag}>"
        else:
            element_name = "(unknown)"

        # ── Resolve identifiers ─────────────────────────────────────────
        rule_id = f.get("rule_id") or f.get("issue_code", "")
        rule_name = f.get("rule_name") or f.get("checklist_item", "")
        description = f.get("description", "")

        # ── Resolve WCAG criterion (legacy schema has it, new one may not)
        wcag = f.get("wcag", {})
        criterion = wcag.get("criterion", "")
        wcag_name = wcag.get("name", "")

        # ── Build row ───────────────────────────────────────────────────
        issue_title = f"{rule_id}: {rule_name}" if rule_id else rule_name
        actual = description or rule_name
        if criterion:
            expected = f"Element should meet WCAG {criterion} ({wcag_name})"
        else:
            expected = f"Element should pass rule {rule_id}"
        category_suffix = wcag_name if wcag_name else rule_id
        category = f"Programmatic / {category_suffix}" if category_suffix else "Programmatic"

        # Use generic fix recommendation if available, fall back to description
        recommendation = _GENERIC_FIXES.get(rule_id, description or rule_name)

        rows.append(ReportRow(
            element_name=element_name,
            page_title=page_title,
            issue_title=issue_title,
            steps_to_reproduce=f"Inspect element: {snippet[:200]}",
            actual_result=actual,
            expected_result=expected,
            recommendation=recommendation,
            wcag_sc=criterion,
            category=category,
            impact=_derive_impact(rule_id=rule_id, wcag_criterion=criterion),
            log_date=log_date,
            reported_by="Programmatic",
        ))
    return rows


# ── LLM prompt normalizers ─────────────────────────────────────────────────

def _norm_page_title(data: dict, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for issue in data.get("issues", []):
        rows.append(ReportRow(
            element_name="<title>",
            issue_title=f"Page Title: {issue}",
            steps_to_reproduce="View the page in a browser and check the tab title, or inspect the <title> element in the HTML head",
            actual_result=issue,
            expected_result="Page title should be descriptive and match H1 content",
            recommendation=_get_recommendation(data),
            wcag_sc=wcag,
            category="Semantic Structure / Page Title",
            impact="Serious",
        ))
    return rows


def _norm_heading_structure(data: dict, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for issue in data.get("issues", []):
        rows.append(ReportRow(
            element_name="<h1>-<h6>",
            issue_title=f"Heading Structure: {issue}",
            steps_to_reproduce="Review the heading hierarchy of the page using browser developer tools or an accessibility tree viewer",
            actual_result=issue,
            expected_result="Headings should form a logical content outline",
            recommendation=issue,
            wcag_sc=wcag,
            category="Semantic Structure / Headings",
            impact="Moderate",
        ))
    for heading in data.get("vague_headings", []):
        rows.append(ReportRow(
            element_name="<h1>-<h6>",
            issue_title=f"Vague heading: \"{heading}\"",
            steps_to_reproduce=f"Search the page for a heading with the text: \"{heading}\"",
            actual_result=f"Heading \"{heading}\" is vague or unclear",
            expected_result="Headings should meaningfully describe their sections",
            recommendation=f"Replace \"{heading}\" with a more descriptive heading",
            wcag_sc=wcag,
            category="Semantic Structure / Headings",
            impact="Moderate",
        ))
    return rows


def _norm_link_clarity(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        if item.get("is_clear", True):
            continue
        text = item.get("text") or "(no text)"
        rows.append(ReportRow(
            element_name=f"<a> \"{text}\"",
            issue_title=f"Unclear link: \"{text}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result=item.get("reason", ""),
            expected_result="Link text should clearly describe its destination when read alone",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Semantic Structure / Links",
            impact="Moderate",
        ))
    return rows


def _norm_iframe_titles(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        if item.get("is_descriptive", True):
            continue
        title = item.get("title") or "(no title)"
        rows.append(ReportRow(
            element_name=f"<iframe> \"{title}\"",
            issue_title=f"Non-descriptive iframe title: \"{title}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result=item.get("reason", ""),
            expected_result="Iframe title should clearly describe the iframe content",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Semantic Structure / Iframes",
            impact="Serious",
        ))
    return rows


def _norm_landmark_structure(data: dict, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for issue in data.get("issues", []):
        rows.append(ReportRow(
            element_name="<main>/<nav>/<header>/<footer>",
            issue_title=f"Landmark issue: {issue}",
            steps_to_reproduce="Inspect the ARIA landmark regions of the page using an accessibility tool or browser extension",
            actual_result=issue,
            expected_result="Landmark structure should be appropriate and balanced",
            recommendation=issue,
            wcag_sc=wcag,
            category="Semantic Structure / Landmarks",
            impact="Minor",
        ))
    return rows


def _norm_label_quality(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        if item.get("is_descriptive", True):
            continue
        field_id = item.get("field_id") or "unknown"
        field_type = item.get("field_type", "input")
        label = item.get("effective_label") or "(no label)"
        issues = item.get("issues", [])
        rows.append(ReportRow(
            element_name=f"<{field_type} id=\"{field_id}\">",
            issue_title=f"Poor label quality: \"{label}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="Form field labels should be descriptive and meaningful",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Forms / Label Quality",
            impact="Serious",
        ))
    return rows


def _norm_required_field_indicators(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        issues = item.get("issues", [])
        if not issues:
            continue
        field_id = item.get("field_id") or "unknown"
        label = item.get("effective_label") or "(no label)"
        rows.append(ReportRow(
            element_name=f"<input id=\"{field_id}\">",
            issue_title=f"Required field not clearly indicated: \"{label}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="Required field status should be communicated visually and programmatically",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Forms / Required Fields",
            impact="Moderate",
        ))
    return rows


def _norm_informative_alt_quality(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        issues = item.get("issues", [])
        if not issues:
            continue
        src = item.get("src", "")
        alt = item.get("alt", "")
        rows.append(ReportRow(
            element_name=f"<img src=\"{src}\">",
            issue_title=f"Poor alt text quality ({item.get('quality', 'poor')}): \"{alt}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="Alt text should accurately and concisely describe image content",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Non-text Content / Informative Images",
            impact="Serious",
        ))
    return rows


def _norm_decorative_verification(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        if item.get("likely_decorative", True):
            continue
        src = item.get("src", "")
        rows.append(ReportRow(
            element_name=f"<img src=\"{src}\" alt=\"\">",
            issue_title=f"Possibly mis-marked as decorative: {src}",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result=item.get("reason", ""),
            expected_result="Image marked as decorative (alt=\"\") should truly be decorative",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Non-text Content / Decorative Verification",
            impact="Moderate",
        ))
    return rows


def _norm_actionable_image_alt(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        issues = item.get("issues", [])
        if not issues:
            continue
        src = item.get("src", "")
        context = item.get("context", "in_link")
        alt = item.get("alt") or "(empty)"
        rows.append(ReportRow(
            element_name=f"<img src=\"{src}\"> ({context})",
            issue_title=f"Actionable image alt issue: \"{alt}\"",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="Images in links/buttons should describe the action/destination, not appearance",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Non-text Content / Actionable Images",
            impact="Critical",
        ))
    return rows


def _norm_svg_accessibility(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        issues = item.get("issues", [])
        if not issues:
            continue
        label = item.get("aria_label") or item.get("title") or "(unlabeled)"
        rows.append(ReportRow(
            element_name=f"<svg> \"{label}\"",
            issue_title=f"SVG accessibility issue: {label}",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="SVGs should have role=\"img\" and an accessible name via title + aria-labelledby",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Non-text Content / SVGs",
            impact="Moderate",
        ))
    return rows


def _norm_icon_font_accessibility(data: list, wcag: str, **ctx) -> list[ReportRow]:
    rows = []
    for item in data:
        issues = item.get("issues", [])
        if not issues:
            continue
        classes = item.get("classes", "")
        pattern = item.get("pattern", "")
        rows.append(ReportRow(
            element_name=f"<i class=\"{classes}\">",
            issue_title=f"Icon font issue ({pattern}): {classes}",
            steps_to_reproduce=item.get("location_hint") or "",
            actual_result="; ".join(issues),
            expected_result="Icon fonts should be properly labeled or hidden from assistive technology",
            recommendation=_get_recommendation(item),
            wcag_sc=wcag,
            category="Non-text Content / Icon Fonts",
            impact="Moderate",
        ))
    return rows


# ── Normalizer registry ────────────────────────────────────────────────────

NORMALIZERS: dict[str, callable] = {
    "page_title": _norm_page_title,
    "heading_structure": _norm_heading_structure,
    "link_clarity": _norm_link_clarity,
    "iframe_titles": _norm_iframe_titles,
    "landmark_structure": _norm_landmark_structure,
    "label_quality": _norm_label_quality,
    "required_field_indicators": _norm_required_field_indicators,
    "informative_alt_quality": _norm_informative_alt_quality,
    "decorative_verification": _norm_decorative_verification,
    "actionable_image_alt": _norm_actionable_image_alt,
    "svg_accessibility": _norm_svg_accessibility,
    "icon_font_accessibility": _norm_icon_font_accessibility,
}


# ── LLM deduplication pass ────────────────────────────────────────────────────

def deduplicate_with_llm(
    rows: list[ReportRow],
    api_key: str,
    model: str,
) -> list[ReportRow]:
    """Use an LLM to remove duplicate findings from *rows*.

    Serializes each row to a compact single-line description, asks the model
    to identify duplicate IDX values, and filters them out. Returns the
    deduplicated list. Falls back to the original list on any error.

    Args:
        rows: Findings after false-positive filtering (IDs not yet assigned).
        api_key: API key for Anthropic or OpenAI.
        model: Model ID (e.g. ``claude-sonnet-4-20250514``).

    Returns:
        A (possibly shorter) list of ReportRow with duplicates removed.
    """
    if not rows or not api_key:
        return rows

    # Build a compact numbered description for each row (1-based IDX).
    lines = []
    for idx, row in enumerate(rows, start=1):
        detail = (row.actual_result or row.issue_title)[:120]
        lines.append(
            f"IDX {idx} | {row.category} | {row.element_name} | "
            f"{row.issue_title} | {detail}"
        )
    findings_text = "\n".join(lines)

    prompt = _DEDUP_PROMPT.format(findings=findings_text)

    try:
        # Detect provider from model ID (same heuristic used elsewhere).
        is_openai = "gpt" in model.lower() or "o1" in model.lower() or "o3" in model.lower()

        if is_openai:
            import openai
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=model,
                max_tokens=512,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.choices[0].message.content or "[]"
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model=model,
                max_tokens=512,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = "".join(
                block.text for block in message.content if block.type == "text"
            )

        to_remove: list[int] = safe_parse_json(response_text)
        if not isinstance(to_remove, list):
            raise ValueError(f"Expected list, got {type(to_remove)}")

        remove_set = {int(i) for i in to_remove}
        original_count = len(rows)
        deduped = [row for idx, row in enumerate(rows, start=1) if idx not in remove_set]
        removed = original_count - len(deduped)
        if removed:
            print(f"  LLM deduplication removed: {removed} duplicate(s)")
        return deduped

    except Exception as exc:
        print(f"  WARNING: LLM deduplication failed ({exc}), skipping.")
        return rows


# ── Main report generation ─────────────────────────────────────────────────

def generate_report(
    output_dir: Path,
    report_dir: Path,
    api_key: str | None = None,
    model: str | None = None,
) -> Path:
    """Generate a unified CSV report from pipeline output.

    Args:
        output_dir: Directory containing manifest.json, programmatic_findings.json,
                    and prompts/ subdirectory.
        report_dir: Directory to write the CSV report into.
        api_key: Optional API key for the LLM deduplication pass. When omitted
                 the deduplication step is skipped.
        model: Model ID to use for deduplication (e.g. ``claude-sonnet-4-20250514``).
               Required when *api_key* is provided.

    Returns:
        Path to the written CSV file.
    """
    # 1. Load manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    run_timestamp = manifest.get("run_timestamp", "")
    log_date = run_timestamp[:10] if run_timestamp else "unknown"
    manifest_model = manifest.get("model", "unknown")

    # 2. Extract page title from page_title prompt payload
    page_title_data = load_prompt_file(output_dir / "prompts" / "page_title.json")
    page_title = ""
    if page_title_data:
        page_title = extract_page_title_from_payload(page_title_data)
    if not page_title:
        # Fallback to HTML filename
        page_title = Path(manifest.get("html_file", "unknown")).stem

    # 3. Normalize programmatic findings
    prog_path = output_dir / "programmatic_findings.json"
    prog_findings = []
    if prog_path.exists():
        with open(prog_path, encoding="utf-8") as f:
            prog_raw = json.load(f)
        prog_findings = normalize_programmatic(prog_raw, page_title, log_date)

    # 4. Normalize LLM prompt results
    llm_findings: list[ReportRow] = []
    prompts_dir = output_dir / "prompts"

    for prompt_entry in manifest.get("prompts_executed", []):
        name = prompt_entry["name"]
        wcag_list = prompt_entry.get("wcag_criteria", [])
        wcag_str = ", ".join(wcag_list)

        normalizer = NORMALIZERS.get(name)
        if normalizer is None:
            continue

        prompt_file = prompts_dir / f"{name}.json"
        prompt_data = load_prompt_file(prompt_file)
        if prompt_data is None:
            continue

        api_result = prompt_data.get("api_result", {})
        if not api_result.get("success", False):
            continue

        response_text = api_result.get("response", "")
        try:
            parsed = safe_parse_json(response_text)
        except (json.JSONDecodeError, ValueError):
            print(f"  WARNING: Could not parse JSON response for {name}, skipping")
            continue

        new_rows = normalizer(parsed, wcag=wcag_str)

        # Fill in shared fields
        for row in new_rows:
            row.page_title = page_title
            row.log_date = log_date
            row.reported_by = manifest_model

        llm_findings.extend(new_rows)

    # 5. Combine all findings
    all_rows = prog_findings + llm_findings

    # 6. Filter false positives using the source HTML
    soup = _load_html_for_filtering(output_dir)
    kept, suppressed = filter_false_positives(all_rows, soup)

    if suppressed:
        print(f"  False positives suppressed: {len(suppressed)}")
        # Group by category for visibility
        fp_cats: dict[str, int] = {}
        for row in suppressed:
            fp_cats[row.category] = fp_cats.get(row.category, 0) + 1
        for cat, count in sorted(fp_cats.items(), key=lambda x: -x[1]):
            print(f"    {cat}: {count}")

    # 6b. LLM deduplication pass (optional — skipped when no api_key provided)
    dedup_model = model or manifest_model
    if api_key and dedup_model:
        kept = deduplicate_with_llm(kept, api_key, dedup_model)

    # 7. Assign sequential IDs
    for i, row in enumerate(kept, start=1):
        row.ID = i

    # 8. Write CSV
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"report_{log_date}.csv"

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in kept:
            writer.writerow({col: getattr(row, col) for col in CSV_COLUMNS})

    # 9. Print summary
    print(f"Report generated: {report_path}")
    print(f"  Programmatic findings: {len(prog_findings)}")
    print(f"  LLM findings:         {len(llm_findings)}")
    print(f"  False positives removed: {len(suppressed)}")
    print(f"  Final rows:           {len(kept)}")
    print(f"  Log date:             {log_date}")
    print(f"  Model:                {manifest_model}")

    return report_path


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    """CLI entry point for report generation."""
    parser = argparse.ArgumentParser(
        description="Generate a unified CSV report from pipeline output."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Directory containing pipeline output (default: ./output)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("./test_results/claude"),
        help="Directory to write the CSV report (default: ./test_results/claude/)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for LLM deduplication pass (optional). "
             "Falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY env vars if omitted.",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-20250514",
        help="Model to use for LLM deduplication (default: claude-sonnet-4-20250514)",
    )
    args = parser.parse_args()

    # Resolve API key from env if not passed explicitly
    import os
    api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")

    generate_report(args.output_dir, args.report_dir, api_key=api_key, model=args.model)


if __name__ == "__main__":
    main()