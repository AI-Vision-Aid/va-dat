"""Daily operational reporting for the durable DAT audit service."""

from __future__ import annotations

from datetime import datetime, timezone
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
                "mode": "Uploaded URL list"
                if job.get("audit_mode") == "url_list"
                else "Full-site crawl",
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
