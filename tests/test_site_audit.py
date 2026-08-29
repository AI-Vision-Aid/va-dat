"""Tests for safe whole-site discovery and consolidated reporting."""

from __future__ import annotations

import io
import json
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from cryptography.fernet import Fernet

from vision_aid.site_audit.crawler import (
    PageCandidate,
    _ai_order_candidates,
    _discover_wordpress_urls,
    _is_candidate_url,
    _sitemap_locations,
    canonicalize_url,
    fetch_public_html,
    validate_public_url,
)
from vision_aid.site_audit.jobs import (
    SiteAuditCoordinator,
    send_report_email,
    validate_request_email,
)
from vision_aid.site_audit.report import build_site_report


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
                {"page_url": "https://www.abilitybazaar.com/", "csv_report": csv_text}
            ],
        )
        self.assertEqual(report.total_findings, 1)
        self.assertIn(b"Whole-Site Accessibility Audit Report", report.html_bytes)
        self.assertIn(b"Page Summary", report.html_bytes)
        self.assertIn(b"page_url", report.csv_bytes)
        with zipfile.ZipFile(io.BytesIO(report.zip_bytes)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"DAT-whole-site-report.html", "DAT-findings.csv", "DAT-summary.json"},
            )


if __name__ == "__main__":
    unittest.main()
