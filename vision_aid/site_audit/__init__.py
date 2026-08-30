"""Asynchronous multi-page accessibility audit support.

The synchronous single-page pipeline remains in :mod:`entry_points.api_server`.
This package adds deterministic crawling, direct URL-list batching, AI-assisted
page selection, durable Cloud Tasks orchestration, and consolidated reports for
long-running audits.
"""

from .crawler import DiscoveryResult, discover_site_urls, fetch_public_html
from .report import build_site_report

__all__ = [
    "DiscoveryResult",
    "build_site_report",
    "discover_site_urls",
    "fetch_public_html",
]
