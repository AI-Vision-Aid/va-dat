"""Durable Cloud Tasks orchestration for asynchronous whole-site audits."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable
from urllib.parse import urlparse

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore, storage, tasks_v2
from google.cloud.firestore_v1.base_query import FieldFilter
from google.protobuf import duration_pb2

from .crawler import discover_site_urls, fetch_public_html, validate_public_url
from .report import build_site_report


ACTIVE_STATUSES = {"queued", "discovering", "auditing", "finalizing"}
TERMINAL_PAGE_STATUSES = {"complete", "failed"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _email_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()


def validate_request_email(address: str, allowed_domains: set[str]) -> str:
    """Validate an email address and optional deployment domain allowlist."""
    normalized = str(address or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized) or len(normalized) > 254:
        raise ValueError("Enter a valid report delivery email address")
    domain = normalized.rsplit("@", 1)[1]
    if allowed_domains and domain not in allowed_domains:
        allowed = ", ".join(sorted(allowed_domains))
        raise ValueError(f"Report delivery is currently limited to: {allowed}")
    return normalized


def _safe_task_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)[:500]


class TaskDispatcher:
    """Enqueue authenticated calls back to the DAT Cloud Run service."""

    def __init__(self, *, project: str, location: str, queue: str, service_url: str, token: str):
        self.client = tasks_v2.CloudTasksClient()
        self.parent = self.client.queue_path(project, location, queue)
        self.service_url = service_url.rstrip("/")
        self.token = token

    def enqueue(self, path: str, payload: dict, task_id: str) -> None:
        """Create one idempotently named HTTP task."""
        name = f"{self.parent}/tasks/{_safe_task_id(task_id)}"
        task = {
            "name": name,
            "dispatch_deadline": duration_pb2.Duration(seconds=1_800),
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self.service_url}{path}",
                "headers": {
                    "Content-Type": "application/json",
                    "X-DAT-Job-Token": self.token,
                },
                "body": json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            },
        }
        try:
            self.client.create_task(parent=self.parent, task=task)
        except AlreadyExists:
            return


def send_report_email(
    *,
    recipient: str,
    base_url: str,
    pages: int,
    findings: int,
    download_url: str,
    report_zip: bytes,
) -> None:
    """Send the completed site report using deployment SMTP settings."""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()
    if not all((smtp_host, smtp_user, smtp_password, smtp_from)):
        raise RuntimeError("SMTP delivery is not configured")

    host = urlparse(base_url).hostname or base_url
    message = EmailMessage()
    message["Subject"] = f"DAT whole-site accessibility report: {host}"
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content(
        "Your Vision Aid DAT whole-site accessibility report is ready.\n\n"
        f"Site: {base_url}\nPages processed: {pages}\nFindings: {findings}\n"
        f"Download: {download_url}\n\n"
        "The attached ZIP contains a printable HTML report, a CSV findings file, "
        "and a JSON page summary."
    )
    if len(report_zip) <= 18 * 1024 * 1024:
        message.add_attachment(
            report_zip,
            maintype="application",
            subtype="zip",
            filename="DAT-whole-site-report.zip",
        )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


class SiteAuditCoordinator:
    """Create, process, finalize, and serve asynchronous site-audit jobs."""

    def __init__(self) -> None:
        self.project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT", "")
        self.location = os.getenv("DAT_TASK_LOCATION", "us-east1")
        self.queue_name = os.getenv("DAT_TASK_QUEUE", "ability-bazaar-dat-audits")
        self.service_url = os.getenv("DAT_SERVICE_URL", "").rstrip("/")
        self.bucket_name = os.getenv("DAT_REPORT_BUCKET", "")
        self.job_token = os.getenv("DAT_JOB_TOKEN", "").strip()
        self.model = os.getenv("DAT_MODEL", "gpt-5.6-sol")
        self.api_key = os.getenv("DAT_OPENAI_API_KEY", "").strip()
        self.allowed_domains = {
            item.strip().lower()
            for item in os.getenv("DAT_ALLOWED_EMAIL_DOMAINS", "").split(",")
            if item.strip()
        }
        self.collection = os.getenv("DAT_JOB_COLLECTION", "dat_site_audit_jobs")
        self._db = None
        self._storage = None
        self._dispatcher = None

    @property
    def configured(self) -> bool:
        return bool(
            self.project
            and self.service_url
            and self.bucket_name
            and self.job_token
            and self.api_key
        )

    @property
    def db(self):
        if self._db is None:
            self._db = firestore.Client(project=self.project)
        return self._db

    @property
    def bucket(self):
        if self._storage is None:
            self._storage = storage.Client(project=self.project)
        return self._storage.bucket(self.bucket_name)

    @property
    def dispatcher(self) -> TaskDispatcher:
        if self._dispatcher is None:
            self._dispatcher = TaskDispatcher(
                project=self.project,
                location=self.location,
                queue=self.queue_name,
                service_url=self.service_url,
                token=self.job_token,
            )
        return self._dispatcher

    def _job_ref(self, job_id: str):
        return self.db.collection(self.collection).document(job_id)

    def internal_token_valid(self, supplied: str) -> bool:
        return bool(self.job_token and hmac.compare_digest(self.job_token, supplied or ""))

    def create_job(self, *, base_url: str, email: str) -> dict:
        """Persist and enqueue a new whole-site audit job."""
        if not self.configured:
            raise RuntimeError("Asynchronous site auditing is not configured")
        normalized_url = validate_public_url(str(base_url or ""))
        normalized_email = validate_request_email(email, self.allowed_domains)
        email_hash = _email_hash(normalized_email)

        recent_for_email = (
            self.db.collection(self.collection)
            .where(filter=FieldFilter("email_hash", "==", email_hash))
            .limit(10)
            .stream()
        )
        if any(
            snapshot.to_dict().get("status") in ACTIVE_STATUSES
            for snapshot in recent_for_email
        ):
            raise ValueError("An audit is already running for this email address")

        job_id = secrets.token_urlsafe(24)
        job = {
            "job_id": job_id,
            "base_url": normalized_url,
            "email": normalized_email,
            "email_hash": email_hash,
            "status": "queued",
            "model": self.model,
            "max_pages": 200,
            "pages_total": 0,
            "pages_completed": 0,
            "pages_failed": 0,
            "candidate_count": 0,
            "capped": False,
            "created_at": _now(),
            "updated_at": _now(),
            "expires_at": _now() + timedelta(days=30),
        }
        self._job_ref(job_id).set(job)
        try:
            self.dispatcher.enqueue(
                "/api/internal/site-audits/discover",
                {"job_id": job_id},
                f"{job_id}-discover",
            )
        except Exception:
            self._job_ref(job_id).update({"status": "enqueue_failed", "updated_at": _now()})
            raise
        return self.public_job(job)

    def get_job(self, job_id: str) -> dict | None:
        snapshot = self._job_ref(job_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def public_job(self, job: dict) -> dict:
        """Return status fields safe for an unauthenticated capability URL."""
        if not job:
            return {}
        result = {
            key: job.get(key)
            for key in (
                "job_id",
                "base_url",
                "status",
                "model",
                "max_pages",
                "pages_total",
                "pages_completed",
                "pages_failed",
                "candidate_count",
                "capped",
                "total_findings",
                "report_ready",
                "email_sent",
                "last_error",
            )
            if key in job
        }
        total = int(job.get("pages_total") or 0)
        finished = int(job.get("pages_completed") or 0) + int(job.get("pages_failed") or 0)
        result["progress_percent"] = round((finished / total) * 100) if total else 0
        if job.get("status") == "complete":
            result["report_url"] = f"{self.service_url}/api/site-audits/{job['job_id']}/report"
        for key, value in list(result.items()):
            if isinstance(value, datetime):
                result[key] = value.isoformat()
        return result

    def run_discovery(self, job_id: str) -> dict:
        """Discover pages and enqueue one durable task per selected page."""
        job_ref = self._job_ref(job_id)
        job = self.get_job(job_id)
        if not job:
            raise ValueError("Unknown job")
        if job.get("status") == "complete":
            return self.public_job(job)

        existing_pages = list(job_ref.collection("pages").stream())
        if not existing_pages:
            job_ref.update({"status": "discovering", "updated_at": _now()})
            discovery = discover_site_urls(
                job["base_url"],
                api_key=self.api_key,
                model=self.model,
                max_pages=200,
            )
            batch = self.db.batch()
            for index, page in enumerate(discovery.pages):
                page_ref = job_ref.collection("pages").document(f"{index:03d}")
                batch.set(
                    page_ref,
                    {
                        "index": index,
                        "url": page.url,
                        "title": page.title,
                        "source": page.source,
                        "status": "queued",
                        "updated_at": _now(),
                    },
                )
            batch.commit()
            job_ref.update(
                {
                    "pages_total": len(discovery.pages),
                    "candidate_count": discovery.candidate_count,
                    "capped": discovery.capped,
                    "ai_discovery_used": discovery.ai_used,
                    "sitemap_count": discovery.sitemap_count,
                    "updated_at": _now(),
                }
            )
            existing_pages = list(job_ref.collection("pages").stream())

        for snapshot in existing_pages:
            page = snapshot.to_dict()
            index = int(page["index"])
            self.dispatcher.enqueue(
                "/api/internal/site-audits/page",
                {"job_id": job_id, "page_id": snapshot.id},
                f"{job_id}-page-{index:03d}",
            )
        job_ref.update({"status": "auditing", "updated_at": _now()})
        return self.public_job(self.get_job(job_id))

    def _store_page_result(self, job_id: str, page_id: str, payload: dict) -> str:
        object_name = f"jobs/{job_id}/pages/{page_id}.json.gz"
        compressed = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.bucket.blob(object_name).upload_from_string(
            compressed,
            content_type="application/gzip",
        )
        return object_name

    def _load_page_result(self, object_name: str) -> dict:
        compressed = self.bucket.blob(object_name).download_as_bytes()
        return json.loads(gzip.decompress(compressed).decode("utf-8"))

    def _mark_page_terminal(
        self,
        *,
        job_id: str,
        page_id: str,
        status: str,
        result_object: str | None = None,
        error: str | None = None,
        issue_count: int = 0,
    ) -> bool:
        """Idempotently finish a page and return whether finalization is ready."""
        job_ref = self._job_ref(job_id)
        page_ref = job_ref.collection("pages").document(page_id)
        transaction = self.db.transaction()

        @firestore.transactional
        def update(transaction):
            job_snapshot = job_ref.get(transaction=transaction)
            page_snapshot = page_ref.get(transaction=transaction)
            job = job_snapshot.to_dict()
            page = page_snapshot.to_dict()
            if page.get("status") in TERMINAL_PAGE_STATUSES:
                finished = int(job.get("pages_completed", 0)) + int(job.get("pages_failed", 0))
                return finished >= int(job.get("pages_total", 0))

            page_update = {
                "status": status,
                "updated_at": _now(),
                "issue_count": issue_count,
            }
            if result_object:
                page_update["result_object"] = result_object
            if error:
                page_update["error"] = error[:1_000]
            transaction.update(page_ref, page_update)

            completed = int(job.get("pages_completed", 0)) + (1 if status == "complete" else 0)
            failed = int(job.get("pages_failed", 0)) + (1 if status == "failed" else 0)
            job_update = {
                "pages_completed": completed,
                "pages_failed": failed,
                "updated_at": _now(),
            }
            ready = completed + failed >= int(job.get("pages_total", 0))
            if ready:
                job_update["status"] = "finalizing"
            transaction.update(job_ref, job_update)
            return ready

        return bool(update(transaction))

    def run_page(
        self,
        *,
        job_id: str,
        page_id: str,
        retry_count: int,
        audit_callable: Callable[[str, str, str], dict],
    ) -> dict:
        """Fetch and audit one selected page, retrying transient failures."""
        job_ref = self._job_ref(job_id)
        job = self.get_job(job_id)
        page_ref = job_ref.collection("pages").document(page_id)
        page_snapshot = page_ref.get()
        if not job or not page_snapshot.exists:
            raise ValueError("Unknown job or page")
        page = page_snapshot.to_dict()
        if page.get("status") in TERMINAL_PAGE_STATUSES:
            return {"status": page["status"], "duplicate": True}
        page_ref.update({"status": "processing", "updated_at": _now()})

        try:
            html_content, final_url = fetch_public_html(page["url"])
            result = audit_callable(html_content, self.api_key, self.model)
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "Page audit failed")
            csv_text = result.get("csv_report") or ""
            issue_count = max(0, len(csv_text.splitlines()) - 1) if csv_text else int(
                result.get("summary", {}).get("programmatic_count", 0)
            )
            stored = {
                "page_url": page["url"],
                "final_url": final_url,
                "csv_report": csv_text,
                "summary": result.get("summary", {}),
            }
            object_name = self._store_page_result(job_id, page_id, stored)
            ready = self._mark_page_terminal(
                job_id=job_id,
                page_id=page_id,
                status="complete",
                result_object=object_name,
                issue_count=issue_count,
            )
        except Exception as exc:
            if retry_count < 2:
                page_ref.update(
                    {"status": "queued", "last_error": str(exc)[:1_000], "updated_at": _now()}
                )
                raise
            ready = self._mark_page_terminal(
                job_id=job_id,
                page_id=page_id,
                status="failed",
                error=str(exc),
            )

        if ready:
            self.dispatcher.enqueue(
                "/api/internal/site-audits/finalize",
                {"job_id": job_id},
                f"{job_id}-finalize",
            )
        return {"status": "complete" if page_ref.get().to_dict().get("status") == "complete" else "failed"}

    def finalize(self, job_id: str) -> dict:
        """Build, store, and email the consolidated whole-site report."""
        job_ref = self._job_ref(job_id)
        job = self.get_job(job_id)
        if not job:
            raise ValueError("Unknown job")
        if job.get("status") == "complete" and job.get("email_sent"):
            return self.public_job(job)

        page_snapshots = sorted(
            job_ref.collection("pages").stream(),
            key=lambda item: int(item.to_dict().get("index", 0)),
        )
        pages: list[dict] = []
        page_results: list[dict] = []
        for snapshot in page_snapshots:
            page = snapshot.to_dict()
            pages.append(
                {
                    "url": page.get("url", ""),
                    "title": page.get("title", ""),
                    "status": page.get("status", "unknown"),
                    "error": page.get("error", ""),
                }
            )
            if page.get("result_object"):
                page_results.append(self._load_page_result(page["result_object"]))

        report = build_site_report(
            base_url=job["base_url"],
            model=job["model"],
            capped=bool(job.get("capped")),
            candidate_count=int(job.get("candidate_count", len(pages))),
            pages=pages,
            page_results=page_results,
        )
        report_prefix = f"jobs/{job_id}/report"
        objects = {
            "zip": f"{report_prefix}.zip",
            "html": f"{report_prefix}.html",
            "csv": f"{report_prefix}.csv",
        }
        self.bucket.blob(objects["zip"]).upload_from_string(
            report.zip_bytes, content_type="application/zip"
        )
        self.bucket.blob(objects["html"]).upload_from_string(
            report.html_bytes, content_type="text/html; charset=utf-8"
        )
        self.bucket.blob(objects["csv"]).upload_from_string(
            report.csv_bytes, content_type="text/csv; charset=utf-8"
        )
        download_url = f"{self.service_url}/api/site-audits/{job_id}/report"
        try:
            send_report_email(
                recipient=job["email"],
                base_url=job["base_url"],
                pages=len(pages),
                findings=report.total_findings,
                download_url=download_url,
                report_zip=report.zip_bytes,
            )
        except Exception as exc:
            job_ref.update(
                {"status": "finalizing", "last_error": f"Email delivery failed: {exc}"[:1_000], "updated_at": _now()}
            )
            raise

        job_ref.update(
            {
                "status": "complete",
                "report_ready": True,
                "report_objects": objects,
                "total_findings": report.total_findings,
                "email_sent": True,
                "completed_at": _now(),
                "updated_at": _now(),
                "last_error": firestore.DELETE_FIELD,
            }
        )
        return self.public_job(self.get_job(job_id))

    def report_bytes(self, job_id: str) -> bytes | None:
        job = self.get_job(job_id)
        if not job or not job.get("report_ready"):
            return None
        object_name = (job.get("report_objects") or {}).get("zip")
        if not object_name:
            return None
        return self.bucket.blob(object_name).download_as_bytes()


_COORDINATOR: SiteAuditCoordinator | None = None


def get_coordinator() -> SiteAuditCoordinator:
    """Return the process-wide lazy coordinator."""
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = SiteAuditCoordinator()
    return _COORDINATOR
