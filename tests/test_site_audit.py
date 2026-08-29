"""Tests for safe whole-site discovery and consolidated reporting."""

from __future__ import annotations

import io
import json
import os
import unittest
import zipfile
from unittest import mock

from cryptography.fernet import Fernet

from vision_aid.site_audit.crawler import (
    PageCandidate,
    _ai_order_candidates,
    _discover_wordpress_urls,
    _is_candidate_url,
    _sitemap_locations,
    canonicalize_url,
    validate_public_url,
)
from vision_aid.site_audit.jobs import SiteAuditCoordinator, validate_request_email
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


class EmailTests(unittest.TestCase):
    def test_allowed_domain(self):
        self.assertEqual(
            validate_request_email(" Ram@VisionAid.org ", {"visionaid.org"}),
            "ram@visionaid.org",
        )

    def test_disallowed_domain(self):
        with self.assertRaisesRegex(ValueError, "limited"):
            validate_request_email("person@example.com", {"visionaid.org"})


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
