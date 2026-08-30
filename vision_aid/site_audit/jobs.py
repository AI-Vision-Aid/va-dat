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
from email.utils import format_datetime, make_msgid
from typing import Callable
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet, InvalidToken
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore, storage, tasks_v2
from google.cloud.firestore_v1.base_query import FieldFilter
from google.protobuf import duration_pb2

from .crawler import discover_site_urls, fetch_public_html, validate_public_url
from .monitor import EASTERN, build_daily_monitor_html, build_daily_monitor_report
from .report import build_site_report
from .url_list import safe_upload_name, validate_uploaded_urls


ACTIVE_STATUSES = {"queued", "discovering", "preparing", "auditing", "finalizing"}
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
    audit_mode: str = "crawl",
) -> dict:
    """Submit a completed report to SMTP and return its acceptance receipt."""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()
    self_delivery_fallback = os.getenv(
        "DAT_SMTP_SELF_DELIVERY_FALLBACK", ""
    ).strip()
    if not all((smtp_host, smtp_user, smtp_password, smtp_from)):
        raise RuntimeError("SMTP delivery is not configured")

    host = urlparse(base_url).hostname or base_url
    is_url_list = audit_mode == "url_list"
    message = EmailMessage()
    message["Subject"] = (
        f"DAT URL-list accessibility report: {pages} page(s)"
        if is_url_list
        else f"DAT whole-site accessibility report: {host}"
    )
    message["From"] = smtp_from
    message["To"] = recipient
    message["Date"] = format_datetime(_now())
    message_id = make_msgid(domain=(smtp_from.rsplit("@", 1)[-1] or None))
    message["Message-ID"] = message_id
    envelope_recipients = [recipient]
    self_delivery_fallback_used = (
        bool(self_delivery_fallback)
        and recipient.strip().casefold() == smtp_from.casefold()
        and self_delivery_fallback.casefold() != recipient.strip().casefold()
    )
    if self_delivery_fallback_used:
        message["Cc"] = self_delivery_fallback
        envelope_recipients.append(self_delivery_fallback)
    attach_report = os.getenv("DAT_EMAIL_ATTACH_REPORT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    message.set_content(
        (
            "Your Vision Aid DAT uploaded URL-list accessibility report is ready.\n\n"
            if is_url_list
            else "Your Vision Aid DAT whole-site accessibility report is ready.\n\n"
        )
        + (
            f"First URL: {base_url}\n"
            if is_url_list
            else f"Site: {base_url}\n"
        )
        + f"Pages processed: {pages}\nFindings: {findings}\n"
        f"Download: {download_url}\n\n"
        "For reliable delivery, the report is provided through the secure download "
        "link instead of as an email attachment. Sign in with the DAT testing "
        "password if prompted."
    )
    if attach_report and len(report_zip) <= 18 * 1024 * 1024:
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
        refused = smtp.send_message(
            message,
            from_addr=smtp_from,
            to_addrs=envelope_recipients,
        )
    if refused:
        refused_recipients = ", ".join(str(item) for item in refused)
        raise RuntimeError(f"Mail server refused: {refused_recipients}")
    accepted_at = _now()
    return {
        "message_id": message_id,
        "accepted_at": accepted_at,
        "recipient": recipient,
        "smtp_host": smtp_host,
        "attachment_included": attach_report and len(report_zip) <= 18 * 1024 * 1024,
        "self_delivery_fallback_used": self_delivery_fallback_used,
        "accepted_recipient_count": len(envelope_recipients),
    }


