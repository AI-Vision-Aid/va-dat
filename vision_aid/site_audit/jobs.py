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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet, InvalidToken
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore, storage, tasks_v2
from google.cloud.firestore_v1.base_query import FieldFilter
from google.protobuf import duration_pb2

from .crawler import discover_site_urls, fetch_public_html, validate_public_url
from .report import build_site_report


ACTIVE_STATUSES = {"queued", "discovering", "auditing", "finalizing"}
TERMINAL_PAGE_STATUSES = {"complete", "failed"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,99}$")


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


def _model_provider(model: str) -> str:
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith("claude-"):
        return "anthropic"
    raise ValueError("Select a supported OpenAI, Anthropic, or Gemini model")


def validate_model_name(model: str) -> str:
    normalized = str(model or "").strip()
    if not MODEL_PATTERN.fullmatch(normalized):
        raise ValueError("Select a valid model")
    _model_provider(normalized)
    return normalized


def verify_model_key(api_key: str, model: str) -> tuple[bool, str]:
    """Verify a provider credential without exposing it or generating tokens."""
    key = str(api_key or "").strip()
    if not key:
        return False, "No API key is configured"
    try:
        provider = _model_provider(validate_model_name(model))
        if provider == "openai":
            request = urllib.request.Request(
                f"https://api.openai.com/v1/models/{quote(model, safe='')}",
                headers={"Authorization": f"Bearer {key}"},
            )
        elif provider == "gemini":
            request = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={quote(key, safe='')}",
            )
        else:
            request = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
            )
        with urllib.request.urlopen(request, timeout=15):
            return True, "Verified"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "Invalid or unauthorized API key"
        if exc.code == 404 and provider == "openai":
            return False, "The selected model is not available to this API key"
        return False, f"Provider returned HTTP {exc.code}"
    except ValueError as exc:
        return False, str(exc)
    except Exception:
        return False, "Could not verify the API key with the provider"


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
        self.credential_encryption_key = os.getenv(
            "DAT_CREDENTIAL_ENCRYPTION_KEY", ""
        ).strip()
        self.allowed_domains = {
            item.strip().lower()
            for item in os.getenv("DAT_ALLOWED_EMAIL_DOMAINS", "").split(",")
            if item.strip()
        }
        self.collection = os.getenv("DAT_JOB_COLLECTION", "dat_site_audit_jobs")
        self._db = None
        self._storage = None
        self._dispatcher = None
        self._credential_cipher = None
        self._key_verification_cache: dict[str, tuple[datetime, bool, str]] = {}

    @property
    def configured(self) -> bool:
        return bool(
            self.project
            and self.service_url
            and self.bucket_name
            and self.job_token
            and self.api_key
            and self.credential_encryption_key
        )

    @property
    def credential_cipher(self) -> Fernet:
        if self._credential_cipher is None:
            if not self.credential_encryption_key:
                raise RuntimeError("Credential override encryption is not configured")
            try:
                self._credential_cipher = Fernet(
                    self.credential_encryption_key.encode("ascii")
                )
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Credential override encryption is invalid") from exc
        return self._credential_cipher

    def _verify_saved_key(self, model: str, *, refresh: bool = False) -> tuple[bool, str]:
        selected_model = validate_model_name(model)
        if _model_provider(selected_model) != "openai":
            return False, "The saved credential is an OpenAI API key"
        cached = self._key_verification_cache.get(selected_model)
        if cached and not refresh and _now() - cached[0] < timedelta(minutes=5):
            return cached[1], cached[2]
        valid, message = verify_model_key(self.api_key, selected_model)
        self._key_verification_cache[selected_model] = (_now(), valid, message)
        return valid, message

    def public_config(self, *, model: str | None = None, refresh: bool = False) -> dict:
        """Return non-secret model and saved-key state for the signed-in UI."""
        selected_model = validate_model_name(model or self.model)
        saved_available = bool(self.api_key and _model_provider(selected_model) == "openai")
        verified, message = (
            self._verify_saved_key(selected_model, refresh=refresh)
            if saved_available
            else (False, "Enter an API key for the selected provider")
        )
        return {
            "model": self.model,
            "selected_model": selected_model,
            "provider": _model_provider(selected_model),
            "api_key_configured": saved_available,
            "api_key_masked": "••••••••••••••••" if saved_available else "",
            "api_key_verified": verified,
            "verification_message": message,
        }

    def verify_requested_key(
        self,
        *,
        model: str,
        api_key: str = "",
        use_saved: bool = False,
        refresh: bool = False,
    ) -> tuple[bool, str]:
        selected_model = validate_model_name(model)
        if use_saved:
            return self._verify_saved_key(selected_model, refresh=refresh)
        return verify_model_key(api_key, selected_model)

    def _encrypt_credential(self, api_key: str) -> str:
        return self.credential_cipher.encrypt(api_key.encode("utf-8")).decode("ascii")

    def _job_api_key(self, job: dict) -> str:
        encrypted = str(job.get("credential_override", ""))
        if encrypted:
            try:
                return self.credential_cipher.decrypt(
                    encrypted.encode("ascii"), ttl=31 * 24 * 60 * 60
                ).decode("utf-8")
            except (InvalidToken, ValueError, TypeError) as exc:
                raise RuntimeError("The job credential override is unavailable") from exc
        if _model_provider(str(job.get("model", self.model))) == "openai":
            return self.api_key
        raise RuntimeError("No API key is available for the selected model")

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

    def create_job(
        self,
        *,
        base_url: str,
        email: str,
        model: str = "",
        api_key: str = "",
    ) -> dict:
        """Persist and enqueue a new whole-site audit job."""
        if not self.configured:
            raise RuntimeError("Asynchronous site auditing is not configured")
        normalized_url = validate_public_url(str(base_url or ""))
        normalized_email = validate_request_email(email, self.allowed_domains)
        selected_model = validate_model_name(model or self.model)
        override_key = str(api_key or "").strip()
        if override_key:
            valid, message = verify_model_key(override_key, selected_model)
            if not valid:
                raise ValueError(message)
            encrypted_override = self._encrypt_credential(override_key)
            credential_source = "override"
        else:
            if _model_provider(selected_model) != "openai":
                raise ValueError("Enter an API key when selecting a non-OpenAI model")
            valid, message = self._verify_saved_key(selected_model)
            if not valid:
                raise ValueError(message)
            encrypted_override = ""
            credential_source = "saved"
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
            "model": selected_model,
            "credential_source": credential_source,
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
        if encrypted_override:
            job["credential_override"] = encrypted_override
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
                api_key=self._job_api_key(job),
                model=job["model"],
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
            result = audit_callable(
                html_content,
                self._job_api_key(job),
                job["model"],
            )
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
        if job.get("credential_override"):
            job_ref.update(
                {
                    "credential_override": firestore.DELETE_FIELD,
                    "credential_cleared_at": _now(),
                }
            )
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
