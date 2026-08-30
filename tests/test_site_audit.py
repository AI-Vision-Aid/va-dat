"""Tests for safe whole-site discovery and consolidated reporting."""

from __future__ import annotations

import io
import json
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from vision_aid.site_audit.crawler import (
    PageCandidate,
    _ai_order_candidates,
    _discover_wordpress_urls,
    _is_candidate_url,
    _looks_like_bot_challenge,
    _sitemap_locations,
    canonicalize_url,
    fetch_public_html,
    validate_public_url,
)
from vision_aid.site_audit.jobs import (
    SiteAuditCoordinator,
    send_daily_monitor_email,
    send_report_email,
    validate_request_email,
)
from vision_aid.site_audit.monitor import build_daily_monitor_report
from vision_aid.site_audit.report import build_site_report
from vision_aid.site_audit.url_list import (
    decode_uploaded_urls,
    extract_uploaded_urls,
)


class AuditModeUiTests(unittest.TestCase):
    def test_obsolete_nested_crawl_option_is_not_offered(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('value="url-nested"', html)
        self.assertNotIn("URL &#8212; With Crawl", html)
        self.assertIn('value="site" checked', html)
        self.assertIn("Full Site &#8212; Crawl and Email", html)


class CrawlerSafetyTests(unittest.TestCase):
    def test_canonicalize_removes_fragment_and_tracking(self):
        actual = canonicalize_url(
            "HTTPS://Example.com:443//about?utm_source=test&b=2&a=1#team"
        )
        self.assertEqual(actual, "https://example.com/about?a=1&b=2")

    @mock.patch("vision_aid.site_audit.crawler.socket.getaddrinfo")
    def test_private_addresses_are_rejected(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(ValueError, "Private"):
            validate_public_url("https://localhost/")

    @mock.patch("vision_aid.site_audit.crawler.socket.getaddrinfo")
    def test_public_addresses_are_accepted(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]
        self.assertEqual(
            validate_public_url("https://www.example.com/path#fragment"),
            "https://www.example.com/path",
        )

    def test_common_sitemap_indexes_are_tried_when_robots_is_blocked(self):
        with mock.patch(
            "vision_aid.site_audit.crawler._fetch_text_or_xml",
            side_effect=RuntimeError("blocked"),
        ):
            locations = _sitemap_locations(
                "https://example.com/", mock.Mock()
            )
        self.assertIn("https://example.com/sitemap.xml", locations)
        self.assertIn("https://example.com/sitemap_index.xml", locations)
        self.assertIn("https://example.com/wp-sitemap.xml", locations)

    def test_wordpress_rest_fallback_discovers_public_pages(self):
        def fetch(_session, url, **_kwargs):
            if url.endswith("/wp-json/wp/v2/types"):
                return json.dumps(
                    {"page": {"rest_base": "pages", "viewable": True}}
                ), url
            if "/wp-json/wp/v2/pages?" in url:
                return json.dumps(
                    [
                        {"link": "https://example.com/about/", "status": "publish"},
                        {"link": "https://example.com/contact/", "status": "publish"},
                    ]
                ), url
            raise AssertionError(url)

        with mock.patch(
            "vision_aid.site_audit.crawler._fetch_text_or_xml", side_effect=fetch
        ):
            urls = _discover_wordpress_urls("https://example.com/", mock.Mock())
        self.assertEqual(
            urls,
            ["https://example.com/about/", "https://example.com/contact/"],
        )

    def test_admin_and_login_actions_are_not_page_candidates(self):
        self.assertFalse(
            _is_candidate_url(
                "https://example.com/wp-admin/", "https://example.com/"
            )
        )
        self.assertFalse(
            _is_candidate_url(
                "https://example.com/wp-login.php?action=lostpassword",
                "https://example.com/",
            )
        )
        self.assertFalse(
            _is_candidate_url(
                "https://example.com/?elementor_snippet=header",
                "https://example.com/",
            )
        )

    def test_ai_order_cannot_drop_discovered_pages(self):
        candidates = [
            PageCandidate("https://example.com/"),
            PageCandidate("https://example.com/about/"),
            PageCandidate("https://example.com/contact/"),
        ]
        client = mock.Mock()
        client.responses.create.return_value = mock.Mock(output_text='{"order":[1]}')
        with mock.patch("openai.OpenAI", return_value=client):
            ordered = _ai_order_candidates(
                candidates,
                api_key="test-credential",
                model="gpt-5.6-sol",
                max_pages=200,
            )
        self.assertEqual(len(ordered), 3)
        self.assertEqual(ordered[0].url, "https://example.com/about/")
        self.assertEqual({item.url for item in ordered}, {item.url for item in candidates})

    def test_browser_fingerprint_fallback_is_used_when_standard_request_is_blocked(self):
        blocked = mock.Mock(status_code=403)
        blocked.close = mock.Mock()
        session = mock.Mock()
        session.get.return_value = blocked
        browser_response = mock.Mock(
            status_code=200,
            is_redirect=False,
            is_permanent_redirect=False,
            headers={"Content-Type": "text/html; charset=utf-8"},
            encoding="utf-8",
        )
        browser_response.iter_content.return_value = [b"<html>working</html>"]
        browser_response.raise_for_status.return_value = None
        with mock.patch(
            "curl_cffi.requests.get", return_value=browser_response
        ) as browser_get:
            html, final_url = fetch_public_html(
                "https://example.com/", session=session
            )
        self.assertEqual(html, "<html>working</html>")
        self.assertEqual(final_url, "https://example.com/")
        browser_get.assert_called_once()

    def test_siteground_soft_200_challenge_is_detected(self):
        self.assertTrue(
            _looks_like_bot_challenge(
                "<title>Robot Challenge Screen</title>Checking the site connection security",
                "https://example.com/",
            )
        )

    def test_soft_200_challenge_is_replaced_by_browser_content(self):
        challenge = mock.Mock(
            status_code=200,
            is_redirect=False,
            is_permanent_redirect=False,
            headers={"Content-Type": "text/html"},
            encoding="utf-8",
        )
        challenge.iter_content.return_value = [
            b"<title>Robot Challenge Screen</title>Checking the site connection security"
        ]
        challenge.raise_for_status.return_value = None
        session = mock.Mock()
        session.get.return_value = challenge
        with mock.patch(
            "vision_aid.site_audit.crawler._browser_fetch",
            return_value=(
                "<html lang='en'><title>Real page</title></html>",
                "https://example.com/",
                "text/html",
            ),
        ) as browser_fetch:
            html, _ = fetch_public_html("https://example.com/", session=session)
        self.assertIn("Real page", html)
        browser_fetch.assert_called_once()


class UrlListUploadTests(unittest.TestCase):
    def test_txt_extracts_deduplicates_and_canonicalizes_urls(self):
        urls = extract_uploaded_urls(
            "pages.txt",
            (
                "1. https://Example.com/about/?utm_source=test#team\n"
                "2. https://example.com/about/\n"
                "3. https://other.example.org/contact).\n"
            ).encode("utf-8"),
        )
        self.assertEqual(
            urls,
            [
                "https://example.com/about/",
                "https://other.example.org/contact",
            ],
        )

    def test_docx_extracts_visible_urls_and_hyperlink_targets(self):
        document_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>https://example.com/one</w:t></w:r></w:p></w:body>
        </w:document>"""
        relationships_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" TargetMode="External"
            Target="https://example.com/two?b=2&amp;a=1" />
        </Relationships>"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)
        urls = extract_uploaded_urls("pages.docx", buffer.getvalue())
        self.assertEqual(
            urls,
            [
                "https://example.com/one",
                "https://example.com/two?a=1&b=2",
            ],
        )

    def test_more_than_200_unique_urls_is_rejected(self):
        contents = "\n".join(
            f"https://example.com/page/{index}" for index in range(201)
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "more than 200"):
            extract_uploaded_urls("pages.txt", contents)

    def test_private_literal_and_unsupported_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Private"):
            extract_uploaded_urls("pages.txt", b"http://127.0.0.1/admin")
        with self.assertRaisesRegex(ValueError, "credentials"):
            extract_uploaded_urls("pages.txt", b"https://user:pass@example.com/admin")
        with self.assertRaisesRegex(ValueError, "txt or .docx"):
            extract_uploaded_urls("pages.pdf", b"https://example.com/")

    def test_base64_upload_decodes_without_exposing_file_contents(self):
        encoded = "aHR0cHM6Ly9leGFtcGxlLmNvbS8="
        name, urls = decode_uploaded_urls("folder\\pages.txt", encoded)
        self.assertEqual(name, "pages.txt")
        self.assertEqual(urls, ["https://example.com/"])


class EmailTests(unittest.TestCase):
    def test_allowed_domain(self):
        self.assertEqual(
            validate_request_email(" Ram@VisionAid.org ", {"visionaid.org"}),
            "ram@visionaid.org",
        )

    def test_disallowed_domain(self):
        with self.assertRaisesRegex(ValueError, "limited"):
            validate_request_email("person@example.com", {"visionaid.org"})

    def test_email_records_smtp_acceptance_and_omits_zip_by_default(self):
        smtp = mock.Mock()
        smtp.send_message.return_value = {}
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=smtp)
        smtp_context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USER": "sender@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM": "sender@example.com",
                "DAT_EMAIL_ATTACH_REPORT": "false",
                "DAT_SMTP_SELF_DELIVERY_FALLBACK": "",
            },
        ), mock.patch(
            "vision_aid.site_audit.jobs.smtplib.SMTP", return_value=smtp_context
        ):
            receipt = send_report_email(
                recipient="recipient@example.com",
                base_url="https://example.com/",
                pages=3,
                findings=7,
                download_url="https://dat.example.com/report",
                report_zip=b"zip",
            )
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertEqual(receipt["recipient"], "recipient@example.com")
        self.assertFalse(receipt["attachment_included"])
        self.assertTrue(receipt["message_id"].startswith("<"))
        self.assertFalse(receipt["self_delivery_fallback_used"])
        self.assertEqual(receipt["accepted_recipient_count"], 1)
        self.assertEqual(
            smtp.send_message.call_args.kwargs["to_addrs"],
            ["recipient@example.com"],
        )

    def test_self_delivery_adds_visible_fallback_copy(self):
        smtp = mock.Mock()
        smtp.send_message.return_value = {}
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=smtp)
        smtp_context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.office365.com",
                "SMTP_PORT": "587",
                "SMTP_USER": "abilitybazaar@visionaid.org",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM": "abilitybazaar@visionaid.org",
                "DAT_EMAIL_ATTACH_REPORT": "false",
                "DAT_SMTP_SELF_DELIVERY_FALLBACK": "ram@visionaid.org",
            },
        ), mock.patch(
            "vision_aid.site_audit.jobs.smtplib.SMTP", return_value=smtp_context
        ):
            receipt = send_report_email(
                recipient="abilitybazaar@visionaid.org",
                base_url="https://dat.visionaid.org/",
                pages=26,
                findings=290,
                download_url="https://dat.example.com/report",
                report_zip=b"zip",
            )
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "abilitybazaar@visionaid.org")
        self.assertEqual(message["Cc"], "ram@visionaid.org")
        self.assertEqual(
            smtp.send_message.call_args.kwargs["to_addrs"],
            ["abilitybazaar@visionaid.org", "ram@visionaid.org"],
        )
        self.assertTrue(receipt["self_delivery_fallback_used"])
        self.assertEqual(receipt["accepted_recipient_count"], 2)

    def test_email_raises_when_recipient_is_refused(self):
        smtp = mock.Mock()
        smtp.send_message.return_value = {"recipient@example.com": (550, b"refused")}
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=smtp)
        smtp_context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_USER": "sender@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM": "sender@example.com",
                "DAT_SMTP_SELF_DELIVERY_FALLBACK": "",
            },
        ), mock.patch(
            "vision_aid.site_audit.jobs.smtplib.SMTP", return_value=smtp_context
        ), self.assertRaisesRegex(RuntimeError, "refused"):
            send_report_email(
                recipient="recipient@example.com",
                base_url="https://example.com/",
                pages=1,
                findings=0,
                download_url="https://dat.example.com/report",
                report_zip=b"zip",
            )

    def test_daily_monitor_email_uses_requested_recipient_and_fallback(self):
        smtp = mock.Mock()
        smtp.send_message.return_value = {}
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=smtp)
        smtp_context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.office365.com",
                "SMTP_PORT": "587",
                "SMTP_USER": "abilitybazaar@visionaid.org",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM": "abilitybazaar@visionaid.org",
                "DAT_SMTP_SELF_DELIVERY_FALLBACK": "ram@visionaid.org",
            },
        ), mock.patch(
            "vision_aid.site_audit.jobs.smtplib.SMTP", return_value=smtp_context
        ):
            receipt = send_daily_monitor_email(
                recipient="abilitybazaar@visionaid.org",
                report={"subject": "DAT daily monitor", "text": "Working OK\n"},
            )
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "abilitybazaar@visionaid.org")
        self.assertEqual(message["Cc"], "ram@visionaid.org")
        self.assertIn("Working OK", message.get_content())
        self.assertEqual(receipt["accepted_recipient_count"], 2)


class CredentialTests(unittest.TestCase):
    def test_override_is_encrypted_and_decrypted_only_for_the_job(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with mock.patch.dict(
            os.environ,
            {
                "DAT_OPENAI_API_KEY": "saved-test-credential",
                "DAT_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
            },
        ):
            coordinator = SiteAuditCoordinator()
        ciphertext = coordinator._encrypt_credential("override-test-credential")
        self.assertNotIn("override-test-credential", ciphertext)
        self.assertEqual(
            coordinator._job_api_key(
                {
                    "model": "gpt-5.6-sol",
                    "credential_override": ciphertext,
                }
            ),
            "override-test-credential",
        )

    def test_public_config_masks_and_never_returns_saved_key(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with mock.patch.dict(
            os.environ,
            {
                "DAT_MODEL": "gpt-5.6-sol",
                "DAT_OPENAI_API_KEY": "saved-test-credential",
                "DAT_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
            },
        ):
            coordinator = SiteAuditCoordinator()
        with mock.patch(
            "vision_aid.site_audit.jobs.verify_model_key",
            return_value=(True, "Verified"),
        ):
            config = coordinator.public_config(refresh=True)
        self.assertTrue(config["api_key_verified"])
        self.assertEqual(config["api_key_masked"], "••••••••••••••••")
        self.assertNotIn("saved-test-credential", repr(config))


class JobCreationTests(unittest.TestCase):
    def test_url_list_job_queues_direct_batch_metadata(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with mock.patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "test-project",
                "DAT_SERVICE_URL": "https://dat.example.com",
                "DAT_REPORT_BUCKET": "test-bucket",
                "DAT_JOB_TOKEN": "test-job-token",
                "DAT_OPENAI_API_KEY": "saved-test-credential",
                "DAT_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
                "DAT_ALLOWED_EMAIL_DOMAINS": "visionaid.org",
                "DAT_MODEL": "gpt-5.6-sol",
            },
            clear=True,
        ):
            coordinator = SiteAuditCoordinator()
        database = mock.Mock()
        collection = database.collection.return_value
        collection.where.return_value.limit.return_value.stream.return_value = []
        job_ref = collection.document.return_value
        coordinator._db = database
        coordinator._dispatcher = mock.Mock()
        uploaded = ["https://example.com/one", "https://other.example.org/two"]
        with mock.patch.object(
            coordinator,
            "_verify_saved_key",
            return_value=(True, "Verified"),
        ), mock.patch(
            "vision_aid.site_audit.jobs.validate_uploaded_urls",
            return_value=uploaded,
        ):
            public = coordinator.create_job(
                email="tester@visionaid.org",
                model="gpt-5.6-sol",
                audit_mode="url_list",
                uploaded_urls=uploaded,
                source_file_name="pages.txt",
            )
        stored = job_ref.set.call_args.args[0]
        self.assertEqual(public["audit_mode"], "url_list")
        self.assertEqual(public["candidate_count"], 2)
        self.assertEqual(stored["provided_urls"], uploaded)
        self.assertEqual(stored["base_url"], uploaded[0])
        self.assertEqual(stored["source_file_name"], "pages.txt")
        self.assertEqual(
            coordinator._dispatcher.enqueue.call_args.args[0],
            "/api/internal/site-audits/discover",
        )


class ProgressTests(unittest.TestCase):
    def test_public_job_reports_percentage_elapsed_time_and_eta(self):
        now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
        coordinator = SiteAuditCoordinator()
        coordinator.service_url = "https://dat.example.com"
        job = {
            "job_id": "test-job",
            "status": "auditing",
            "pages_total": 10,
            "pages_completed": 4,
            "pages_failed": 0,
            "created_at": now - timedelta(minutes=4),
            "audit_started_at": now - timedelta(minutes=2),
        }
        with mock.patch("vision_aid.site_audit.jobs._now", return_value=now):
            public = coordinator.public_job(job)
        self.assertEqual(public["progress_percent"], 39)
        self.assertEqual(public["pages_remaining"], 6)
        self.assertEqual(public["elapsed_seconds"], 120)
        self.assertEqual(public["estimated_seconds_remaining"], 180)


class DailyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.window_end = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
        self.window_start = self.window_end - timedelta(hours=24)
        self.healthy_checks = {
            "service_endpoint": True,
            "core_configuration": True,
            "firestore": True,
            "report_storage": True,
            "saved_model_key": True,
            "email_configuration": True,
        }

    def test_daily_report_lists_sites_pages_cost_and_healthy_status(self):
        report = build_daily_monitor_report(
            jobs=[
                {
                    "base_url": "https://dat.visionaid.org/",
                    "site_hosts": ["dat.visionaid.org"],
                    "audit_mode": "crawl",
                    "status": "complete",
                    "model": "gpt-5.6-sol",
                    "pages_total": 26,
                    "pages_completed": 25,
                    "pages_failed": 1,
                    "estimated_cost_usd": 1.234567,
                    "created_at": self.window_start + timedelta(hours=2),
                },
                {
                    "base_url": "https://example.com/one",
                    "site_hosts": ["example.com", "other.example.org"],
                    "audit_mode": "url_list",
                    "status": "complete",
                    "model": "gpt-4.1",
                    "pages_total": 2,
                    "pages_completed": 2,
                    "pages_failed": 0,
                    "estimated_cost_usd": 0.1,
                    "created_at": self.window_start + timedelta(hours=3),
                },
            ],
            window_start=self.window_start,
            window_end=self.window_end,
            checks=self.healthy_checks,
            issues=[],
        )
        self.assertEqual(report["health_status"], "ok")
        self.assertEqual(report["audit_count"], 2)
        self.assertEqual(report["pages_processed"], 28)
        self.assertEqual(report["pages_failed"], 1)
        self.assertEqual(report["estimated_cost_usd"], 1.334567)
        self.assertIn("dat.visionaid.org", report["text"])
        self.assertIn("example.com, other.example.org", report["text"])
        self.assertIn("$1.234567", report["text"])

    def test_health_issue_is_warning_and_failed_check_is_error(self):
        warning = build_daily_monitor_report(
            jobs=[],
            window_start=self.window_start,
            window_end=self.window_end,
            checks=self.healthy_checks,
            issues=["One page failed."],
        )
        self.assertEqual(warning["health_status"], "warning")
        self.assertIn("WORKING WITH ISSUES", warning["subject"])

        failed_checks = dict(self.healthy_checks)
        failed_checks["report_storage"] = False
        error = build_daily_monitor_report(
            jobs=[],
            window_start=self.window_start,
            window_end=self.window_end,
            checks=failed_checks,
            issues=["Storage unavailable."],
        )
        self.assertEqual(error["health_status"], "error")
        self.assertIn("NOT WORKING", error["text"])

    def test_synchronous_usage_event_omits_content_credentials_and_url_path(self):
        coordinator = SiteAuditCoordinator()
        coordinator.project = "test-project"
        database = mock.Mock()
        coordinator._db = database
        coordinator.record_usage_event(
            audit_mode="single_url",
            model="gpt-4.1",
            base_url="https://example.com/private/path?token=do-not-store",
            result={
                "success": True,
                "api_key": "do-not-store",
                "csv_report": "private report contents",
                "summary": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 20,
                    "estimated_cost_usd": 0.0123,
                },
            },
        )
        stored = (
            database.collection.return_value.document.return_value.set.call_args.args[0]
        )
        self.assertEqual(stored["base_url"], "https://example.com/")
        self.assertEqual(stored["site_hosts"], ["example.com"])
        self.assertEqual(stored["pages_completed"], 1)
        self.assertEqual(stored["estimated_cost_usd"], 0.0123)
        self.assertNotIn("do-not-store", repr(stored))
        self.assertNotIn("private report contents", repr(stored))

    def test_uploaded_html_usage_is_identified_without_inventing_a_site(self):
        report = build_daily_monitor_report(
            jobs=[
                {
                    "audit_mode": "html_upload",
                    "status": "complete",
                    "model": "gpt-4.1",
                    "pages_total": 1,
                    "pages_completed": 1,
                    "estimated_cost_usd": 0.05,
                    "created_at": self.window_start + timedelta(hours=1),
                }
            ],
            window_start=self.window_start,
            window_end=self.window_end,
            checks=self.healthy_checks,
            issues=[],
        )
        self.assertIn("Uploaded HTML (site not supplied)", report["text"])
        self.assertIn("Mode: Uploaded HTML", report["text"])

    def test_live_checks_use_object_access_instead_of_bucket_metadata(self):
        coordinator = SiteAuditCoordinator()
        coordinator.project = "test-project"
        coordinator.service_url = "https://dat.example.com"
        coordinator.bucket_name = "test-bucket"
        coordinator.job_token = "job-token"
        coordinator.api_key = "saved-key"
        coordinator.credential_encryption_key = Fernet.generate_key().decode("ascii")
        database = mock.Mock()
        database.collection.return_value.where.return_value.stream.return_value = []
        coordinator._db = database
        bucket = mock.Mock()
        bucket.list_blobs.return_value = []
        coordinator._storage = mock.Mock()
        coordinator._storage.bucket.return_value = bucket
        health_response = mock.Mock()
        health_response.read.return_value = json.dumps(
            {"status": "ok", "service": "vision-aid-dat"}
        ).encode("utf-8")
        health_context = mock.Mock()
        health_context.__enter__ = mock.Mock(return_value=health_response)
        health_context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_USER": "sender@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_FROM": "sender@example.com",
            },
        ), mock.patch(
            "vision_aid.site_audit.jobs.urllib.request.urlopen",
            return_value=health_context,
        ), mock.patch.object(
            coordinator, "_verify_saved_key", return_value=(True, "Verified")
        ):
            report = coordinator.collect_daily_monitor_report(
                window_end=self.window_end
            )
        self.assertTrue(report["checks"]["report_storage"])
        self.assertEqual(report["health_status"], "ok")
        bucket.list_blobs.assert_called_once_with(max_results=1)
        self.assertFalse(bucket.exists.called)

    def test_sent_report_is_not_emailed_twice_for_same_eastern_date(self):
        coordinator = SiteAuditCoordinator()
        existing = mock.Mock()
        existing.exists = True
        existing.to_dict.return_value = {"status": "sent"}
        monitor_ref = mock.Mock()
        monitor_ref.get.return_value = existing
        database = mock.Mock()
        database.collection.return_value.document.return_value = monitor_ref
        coordinator._db = database
        report = build_daily_monitor_report(
            jobs=[],
            window_start=self.window_start,
            window_end=self.window_end,
            checks=self.healthy_checks,
            issues=[],
        )
        with mock.patch.object(
            coordinator, "collect_daily_monitor_report", return_value=report
        ), mock.patch(
            "vision_aid.site_audit.jobs.send_daily_monitor_email"
        ) as send_email:
            result = coordinator.run_daily_monitor(
                schedule_time="2026-08-30T10:00:00Z"
            )
        self.assertTrue(result["duplicate"])
        send_email.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_report_zip_contains_cover_csv_and_summary(self):
        csv_text = (
            "ID,element_name,browser_combination,page_title,issue_title,steps_to_reproduce,"
            "actual_result,expected_result,recommendation,wcag_sc,category,impact,log_date,reported_by\n"
            "1,<img>,N/A,Home,Missing alt,Inspect image,No alt,Needs text,Add alt,1.1.1,"
            "Non-text Content,Serious,2026-08-29,gpt-5.6-sol\n"
        )
        report = build_site_report(
            base_url="https://www.abilitybazaar.com/",
            model="gpt-5.6-sol",
            capped=False,
            candidate_count=1,
            pages=[{"url": "https://www.abilitybazaar.com/", "status": "complete"}],
            page_results=[
                {
                    "page_url": "https://www.abilitybazaar.com/",
                    "csv_report": csv_text,
                    "summary": {
                        "total_input_tokens": 1_000,
                        "total_output_tokens": 500,
                        "estimated_cost_usd": 0.012345,
                    },
                }
            ],
        )
        self.assertEqual(report.total_findings, 1)
        self.assertIn(b"Whole-Site Accessibility Audit Report", report.html_bytes)
        self.assertIn(b"Page Summary", report.html_bytes)
        self.assertIn(b"page_url", report.csv_bytes)
        self.assertEqual(report.total_tokens, 1_500)
        self.assertEqual(report.estimated_cost_usd, 0.012345)
        with zipfile.ZipFile(io.BytesIO(report.zip_bytes)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "DAT-whole-site-report.html",
                    "DAT-findings.csv",
                    "DAT-summary.json",
                    "DAT-token-and-cost-report.html",
                },
            )
            usage_report = archive.read("DAT-token-and-cost-report.html")
            self.assertIn(b"1,500", usage_report)
            self.assertIn(b"$0.012345", usage_report)

    def test_uploaded_url_list_report_identifies_direct_batch_mode(self):
        report = build_site_report(
            base_url="https://example.com/one",
            model="gpt-5.6-sol",
            audit_mode="url_list",
            source_file_name="pages.txt",
            capped=False,
            candidate_count=2,
            pages=[
                {"url": "https://example.com/one", "status": "complete"},
                {"url": "https://other.example.org/two", "status": "complete"},
            ],
            page_results=[],
        )
        self.assertIn(b"URL-List Batch Accessibility Audit Report", report.html_bytes)
        self.assertIn(b"processed directly without crawling", report.html_bytes)
        with zipfile.ZipFile(io.BytesIO(report.zip_bytes)) as archive:
            summary = json.loads(archive.read("DAT-summary.json"))
            usage = archive.read("DAT-token-and-cost-report.html")
        self.assertEqual(summary["audit_mode"], "url_list")
        self.assertEqual(summary["source_file_name"], "pages.txt")
        self.assertIn(b"Uploaded URL list (pages.txt)", usage)


if __name__ == "__main__":
    unittest.main()
