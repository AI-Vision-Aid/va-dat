"""Build a consolidated HTML/CSV/ZIP report for a whole-site audit."""

from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SiteReport:
    """Generated report artifacts and aggregate counts."""

    html_bytes: bytes
    csv_bytes: bytes
    zip_bytes: bytes
    total_findings: int
    pages_succeeded: int
    pages_failed: int


def _csv_rows(csv_text: str | None, page_url: str) -> list[dict[str, str]]:
    if not csv_text:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        normalized = {key: (value or "") for key, value in row.items() if key}
        normalized["page_url"] = page_url
        rows.append(normalized)
    return rows


def summarize_rows(rows: list[dict[str, str]]) -> str:
    """Return a concise deterministic issue summary for one page."""
    if not rows:
        return "No reportable findings were produced."
    categories = Counter(row.get("category") or "Uncategorized" for row in rows)
    top = ", ".join(f"{name} ({count})" for name, count in categories.most_common(3))
    return f"{len(rows)} finding(s). Most common categories: {top}."


def _render_html(
    *,
    base_url: str,
    model: str,
    capped: bool,
    candidate_count: int,
    pages: list[dict],
    all_rows: list[dict[str, str]],
) -> bytes:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_succeeded = sum(1 for page in pages if page.get("status") == "complete")
    total_failed = sum(1 for page in pages if page.get("status") == "failed")
    cap_note = (
        "The 200-page safety cap was reached; remaining candidates were not audited."
        if capped
        else "All selected public pages were processed within the 200-page cap."
    )

    page_rows = []
    for index, page in enumerate(pages, start=1):
        url = html.escape(page.get("url", ""))
        status = html.escape(page.get("status", "unknown"))
        summary = html.escape(page.get("issue_summary", "No summary available."))
        page_rows.append(
            f"<tr><td>{index}</td><td><a href=\"{url}\">{url}</a></td>"
            f"<td>{status}</td><td>{int(page.get('issue_count', 0))}</td><td>{summary}</td></tr>"
        )

    finding_sections = []
    rows_by_page: dict[str, list[dict[str, str]]] = {}
    for row in all_rows:
        rows_by_page.setdefault(row.get("page_url", ""), []).append(row)
    for page in pages:
        page_url = page.get("url", "")
        rows = rows_by_page.get(page_url, [])
        rendered_rows = []
        for row in rows:
            rendered_rows.append(
                "<tr>"
                f"<td>{html.escape(row.get('impact', ''))}</td>"
                f"<td>{html.escape(row.get('wcag_sc', ''))}</td>"
                f"<td>{html.escape(row.get('issue_title', ''))}</td>"
                f"<td>{html.escape(row.get('element_name', ''))}</td>"
                f"<td>{html.escape(row.get('recommendation', ''))}</td>"
                "</tr>"
            )
        if not rendered_rows:
            rendered_rows.append("<tr><td colspan=\"5\">No reportable findings.</td></tr>")
        finding_sections.append(
            "<section class=\"page-detail\">"
            f"<h2>{html.escape(page_url)}</h2>"
            f"<p>{html.escape(page.get('issue_summary', ''))}</p>"
            "<table><thead><tr><th>Impact</th><th>WCAG</th><th>Issue</th>"
            "<th>Element</th><th>Recommendation</th></tr></thead><tbody>"
            + "".join(rendered_rows)
            + "</tbody></table></section>"
        )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Aid DAT whole-site report</title>
