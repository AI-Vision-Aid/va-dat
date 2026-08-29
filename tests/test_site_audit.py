"""Tests for safe whole-site discovery and consolidated reporting."""

from __future__ import annotations

import io
import unittest
import zipfile
from unittest import mock

from vision_aid.site_audit.crawler import canonicalize_url, validate_public_url
from vision_aid.site_audit.jobs import validate_request_email
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


class EmailTests(unittest.TestCase):
    def test_allowed_domain(self):
        self.assertEqual(
            validate_request_email(" Ram@VisionAid.org ", {"visionaid.org"}),
            "ram@visionaid.org",
        )

    def test_disallowed_domain(self):
        with self.assertRaisesRegex(ValueError, "limited"):
            validate_request_email("person@example.com", {"visionaid.org"})


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
