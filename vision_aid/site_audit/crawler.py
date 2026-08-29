"""Safe, sitemap-aware whole-site discovery with AI-assisted selection."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = "VisionAid-DAT/1.0 (+https://www.visionaid.org/)"
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
    "wbraid",
}
SKIP_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
SITEMAP_LIMIT = 50
CANDIDATE_LIMIT = 2_000
DISCOVERY_FETCH_LIMIT = 400
MAX_HTML_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PageCandidate:
    """One public page candidate found from a sitemap or in-site link."""

    url: str
    title: str = ""
    source: str = "link"


@dataclass(frozen=True)
class DiscoveryResult:
    """Final ordered page list and discovery metadata."""

    pages: list[PageCandidate]
    candidate_count: int
    capped: bool
    ai_used: bool
    sitemap_count: int


def canonicalize_url(url: str) -> str:
    """Return a stable HTTP(S) URL without fragments or tracking parameters."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_items))
    return urlunparse((scheme, netloc, path, "", query, ""))


def _site_host(host: str) -> str:
    """Treat a site's apex and ``www`` host as the same crawl boundary."""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _is_same_site(url: str, base_url: str) -> bool:
    return _site_host(urlparse(url).hostname or "") == _site_host(
        urlparse(base_url).hostname or ""
    )