<style>
body{{font-family:Arial,sans-serif;color:#172033;line-height:1.45;margin:0}}main{{max-width:1100px;margin:auto;padding:36px}}
h1,h2{{color:#183f7a}}a{{color:#0b5cab;overflow-wrap:anywhere}}.cover{{min-height:90vh;page-break-after:always}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin:24px 0}}.metric{{border:2px solid #dce6f4;border-radius:8px;padding:16px}}
.metric strong{{display:block;font-size:1.8rem}}table{{width:100%;border-collapse:collapse;margin:16px 0 28px;font-size:.88rem}}
th,td{{border:1px solid #bac7d8;padding:8px;vertical-align:top;text-align:left}}th{{background:#eef4fb}}.page-detail{{page-break-before:always}}
@media(max-width:700px){{.metrics{{grid-template-columns:1fr 1fr}}main{{padding:18px}}table{{display:block;overflow:auto}}}}
@media print{{a{{color:inherit;text-decoration:none}}main{{max-width:none;padding:0}}}}
</style></head><body><main>
<section class="cover"><p>Vision Aid Digital Accessibility Testing</p><h1>Whole-Site Accessibility Audit Report</h1>
<p><strong>Base URL:</strong> <a href="{html.escape(base_url)}">{html.escape(base_url)}</a><br>
<strong>Generated:</strong> {generated}<br><strong>Model:</strong> {html.escape(model)}</p>
<div class="metrics"><div class="metric"><strong>{len(pages)}</strong>Pages selected</div>
<div class="metric"><strong>{total_succeeded}</strong>Pages completed</div><div class="metric"><strong>{total_failed}</strong>Pages failed</div>
<div class="metric"><strong>{len(all_rows)}</strong>Total findings</div></div>
<p><strong>Discovery:</strong> {candidate_count} candidate URL(s) found. {html.escape(cap_note)}</p>
<h2>Page Summary</h2><table><thead><tr><th>#</th><th>Page</th><th>Status</th><th>Issues</th><th>Summary</th></tr></thead>
<tbody>{''.join(page_rows)}</tbody></table></section>
{''.join(finding_sections)}
</main></body></html>"""
    return document.encode("utf-8")


def _render_csv(all_rows: list[dict[str, str]]) -> bytes:
    preferred = [
        "page_url",
        "ID",
        "element_name",
        "browser_combination",
        "page_title",
        "issue_title",
        "steps_to_reproduce",
        "actual_result",
        "expected_result",
        "recommendation",
        "wcag_sc",
        "category",
        "impact",
        "log_date",
        "reported_by",
    ]
    extras = sorted({key for row in all_rows for key in row if key not in preferred})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[*preferred, *extras], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(all_rows)
    return output.getvalue().encode("utf-8-sig")


def build_site_report(
    *,
    base_url: str,
    model: str,
    capped: bool,
    candidate_count: int,
    pages: list[dict],
    page_results: list[dict],
) -> SiteReport:
    """Build a printable HTML report and machine-readable CSV inside a ZIP."""
    results_by_url = {item.get("page_url", ""): item for item in page_results}
    all_rows: list[dict[str, str]] = []
    normalized_pages = []
    for page in pages:
        page_url = page.get("url", "")
        result = results_by_url.get(page_url, {})
        rows = _csv_rows(result.get("csv_report"), page_url)
        all_rows.extend(rows)
        normalized = dict(page)
        normalized["issue_count"] = len(rows)
        normalized["issue_summary"] = (
            page.get("error") if page.get("status") == "failed" else summarize_rows(rows)
        )
        normalized_pages.append(normalized)

    html_bytes = _render_html(
        base_url=base_url,
        model=model,
        capped=capped,
        candidate_count=candidate_count,
        pages=normalized_pages,
        all_rows=all_rows,
    )
    csv_bytes = _render_csv(all_rows)
    summary_json = json.dumps(
        {
            "base_url": base_url,
            "model": model,
            "pages_selected": len(normalized_pages),
            "pages_completed": sum(1 for page in normalized_pages if page.get("status") == "complete"),
            "pages_failed": sum(1 for page in normalized_pages if page.get("status") == "failed"),
            "candidate_count": candidate_count,
            "capped": capped,
            "total_findings": len(all_rows),
            "pages": [
                {
                    "url": page.get("url"),
                    "status": page.get("status"),
                    "issue_count": page.get("issue_count"),
                    "summary": page.get("issue_summary"),
                }
                for page in normalized_pages
            ],
        },
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("DAT-whole-site-report.html", html_bytes)
        archive.writestr("DAT-findings.csv", csv_bytes)
        archive.writestr("DAT-summary.json", summary_json)

    return SiteReport(
        html_bytes=html_bytes,
        csv_bytes=csv_bytes,
        zip_bytes=zip_buffer.getvalue(),
        total_findings=len(all_rows),
        pages_succeeded=sum(1 for page in normalized_pages if page.get("status") == "complete"),
        pages_failed=sum(1 for page in normalized_pages if page.get("status") == "failed"),
    )