def send_daily_monitor_email(*, recipient: str, report: dict) -> dict:
    """Submit the daily DAT usage and health report to SMTP."""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()
    self_delivery_fallback = os.getenv(
        "DAT_SMTP_SELF_DELIVERY_FALLBACK", ""
    ).strip()
    if not all((smtp_host, smtp_user, smtp_password, smtp_from)):
        raise RuntimeError("SMTP delivery is not configured")

    message = EmailMessage()
    message["Subject"] = str(report["subject"])
    message["From"] = smtp_from
    message["To"] = recipient
    message["Date"] = format_datetime(_now())
    message_id = make_msgid(domain=(smtp_from.rsplit("@", 1)[-1] or None))
    message["Message-ID"] = message_id
    message.set_content(str(report["text"]))
    envelope_recipients = [recipient]
    self_delivery_fallback_used = (
        bool(self_delivery_fallback)
        and recipient.strip().casefold() == smtp_from.casefold()
        and self_delivery_fallback.casefold() != recipient.strip().casefold()
    )
    if self_delivery_fallback_used:
        message["Cc"] = self_delivery_fallback
        envelope_recipients.append(self_delivery_fallback)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        refused = smtp.send_message(
            message,
            from_addr=smtp_from,
            to_addrs=envelope_recipients,
        )
    if refused:
        refused_recipients = ", ".join(str(item) for item in refused)
        raise RuntimeError(f"Mail server refused: {refused_recipients}")
    return {
        "message_id": message_id,
        "accepted_at": _now(),
        "self_delivery_fallback_used": self_delivery_fallback_used,
        "accepted_recipient_count": len(envelope_recipients),
    }


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
        self.usage_collection = os.getenv(
            "DAT_USAGE_COLLECTION", "dat_audit_usage_events"
        )
        self.monitor_collection = os.getenv(
            "DAT_MONITOR_COLLECTION", "dat_site_audit_monitor_runs"
        )
        self.monitor_email = os.getenv(
            "DAT_DAILY_MONITOR_EMAIL", "abilitybazaar@visionaid.org"
        ).strip().lower()
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

    def public_config(
        self,
        *,
        model: str | None = None,
        refresh: bool = False,
        allow_saved_key: bool = False,
    ) -> dict:
        """Return model state while exposing saved-key metadata only to admins."""
        selected_model = validate_model_name(model or self.model)
        saved_available = bool(
            allow_saved_key
            and self.api_key
            and _model_provider(selected_model) == "openai"
        )
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
            "admin_authenticated": bool(allow_saved_key),
        }

    def verify_requested_key(
        self,
        *,
        model: str,
        api_key: str = "",
        use_saved: bool = False,
        refresh: bool = False,
        allow_saved_key: bool = False,
    ) -> tuple[bool, str]:
        selected_model = validate_model_name(model)
        if use_saved:
            if not allow_saved_key:
                raise ValueError("Admin sign-in is required to use the saved API key")
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

    def _monitor_ref(self, report_date: str):
        return self.db.collection(self.monitor_collection).document(report_date)

    def record_usage_event(
        self,
        *,
        audit_mode: str,
        model: str,
        result: dict,
        base_url: str = "",
        pages_total: int = 1,
        pages_completed: int | None = None,
        pages_failed: int | None = None,
    ) -> None:
        """Persist non-sensitive usage totals for synchronous audit modes."""
        if not self.project:
            return
        success = bool(result.get("success"))
        total = max(0, int(pages_total))
        completed = (
            max(0, int(pages_completed))
            if pages_completed is not None
            else (total if success else 0)
        )
        failed = (
            max(0, int(pages_failed))
            if pages_failed is not None
            else (0 if success else total)
        )
        summary = result.get("summary") or {}
        parsed = urlparse(str(base_url or ""))
        host = (parsed.hostname or "").lower()
        origin = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""
        event_id = secrets.token_urlsafe(18)
        now = _now()
        self.db.collection(self.usage_collection).document(event_id).set(
            {
                "event_id": event_id,
                "audit_mode": str(audit_mode),
                "base_url": origin,
                "site_hosts": [host] if host else [],
                "status": "complete" if success else "failed",
                "model": str(model or "unknown"),
                "pages_total": total,
                "pages_completed": completed,
                "pages_failed": failed,
                "total_input_tokens": max(
                    0, int(summary.get("total_input_tokens") or 0)
                ),
                "total_output_tokens": max(
                    0, int(summary.get("total_output_tokens") or 0)
                ),
                "estimated_cost_usd": summary.get("estimated_cost_usd"),
                "created_at": now,
                "updated_at": now,
                "expires_at": now + timedelta(days=30),
            }
        )

    def internal_token_valid(self, supplied: str) -> bool:
        return bool(self.job_token and hmac.compare_digest(self.job_token, supplied or ""))

    def create_job(
        self,
        *,
        base_url: str = "",
        email: str,
        model: str = "",
        api_key: str = "",
        audit_mode: str = "crawl",
        uploaded_urls: list[str] | None = None,
        source_file_name: str = "",
        allow_saved_key: bool = False,
    ) -> dict:
        """Persist and enqueue a new crawl or uploaded URL-list audit job."""
        if not self.configured:
            raise RuntimeError("Asynchronous site auditing is not configured")
        normalized_mode = str(audit_mode or "crawl").strip().lower()
        if normalized_mode not in {"crawl", "url_list"}:
            raise ValueError("Select full-site crawl or uploaded URL-list mode")
        normalized_file_name = ""
        normalized_uploaded_urls: list[str] = []
        if normalized_mode == "url_list":
            normalized_uploaded_urls = validate_uploaded_urls(uploaded_urls or [])
            normalized_url = normalized_uploaded_urls[0]
            normalized_file_name = safe_upload_name(source_file_name)
        else:
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
            if not allow_saved_key:
                raise ValueError(
                    "Enter an API key or sign in as an administrator to use the saved key"
                )
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
            "audit_mode": normalized_mode,
            "email": normalized_email,
            "email_hash": email_hash,
            "status": "queued",
            "model": selected_model,
            "credential_source": credential_source,
            "max_pages": 200,
            "pages_total": 0,
            "pages_completed": 0,
            "pages_failed": 0,
            "candidate_count": len(normalized_uploaded_urls),
            "capped": False,
            "created_at": _now(),
            "updated_at": _now(),
            "expires_at": _now() + timedelta(days=30),
            "site_hosts": sorted(
                {
                    urlparse(item).hostname.lower()
                    for item in (normalized_uploaded_urls or [normalized_url])
                    if urlparse(item).hostname
                }
            ),
        }
        if normalized_mode == "url_list":
            job["provided_urls"] = normalized_uploaded_urls
            job["source_file_name"] = normalized_file_name
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
                "audit_mode",
                "source_file_name",
                "status",
                "model",
                "max_pages",
                "pages_total",
                "pages_completed",
                "pages_failed",
                "candidate_count",
                "discovery_sources",
                "capped",
                "total_findings",
                "total_input_tokens",
                "total_output_tokens",
                "total_tokens",
                "estimated_cost_usd",
                "report_ready",
                "email_sent",
                "email_delivery_status",
                "email_accepted_at",
                "last_error",
                "created_at",
                "audit_started_at",
                "completed_at",
            )
            if key in job
        }
        total = int(job.get("pages_total") or 0)
        finished = int(job.get("pages_completed") or 0) + int(job.get("pages_failed") or 0)
        remaining = max(0, total - finished)
        status = str(job.get("status", ""))
        if status == "complete":
            progress = 100
        elif status == "finalizing":
            progress = 98
        elif total:
            progress = min(97, round((finished / total) * 97))
        elif status in {"discovering", "preparing"}:
            progress = 2
        else:
            progress = 0
        result["progress_percent"] = progress
        result["pages_remaining"] = remaining

        started_at = job.get("audit_started_at") or job.get("created_at")
        ended_at = job.get("completed_at") if status == "complete" else _now()
        if isinstance(started_at, datetime) and isinstance(ended_at, datetime):
            elapsed = max(0, int((ended_at - started_at).total_seconds()))
            result["elapsed_seconds"] = elapsed
            if status == "auditing" and finished > 0 and remaining > 0:
                result["estimated_seconds_remaining"] = max(
                    1, round((elapsed / finished) * remaining)
                )
            elif status in {"finalizing", "complete"}:
                result["estimated_seconds_remaining"] = 0
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
            audit_mode = str(job.get("audit_mode") or "crawl")
            job_ref.update(
                {
                    "status": "preparing" if audit_mode == "url_list" else "discovering",
                    "discovery_started_at": _now(),
                    "updated_at": _now(),
                }
            )
            if audit_mode == "url_list":
                uploaded_urls = list(job.get("provided_urls") or [])
                if not uploaded_urls:
                    raise ValueError("The uploaded URL list is unavailable")
                page_specs = [
                    {"url": url, "title": "", "source": "upload"}
                    for url in uploaded_urls
                ]
                discovery_metadata = {
                    "pages_total": len(page_specs),
                    "candidate_count": len(page_specs),
                    "capped": False,
                    "ai_discovery_used": False,
                    "ai_web_discovery_used": False,
                    "sitemap_count": 0,
                    "discovery_sources": {"upload": len(page_specs)},
                    "provided_urls": firestore.DELETE_FIELD,
                }
            else:
                discovery = discover_site_urls(
                    job["base_url"],
                    api_key=self._job_api_key(job),
                    model=job["model"],
                    max_pages=200,
                )
                page_specs = [
                    {"url": page.url, "title": page.title, "source": page.source}
                    for page in discovery.pages
                ]
                discovery_metadata = {
                    "pages_total": len(page_specs),
                    "candidate_count": discovery.candidate_count,
                    "capped": discovery.capped,
                    "ai_discovery_used": discovery.ai_used,
                    "ai_web_discovery_used": discovery.ai_web_used,
                    "sitemap_count": discovery.sitemap_count,
                    "discovery_sources": discovery.source_counts,
                }
            batch = self.db.batch()
            for index, page in enumerate(page_specs):
                page_ref = job_ref.collection("pages").document(f"{index:03d}")
                batch.set(
                    page_ref,
                    {
                        "index": index,
                        "url": page["url"],
                        "title": page["title"],
                        "source": page["source"],
                        "status": "queued",
                        "updated_at": _now(),
                    },
                )
            batch.commit()
            job_ref.update({**discovery_metadata, "updated_at": _now()})
            existing_pages = list(job_ref.collection("pages").stream())

        for snapshot in existing_pages:
            page = snapshot.to_dict()
            index = int(page["index"])
            self.dispatcher.enqueue(
                "/api/internal/site-audits/page",
                {"job_id": job_id, "page_id": snapshot.id},
                f"{job_id}-page-{index:03d}",
            )
        refreshed_job = self.get_job(job_id) or {}
        audit_update = {"status": "auditing", "updated_at": _now()}
        if not refreshed_job.get("audit_started_at"):
            audit_update["audit_started_at"] = _now()
        job_ref.update(audit_update)
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
                "last_page_completed_at": _now(),
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
            audit_mode=str(job.get("audit_mode") or "crawl"),
            source_file_name=str(job.get("source_file_name") or ""),
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
            delivery = send_report_email(
                recipient=job["email"],
                base_url=job["base_url"],
                pages=len(pages),
                findings=report.total_findings,
                download_url=download_url,
                report_zip=report.zip_bytes,
                audit_mode=str(job.get("audit_mode") or "crawl"),
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
                "total_input_tokens": report.total_input_tokens,
                "total_output_tokens": report.total_output_tokens,
                "total_tokens": report.total_tokens,
                "estimated_cost_usd": report.estimated_cost_usd,
                "email_sent": True,
                "email_delivery_status": "accepted",
                "email_message_id": delivery["message_id"],
                "email_accepted_at": delivery["accepted_at"],
                "email_attachment_included": delivery["attachment_included"],
                "email_self_delivery_fallback_used": delivery[
                    "self_delivery_fallback_used"
                ],
                "email_accepted_recipient_count": delivery[
                    "accepted_recipient_count"
                ],
                "completed_at": _now(),
                "updated_at": _now(),
                "last_error": firestore.DELETE_FIELD,
            }
        )
        return self.public_job(self.get_job(job_id))

    def resend_report(self, job_id: str) -> dict:
        """Resubmit an existing completed report without rerunning the audit."""
        job_ref = self._job_ref(job_id)
        job = self.get_job(job_id)
        if not job or not job.get("report_ready"):
            raise ValueError("The report is not ready to resend")
        report_zip = self.report_bytes(job_id)
        if report_zip is None:
            raise ValueError("The report file is unavailable")
        delivery = send_report_email(
            recipient=job["email"],
            base_url=job["base_url"],
            pages=int(job.get("pages_total", 0)),
            findings=int(job.get("total_findings", 0)),
            download_url=f"{self.service_url}/api/site-audits/{job_id}/report",
            report_zip=report_zip,
            audit_mode=str(job.get("audit_mode") or "crawl"),
        )
        job_ref.update(
            {
                "email_sent": True,
                "email_delivery_status": "accepted",
                "email_message_id": delivery["message_id"],
                "email_accepted_at": delivery["accepted_at"],
                "email_attachment_included": delivery["attachment_included"],
                "email_self_delivery_fallback_used": delivery[
                    "self_delivery_fallback_used"
                ],
                "email_accepted_recipient_count": delivery[
                    "accepted_recipient_count"
                ],
                "email_resend_count": firestore.Increment(1),
                "updated_at": _now(),
                "last_error": firestore.DELETE_FIELD,
            }
        )
        return self.public_job(self.get_job(job_id))

    @staticmethod
    def _parse_monitor_time(schedule_time: str) -> datetime:
        value = str(schedule_time or "").strip()
        if not value:
            return _now()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid scheduler time") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def collect_daily_monitor_report(self, *, window_end: datetime) -> dict:
        """Collect the previous 24 hours of usage and run operational checks."""
        window_end = window_end.astimezone(timezone.utc)
        window_start = window_end - timedelta(hours=24)
        checks = {
            "service_endpoint": False,
            "core_configuration": self.configured,
            "firestore": False,
            "report_storage": False,
            "saved_model_key": False,
            "email_configuration": all(
                os.getenv(name, "").strip()
                for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")
            )
            and bool(self.monitor_email),
        }
        issues: list[str] = []
        jobs: list[dict] = []

        if not checks["core_configuration"]:
            issues.append("One or more required background-audit settings are missing.")
        if not checks["email_configuration"]:
            issues.append("The daily report email configuration is incomplete.")

        try:
            request = urllib.request.Request(
                f"{self.service_url}/api/health",
                headers={"User-Agent": "Vision-Aid-DAT-Daily-Monitor/1.0"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                health = json.loads(response.read().decode("utf-8"))
            checks["service_endpoint"] = (
                health.get("status") == "ok"
                and health.get("service") == "vision-aid-dat"
            )
        except Exception as exc:
            issues.append(f"The service health endpoint check failed ({type(exc).__name__}).")
        if not checks["service_endpoint"] and not any(
            item.startswith("The service health endpoint") for item in issues
        ):
            issues.append("The service health endpoint returned an unhealthy response.")

        try:
            snapshots = (
                self.db.collection(self.collection)
                .where(filter=FieldFilter("updated_at", ">=", window_start))
                .stream()
            )
            jobs = [snapshot.to_dict() for snapshot in snapshots]
            usage_snapshots = (
                self.db.collection(self.usage_collection)
                .where(filter=FieldFilter("updated_at", ">=", window_start))
                .stream()
            )
            jobs.extend(snapshot.to_dict() for snapshot in usage_snapshots)
            checks["firestore"] = True
        except Exception as exc:
            issues.append(f"The audit job database check failed ({type(exc).__name__}).")

        try:
            # The runtime intentionally has object access without bucket-metadata
            # administration. Listing at most one object verifies the permission
            # the audit actually needs without producing a health-check object.
            next(iter(self.bucket.list_blobs(max_results=1)), None)
            checks["report_storage"] = True
        except Exception as exc:
            issues.append(f"The report storage check failed ({type(exc).__name__}).")
        if not checks["report_storage"] and not any(
            item.startswith("The report storage check") for item in issues
        ):
            issues.append("The configured report storage bucket was not found.")

        try:
            key_valid, key_message = self._verify_saved_key(self.model, refresh=True)
            checks["saved_model_key"] = key_valid
            if not key_valid:
                issues.append(f"The saved AI model credential is not ready: {key_message}.")
        except Exception as exc:
            issues.append(f"The saved AI model credential check failed ({type(exc).__name__}).")

        if checks["firestore"]:
            try:
                active_snapshots = (
                    self.db.collection(self.collection)
                    .where(filter=FieldFilter("status", "in", sorted(ACTIVE_STATUSES)))
                    .stream()
                )
                stale_cutoff = window_end - timedelta(hours=6)
                stale_jobs = []
                for snapshot in active_snapshots:
                    active_job = snapshot.to_dict()
                    updated_at = active_job.get("updated_at") or active_job.get("created_at")
                    if isinstance(updated_at, datetime):
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=timezone.utc)
                        if updated_at.astimezone(timezone.utc) < stale_cutoff:
                            stale_jobs.append(active_job)
                if stale_jobs:
                    issues.append(
                        f"{len(stale_jobs)} audit job(s) have made no progress for more than 6 hours."
                    )
            except Exception as exc:
                issues.append(f"The stale-job check failed ({type(exc).__name__}).")

            page_failures = sum(max(0, int(job.get("pages_failed") or 0)) for job in jobs)
            if page_failures:
                issues.append(
                    f"{page_failures} page(s) failed during audits requested in this reporting period."
                )
            enqueue_failures = sum(job.get("status") == "enqueue_failed" for job in jobs)
            if enqueue_failures:
                issues.append(
                    f"{enqueue_failures} audit request(s) could not be added to the processing queue."
                )
            email_failures = sum(
                1
                for job in jobs
                if str(job.get("last_error") or "").startswith("Email delivery failed")
                or (
                    job.get("audit_mode") in {"crawl", "url_list"}
                    and job.get("status") == "complete"
                    and not job.get("email_sent")
                )
            )
            if email_failures:
                issues.append(
                    f"{email_failures} completed or finalizing audit(s) have an email delivery issue."
                )

        return build_daily_monitor_report(
            jobs=jobs,
            window_start=window_start,
            window_end=window_end,
            checks=checks,
            issues=issues,
        )

    def run_daily_monitor(
        self,
        *,
        schedule_time: str = "",
        send_email: bool = True,
        force: bool = False,
    ) -> dict:
        """Run, optionally email, and deduplicate one daily monitor report."""
        window_end = self._parse_monitor_time(schedule_time)
        report_date = window_end.astimezone(EASTERN).strftime("%Y-%m-%d")
        report = self.collect_daily_monitor_report(window_end=window_end)

        if send_email and report["checks"].get("firestore") and not force:
            existing = self._monitor_ref(report_date).get()
            existing_data = existing.to_dict() if existing.exists else {}
            if existing_data.get("status") == "sent":
                if not existing_data.get("report_object"):
                    report_object = f"monitor/daily/{report_date}.html"
                    self.bucket.blob(report_object).upload_from_string(
                        build_daily_monitor_html(report),
                        content_type="text/html; charset=utf-8",
                    )
                    self._monitor_ref(report_date).set(
                        {
                            "report_object": report_object,
                            "report_saved_at": _now(),
                            "updated_at": _now(),
                        },
                        merge=True,
                    )
                    self._prune_daily_monitor_reports()
                return {
                    "report_date": report_date,
                    "health_status": report["health_status"],
                    "audit_count": report["audit_count"],
                    "pages_processed": report["pages_processed"],
                    "estimated_cost_usd": report["estimated_cost_usd"],
                    "email_sent": True,
                    "duplicate": True,
                }

        monitor_ref = self._monitor_ref(report_date) if report["checks"].get("firestore") else None
        if send_email and monitor_ref:
            monitor_ref.set(
                {
                    "report_date": report_date,
                    "status": "sending",
                    "window_start": report["window_start"],
                    "window_end": report["window_end"],
                    "health_status": report["health_status"],
                    "audit_count": report["audit_count"],
                    "pages_processed": report["pages_processed"],
                    "estimated_cost_usd": report["estimated_cost_usd"],
                    "updated_at": _now(),
                },
                merge=True,
            )

        receipt = None
        if send_email:
            try:
                report_object = f"monitor/daily/{report_date}.html"
                self.bucket.blob(report_object).upload_from_string(
                    build_daily_monitor_html(report),
                    content_type="text/html; charset=utf-8",
                )
                if monitor_ref:
                    monitor_ref.set(
                        {
                            "report_object": report_object,
                            "report_saved_at": _now(),
                            "updated_at": _now(),
                        },
                        merge=True,
                    )
                self._prune_daily_monitor_reports()
                receipt = send_daily_monitor_email(
                    recipient=self.monitor_email,
                    report=report,
                )
            except Exception as exc:
                if monitor_ref:
                    monitor_ref.set(
                        {
                            "status": "failed",
                            "last_error": f"Email delivery failed ({type(exc).__name__})",
                            "updated_at": _now(),
                        },
                        merge=True,
                    )
                raise
            if monitor_ref:
                monitor_ref.set(
                    {
                        "status": "sent",
                        "email_accepted_at": receipt["accepted_at"],
                        "email_message_id": receipt["message_id"],
                        "email_self_delivery_fallback_used": receipt[
                            "self_delivery_fallback_used"
                        ],
                        "email_accepted_recipient_count": receipt[
                            "accepted_recipient_count"
                        ],
                        "updated_at": _now(),
                        "last_error": firestore.DELETE_FIELD,
                    },
                    merge=True,
                )

        return {
            "report_date": report_date,
            "health_status": report["health_status"],
            "health_label": report["health_label"],
            "issues": report["issues"],
            "audit_count": report["audit_count"],
            "pages_processed": report["pages_processed"],
            "pages_failed": report["pages_failed"],
            "estimated_cost_usd": report["estimated_cost_usd"],
            "email_sent": bool(receipt),
            "duplicate": False,
        }

    def _prune_daily_monitor_reports(self, *, keep: int = 30) -> None:
        """Delete daily web-report records and objects beyond the retention limit."""
        try:
            snapshots = list(
                self.db.collection(self.monitor_collection)
                .order_by("window_end", direction=firestore.Query.DESCENDING)
                .stream()
            )
            for snapshot in snapshots[max(1, int(keep)) :]:
                item = snapshot.to_dict()
                object_name = str(item.get("report_object") or "")
                if object_name.startswith("monitor/daily/") and object_name.endswith(
                    ".html"
                ):
                    self.bucket.blob(object_name).delete()
                snapshot.reference.delete()
        except Exception as exc:
            # Retention cleanup must not block the daily health email. A later
            # monitor run will retry the same bounded cleanup.
            print(f"Daily monitor retention cleanup failed ({type(exc).__name__})")

    def analytics_summary(self) -> dict:
        """Return privacy-safe cumulative usage metrics for administrators."""
        jobs = [
            snapshot.to_dict()
            for snapshot in self.db.collection(self.collection).stream()
        ]
        usage_events = [
            snapshot.to_dict()
            for snapshot in self.db.collection(self.usage_collection).stream()
        ]
        records = jobs + usage_events
        users = {str(item.get("email_hash")) for item in jobs if item.get("email_hash")}
        sites: set[str] = set()
        for item in records:
            hosts = [str(host).lower() for host in (item.get("site_hosts") or []) if host]
            if not hosts:
                legacy_host = urlparse(str(item.get("base_url") or "")).hostname
                if legacy_host:
                    hosts.append(legacy_host.lower())
            sites.update(hosts)
        completed_sync = sum(item.get("status") == "complete" for item in usage_events)
        completed_reports = sum(bool(item.get("report_ready")) for item in jobs)
        input_tokens = sum(
            max(0, int(item.get("total_input_tokens") or 0)) for item in records
        )
        output_tokens = sum(
            max(0, int(item.get("total_output_tokens") or 0)) for item in records
        )
        return {
            "users": len(users),
            "reports": completed_sync + completed_reports,
            "sites": len(sites),
            "pages_scanned": sum(
                max(0, int(item.get("pages_completed") or 0)) for item in records
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(
                sum(float(item.get("estimated_cost_usd") or 0) for item in records),
                6,
            ),
            "history_note": (
                "Users are distinct email requesters. Totals reflect records "
                "currently retained by the service; anonymous public users are not identified."
            ),
        }

    def list_daily_monitor_reports(self, *, limit: int = 30) -> list[dict]:
        """Return recent saved monitor-report metadata for the admin dashboard."""
        snapshots = (
            self.db.collection(self.monitor_collection)
            .order_by("window_end", direction=firestore.Query.DESCENDING)
            .limit(max(1, min(int(limit), 100)))
            .stream()
        )
        reports = []
        for snapshot in snapshots:
            item = snapshot.to_dict()
            report_date = str(item.get("report_date") or snapshot.id)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
                continue
            reports.append(
                {
                    "report_date": report_date,
                    "status": str(item.get("status") or "unknown"),
                    "health_status": str(item.get("health_status") or "unknown"),
                    "audit_count": max(0, int(item.get("audit_count") or 0)),
                    "pages_processed": max(0, int(item.get("pages_processed") or 0)),
                    "estimated_cost_usd": max(
                        0.0, float(item.get("estimated_cost_usd") or 0)
                    ),
                    "report_available": bool(item.get("report_object")),
                    "report_url": f"/analytics/reports/{report_date}"
                    if item.get("report_object")
                    else "",
                }
            )
        return reports

    def daily_monitor_report_bytes(self, report_date: str) -> bytes | None:
        """Load one private daily web report by its bounded calendar date."""
        normalized = str(report_date or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            raise ValueError("Invalid report date")
        snapshot = self._monitor_ref(normalized).get()
        if not snapshot.exists:
            return None
        object_name = str(snapshot.to_dict().get("report_object") or "")
        if not object_name.startswith("monitor/daily/") or not object_name.endswith(
            ".html"
        ):
            return None
        return self.bucket.blob(object_name).download_as_bytes()

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