def validate_public_url(url: str) -> str:
    """Validate and normalize a URL, rejecting private-network SSRF targets."""
    normalized = canonicalize_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http:// or https:// URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not supported")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {parsed.hostname}") from exc

    if not addresses:
        raise ValueError(f"Could not resolve host: {parsed.hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("Private, local, or reserved network URLs are not supported")
    return normalized


def _request(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    max_bytes: int,
    accepted_types: tuple[str, ...],
) -> tuple[str, str, str]:
    """Fetch a public URL safely and return ``(body, final_url, content_type)``."""
    current = validate_public_url(url)
    for _ in range(6):
        response = session.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.1"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.HTTPError("Redirect response had no Location header")
            current = validate_public_url(urljoin(current, location))
            continue
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if accepted_types and not any(content_type == item for item in accepted_types):
            response.close()
            raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                response.close()
                raise ValueError(f"Response exceeds the {max_bytes // (1024 * 1024)} MB limit")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        body = b"".join(chunks).decode(encoding, errors="replace")
        final_url = canonicalize_url(current)
        response.close()
        return body, final_url, content_type
    raise ValueError("Too many redirects")


def fetch_public_html(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> tuple[str, str]:
    """Fetch one public HTML page and return ``(html, final_url)``."""
    active_session = session or requests.Session()
    body, final_url, _ = _request(
        active_session,
        url,
        timeout=timeout,
        max_bytes=MAX_HTML_BYTES,
        accepted_types=("text/html", "application/xhtml+xml"),
    )
    return body, final_url


def _fetch_text_or_xml(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 20,
) -> tuple[str, str]:
    body, final_url, _ = _request(
        session,
        url,
        timeout=timeout,
        max_bytes=5 * 1024 * 1024,
        accepted_types=(),
    )
    return body, final_url


def _sitemap_locations(base_url: str, session: requests.Session) -> list[str]:
    """Return robots-declared sitemaps plus the conventional sitemap URL."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    locations = [urljoin(origin, "/sitemap.xml")]
    try:
        robots, _ = _fetch_text_or_xml(session, urljoin(origin, "/robots.txt"))
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                location = line.split(":", 1)[1].strip()
                if location:
                    locations.append(urljoin(origin, location))
    except Exception:
        pass
    return list(dict.fromkeys(canonicalize_url(item) for item in locations))


def _read_sitemaps(base_url: str, session: requests.Session) -> tuple[list[str], int]:
    """Read sitemap indexes recursively, returning same-site page URLs."""
    queue = deque(_sitemap_locations(base_url, session))
    seen_sitemaps: set[str] = set()
    page_urls: list[str] = []

    while queue and len(seen_sitemaps) < SITEMAP_LIMIT and len(page_urls) < CANDIDATE_LIMIT:
        sitemap_url = canonicalize_url(queue.popleft())
        if sitemap_url in seen_sitemaps or not _is_same_site(sitemap_url, base_url):
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            xml_text, _ = _fetch_text_or_xml(session, sitemap_url)
            root = ET.fromstring(xml_text)
        except Exception:
            continue

        root_name = root.tag.rsplit("}", 1)[-1].lower()
        locations = [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1].lower() == "loc" and element.text
        ]
        if root_name == "sitemapindex":
            queue.extend(locations)
            continue
        for location in locations:
            candidate = canonicalize_url(location)
            if _is_candidate_url(candidate, base_url) and candidate not in page_urls:
                page_urls.append(candidate)
                if len(page_urls) >= CANDIDATE_LIMIT:
                    break
    return page_urls, len(seen_sitemaps)


def _is_candidate_url(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if not _is_same_site(url, base_url):
        return False
    path = parsed.path.lower()
    return not any(path.endswith(extension) for extension in SKIP_EXTENSIONS)


def _extract_links(html: str, page_url: str, base_url: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:200] if soup.title else ""
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        candidate = canonicalize_url(urljoin(page_url, href))
        if _is_candidate_url(candidate, base_url):
            links.append(candidate)
    return title, list(dict.fromkeys(links))


def _parse_json_object(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    cleaned = match.group(1).strip() if match else text.strip()
    return json.loads(cleaned)


def _ai_filter_candidates(
    candidates: list[PageCandidate],
    *,
    api_key: str,
    model: str,
    max_pages: int,
) -> list[PageCandidate]:
    """Use OpenAI to identify and order public, user-facing HTML pages."""
    if not api_key or not candidates:
        return candidates[:max_pages]

    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=4, timeout=180.0)
    rows = "\n".join(
        f"{index}\t{candidate.url}\t{candidate.title or '(title unknown)'}\t{candidate.source}"
        for index, candidate in enumerate(candidates)
    )
    prompt = f"""You are selecting pages for a whole-site WCAG accessibility audit.
The deterministic crawler found the numbered same-site URL candidates below.
Return public, user-facing HTML pages only. Exclude logout/action endpoints,
admin-only paths, duplicate query variants, feeds, downloads, and obvious
non-pages. Prefer canonical, representative pages and put the most important
pages first. Never invent a URL. Return JSON only as
{{"include":[integer indexes]}} with at most {max_pages} unique indexes.

Candidates:
{rows}
"""
    response = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=8_000,
        reasoning={"effort": "low"},
    )
    parsed = _parse_json_object(response.output_text)
    indexes: list[int] = []
    for value in parsed.get("include", []):
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(candidates) and index not in indexes:
            indexes.append(index)
        if len(indexes) >= max_pages:
            break
    if not indexes:
        return candidates[:max_pages]
    return [candidates[index] for index in indexes]


def discover_site_urls(
    base_url: str,
    *,
    api_key: str = "",
    model: str = "gpt-5.6-sol",
    max_pages: int = 200,
    session: requests.Session | None = None,
) -> DiscoveryResult:
    """Discover, AI-filter, and order up to ``max_pages`` site pages.

    Discovery combines the site's sitemap/robots declarations with a bounded
    breadth-first traversal of same-site links. The model may filter and order
    only URLs the deterministic crawler actually found; hallucinated URLs are
    therefore impossible to add to the audit.
    """
    if not 1 <= max_pages <= 200:
        raise ValueError("max_pages must be between 1 and 200")
    normalized_base = validate_public_url(base_url)
    active_session = session or requests.Session()

    sitemap_urls, sitemap_count = _read_sitemaps(normalized_base, active_session)
    queue = deque([normalized_base, *sitemap_urls])
    candidates: dict[str, PageCandidate] = {
        normalized_base: PageCandidate(normalized_base, source="base")
    }
    for sitemap_url in sitemap_urls:
        candidates.setdefault(
            sitemap_url, PageCandidate(sitemap_url, source="sitemap")
        )

    fetched: set[str] = set()
    while queue and len(fetched) < DISCOVERY_FETCH_LIMIT and len(candidates) < CANDIDATE_LIMIT:
        current = canonicalize_url(queue.popleft())
        if current in fetched or not _is_candidate_url(current, normalized_base):
            continue
        fetched.add(current)
        try:
            html, final_url = fetch_public_html(current, session=active_session)
        except Exception:
            continue
        if not _is_same_site(final_url, normalized_base):
            continue
        title, links = _extract_links(html, final_url, normalized_base)
        existing = candidates.get(current)
        candidates[current] = PageCandidate(
            url=current,
            title=title or (existing.title if existing else ""),
            source=existing.source if existing else "link",
        )
        for link in links:
            if link not in candidates:
                candidates[link] = PageCandidate(link, source="link")
                queue.append(link)
                if len(candidates) >= CANDIDATE_LIMIT:
                    break

    ordered = list(candidates.values())
    base_candidate = candidates[normalized_base]
    ordered = [base_candidate, *[item for item in ordered if item.url != normalized_base]]
    ai_used = bool(api_key)
    try:
        selected = _ai_filter_candidates(
            ordered,
            api_key=api_key,
            model=model,
            max_pages=max_pages,
        )
    except Exception as exc:
        print(f"  WARNING: AI page selection failed ({exc}); using crawler order")
        selected = ordered[:max_pages]
        ai_used = False

    if normalized_base not in {item.url for item in selected}:
        selected = [base_candidate, *selected][:max_pages]
    capped = len(ordered) > max_pages
    return DiscoveryResult(
        pages=selected[:max_pages],
        candidate_count=len(ordered),
        capped=capped,
        ai_used=ai_used,
        sitemap_count=sitemap_count,
    )
