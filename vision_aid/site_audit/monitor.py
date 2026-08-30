"""Daily operational reporting for the durable DAT audit service."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _site_label(job: dict) -> str:
    if job.get("audit_mode") == "html_upload":
        return "Uploaded HTML (site not supplied)"
    hosts = [str(item).strip().lower() for item in job.get("site_hosts", []) if item]
    if not hosts:
        host = (urlparse(str(job.get("base_url") or "")).hostname or "").lower()
        if host:
            hosts = [host]
    hosts = sorted(set(hosts))
    if not hosts:
        return "Unknown site"
    if len(hosts) <= 3:
        return ", ".join(hosts)
    return f"{', '.join(hosts[:3])} (+{len(hosts) - 3} more)"


def build_daily_monitor_report(
    *,
    jobs: list[dict],
    window_start: datetime,
    window_end: datetime,
    checks: dict[str, bool],
    issues: list[str],
) -> dict:
    """Build a privacy-safe, plain-text 24-hour usage and health summary."""
    start_utc = _utc(window_start) or window_start
    end_utc = _utc(window_end) or window_end
    ordered_jobs = sorted(
        jobs,
        key=lambda job: _utc(job.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )

    rows: list[dict] = []
    total_pages = 0
    total_failed_pages = 0
    total_cost = 0.0
    pending_costs = 0
    for job in ordered_jobs:
        completed = max(0, int(job.get("pages_completed") or 0))
        failed = max(0, int(job.get("pages_failed") or 0))
        pages_processed = completed + failed
        total_pages += pages_processed
        total_failed_pages += failed
        raw_cost = job.get("estimated_cost_usd")
        cost = None
        if raw_cost is not None:
            try:
                cost = max(0.0, float(raw_cost))
                total_cost += cost
            except (TypeError, ValueError):
                cost = None
        if cost is None:
            pending_costs += 1
        created_at = _utc(job.get("created_at"))
        rows.append(
            {
                "site": _site_label(job),
                "mode": {
                    "url_list": "Uploaded URL list",
                    "single_url": "Single-page URL",
                    "html_upload": "Uploaded HTML",
                    "legacy_crawl": "Legacy nested crawl",
                }.get(str(job.get("audit_mode") or "crawl"), "Full-site crawl"),
                "status": str(job.get("status") or "unknown"),
                "pages_processed": pages_processed,
                "pages_total": max(0, int(job.get("pages_total") or 0)),
                "pages_failed": failed,
                "estimated_cost_usd": cost,
                "model": str(job.get("model") or "unknown"),
                "started_at": created_at.isoformat() if created_at else "unknown",
            }
        )

    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        health_status = "error"
        health_label = "NOT WORKING"
    elif issues:
        health_status = "warning"
        health_label = "WORKING WITH ISSUES"
    else:
        health_status = "ok"
        health_label = "WORKING OK"

    start_et = start_utc.astimezone(EASTERN)
    end_et = end_utc.astimezone(EASTERN)
    lines = [
        "Vision Aid DAT daily usage and health report",
        "",
        f"Reporting window: {start_et:%Y-%m-%d %I:%M %p} to {end_et:%Y-%m-%d %I:%M %p} Eastern",
        f"Tool health: {health_label}",
        "",
        "Health checks:",
    ]
    friendly_checks = {
        "service_endpoint": "Service health endpoint",
        "core_configuration": "Background audit configuration",
        "firestore": "Audit job database",
        "report_storage": "Report storage",
        "saved_model_key": "Saved AI model credential",
        "email_configuration": "Email configuration",
    }
    for name, passed in checks.items():
        lines.append(f"- {friendly_checks.get(name, name)}: {'OK' if passed else 'FAILED'}")
    if issues:
        lines.extend(["", "Issues requiring attention:"])
        lines.extend(f"- {issue}" for issue in issues)

    lines.extend(
        [
            "",
            "24-hour usage totals:",
            f"- Audit requests: {len(rows)}",
            f"- Pages processed: {total_pages}",
            f"- Pages failed: {total_failed_pages}",
            f"- Estimated AI cost: ${total_cost:.6f}",
        ]
    )
    if pending_costs:
        lines.append(f"- Audits with cost not yet available: {pending_costs}")

    lines.extend(["", "Sites scanned:"])
    if not rows:
        lines.append("- No background site audits were requested in this period.")
    for index, row in enumerate(rows, start=1):
        cost_text = (
            f"${row['estimated_cost_usd']:.6f}"
            if row["estimated_cost_usd"] is not None
            else "pending/unavailable"
        )
        lines.extend(
            [
                f"{index}. {row['site']}",
                f"   Mode: {row['mode']}",
                f"   Status: {row['status']}",
                f"   Pages: {row['pages_processed']} processed / {row['pages_total']} selected; {row['pages_failed']} failed",
                f"   Model: {row['model']}",
                f"   Estimated cost: {cost_text}",
            ]
        )

    return {
        "window_start": start_utc,
        "window_end": end_utc,
        "health_status": health_status,
        "health_label": health_label,
        "checks": checks,
        "issues": issues,
        "audits": rows,
        "audit_count": len(rows),
        "pages_processed": total_pages,
        "pages_failed": total_failed_pages,
        "estimated_cost_usd": round(total_cost, 6),
        "pending_cost_count": pending_costs,
        "subject": f"DAT daily monitor: {health_label} — {len(rows)} audit(s), {total_pages} page(s)",
        "text": "\n".join(lines) + "\n",
    }


def build_daily_monitor_html(report: dict) -> bytes:
    """Render a self-contained, admin-only web copy of a daily report."""
    window_start = _utc(report.get("window_start"))
    window_end = _utc(report.get("window_end"))
    window_text = "Reporting window unavailable"
    if window_start and window_end:
        window_text = (
            f"{window_start.astimezone(EASTERN):%Y-%m-%d %I:%M %p} to "
            f"{window_end.astimezone(EASTERN):%Y-%m-%d %I:%M %p} Eastern"
        )
    status_class = {
        "ok": "ok",
        "warning": "warning",
        "error": "error",
    }.get(str(report.get("health_status")), "error")
    friendly_checks = {
        "service_endpoint": "Service health endpoint",
        "core_configuration": "Background audit configuration",
        "firestore": "Audit job database",
        "report_storage": "Report storage",
        "saved_model_key": "Saved AI model credential",
        "email_configuration": "Email configuration",
    }
    check_items = "".join(
        "<li><span>{}</span><strong class=\"{}\">{}</strong></li>".format(
            escape(friendly_checks.get(name, name)),
            "ok" if passed else "error",
            "OK" if passed else "FAILED",
        )
        for name, passed in report.get("checks", {}).items()
    )
    issues = report.get("issues") or []
    issue_section = ""
    if issues:
        issue_section = (
            "<section><h2>Issues requiring attention</h2><ul>"
            + "".join(f"<li>{escape(str(issue))}</li>" for issue in issues)
            + "</ul></section>"
        )
    rows = []
    for audit in report.get("audits", []):
        cost = audit.get("estimated_cost_usd")
        cost_text = f"${float(cost):.6f}" if cost is not None else "Pending/unavailable"
        rows.append(
            "<tr>"
            f"<th scope=\"row\">{escape(str(audit.get('site') or 'Unknown site'))}</th>"
            f"<td>{escape(str(audit.get('mode') or 'Unknown'))}</td>"
            f"<td>{escape(str(audit.get('status') or 'unknown'))}</td>"
            f"<td>{int(audit.get('pages_processed') or 0)} / {int(audit.get('pages_total') or 0)}</td>"
            f"<td>{int(audit.get('pages_failed') or 0)}</td>"
            f"<td>{escape(str(audit.get('model') or 'unknown'))}</td>"
            f"<td>{escape(cost_text)}</td>"
            "</tr>"
        )
    table_body = "".join(rows) or (
        '<tr><td colspan="7">No audits were active during this reporting period.</td></tr>'
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>DAT daily monitor report</title>
<style>
body{{font-family:Arial,sans-serif;margin:0;background:#f3f6fa;color:#172033;line-height:1.5}}main{{max-width:1100px;margin:auto;padding:32px 20px 56px}}a{{color:#174f8a}}.card{{background:#fff;border:1px solid #cad5e2;border-radius:12px;padding:22px;margin:18px 0}}.status{{display:inline-block;border-radius:999px;padding:6px 12px;font-weight:700}}.status.ok{{background:#dff4e8;color:#14532d}}.status.warning{{background:#fff1c7;color:#713f12}}.status.error{{background:#fde2e2;color:#7f1d1d}}.ok{{color:#166534}}.error{{color:#991b1b}}.checks{{padding:0;list-style:none}}.checks li{{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid #e5e9ef;padding:8px 0}}.totals{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.metric{{background:#eef4fb;border-radius:8px;padding:14px}}.metric strong{{display:block;font-size:1.35rem}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border:1px solid #d7dee8;text-align:left;vertical-align:top}}thead th{{background:#eaf1f8}}.table-wrap{{overflow-x:auto}}@media(max-width:600px){{main{{padding:20px 12px}}}}
</style></head><body><main>
<p><a href="/analytics">&larr; Back to Analytics</a></p>
<h1>Vision Aid DAT daily usage and health report</h1>
<p>{escape(window_text)}</p>
<p><span class="status {status_class}">{escape(str(report.get('health_label') or 'UNKNOWN'))}</span></p>
<section class="card"><h2>24-hour usage totals</h2><div class="totals">
<div class="metric">Audit requests<strong>{int(report.get('audit_count') or 0)}</strong></div>
<div class="metric">Pages processed<strong>{int(report.get('pages_processed') or 0)}</strong></div>
<div class="metric">Pages failed<strong>{int(report.get('pages_failed') or 0)}</strong></div>
<div class="metric">Estimated AI cost<strong>${float(report.get('estimated_cost_usd') or 0):.6f}</strong></div>
</div></section>
<section class="card"><h2>Health checks</h2><ul class="checks">{check_items}</ul></section>
{issue_section}
<section><h2>Sites and audits</h2><div class="table-wrap"><table><thead><tr><th>Site or source</th><th>Mode</th><th>Status</th><th>Pages processed / selected</th><th>Failed</th><th>Model</th><th>Estimated cost</th></tr></thead><tbody>{table_body}</tbody></table></div></section>
</main></body></html>"""
    return document.encode("utf-8")
