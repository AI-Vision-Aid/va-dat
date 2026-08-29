"""Safe, multi-source whole-site discovery with AI-assisted ordering."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.1",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
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
SITEMAP_LIMIT = 100
CANDIDATE_LIMIT = 2_000
DISCOVERY_FETCH_LIMIT = 400
MAX_HTML_BYTES = 8 * 1024 * 1024
COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
)
SKIP_PATH_PREFIXES = (
    "/wp-admin",
    "/wp-json",
    "/wp-login.php",
    "/admin",
    "/login",
    "/logout",
)
SKIP_QUERY_ACTIONS = {
    "delete",
    "edit",
    "lostpassword",
    "logout",
    "register",
    "resetpass",
}
SKIP_QUERY_KEYS = {
    "add-to-cart",
    "customize_changeset_uuid",
    "elementor-preview",
    "elementor_snippet",
    "preview",
    "replytocom",
    "wc-ajax",
}
BOT_CHALLENGE_MARKERS = (
    "robot challenge screen",
    "checking the site connection security",
    "d1rozh26tys225.cloudfront.net/loader.svg",
    "sg-captcha",
    "sgcaptcha",
)


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
    ai_web_used: bool
    sitemap_count: int
    source_counts: dict[str, int]


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


def _looks_like_bot_challenge(body: str, final_url: str) -> bool:
    """Recognize soft-200 interstitials that must never be audited as pages."""
    path = urlparse(final_url).path.lower()
    if "/.well-known/sgcaptcha" in path or "/.well-known/captcha" in path:
        return True
    sample = body[:250_000].lower()
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


def _browser_fetch(
    url: str,
    *,
    timeout: int,
    max_bytes: int,
    accepted_types: tuple[str, ...],
) -> tuple[str, str, str]:
    """Solve JavaScript interstitials in Chromium while preserving SSRF checks."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    original = validate_public_url(url)
    executable = os.getenv("DAT_CHROMIUM_EXECUTABLE", "").strip() or None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={
                    "Accept-Language": REQUEST_HEADERS["Accept-Language"]
                },
                service_workers="block",
            )
            page = context.new_page()

            def safe_route(route, request):
                request_url = request.url
                if request_url.startswith(("http://", "https://")):
                    try:
                        validate_public_url(request_url)
                    except ValueError:
                        route.abort("blockedbyclient")
                        return
                route.continue_()

            page.route("**/*", safe_route)
            response = page.goto(
                original,
                wait_until="domcontentloaded",
                timeout=timeout * 1_000,
            )
            deadline = time.monotonic() + min(25, timeout)
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "The site's browser security challenge could not be completed"
                    )
                try:
                    current_body = page.content()
                    current_url = page.url
                except PlaywrightError:
                    # A successful interstitial may replace itself while this
                    # loop is sampling the DOM. Let navigation settle instead
                    # of treating that normal transition as a page failure.
                    page.wait_for_timeout(250)
                    continue
                if not _looks_like_bot_challenge(current_body, current_url):
                    break
                page.wait_for_timeout(1_000)

            # Reload after the challenge so ``response.body()`` is the actual
            # requested resource rather than the initial interstitial.
            response = page.reload(
                wait_until="domcontentloaded",
                timeout=timeout * 1_000,
            )
            final_url = validate_public_url(page.url)
            if response is None:
                raise RuntimeError("The browser did not return a page response")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if accepted_types and not any(content_type == item for item in accepted_types):
                raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
            content = response.body()
            if len(content) > max_bytes:
                raise ValueError(
                    f"Response exceeds the {max_bytes // (1024 * 1024)} MB limit"
                )
            encoding_match = re.search(
                r"charset=([A-Za-z0-9._-]+)", response.headers.get("content-type", "")
            )
            encoding = encoding_match.group(1) if encoding_match else "utf-8"
            body = content.decode(encoding, errors="replace")
            if _looks_like_bot_challenge(body, final_url):
                raise RuntimeError("The site returned a browser security challenge")
            return body, canonicalize_url(final_url), content_type
        finally:
            browser.close()


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
        try:
            response = session.get(
                current,
                headers=REQUEST_HEADERS,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {403, 406, 429, 503}:
                response.close()
                raise requests.HTTPError(
                    f"HTTP {response.status_code} blocked the standard crawler"
                )
        except (requests.RequestException, OSError) as primary_error:
            # Some bot-protected sites reject the default Python TLS signature
            # even when the headers identify a current browser. curl_cffi uses a
            # real browser TLS/HTTP fingerprint while the redirect and SSRF
            # checks below remain under our control.
            try:
                from curl_cffi import requests as browser_requests

                response = browser_requests.get(
                    current,
                    headers=REQUEST_HEADERS,
                    timeout=timeout,
                    allow_redirects=False,
                    impersonate="chrome",
                    stream=True,
                )
            except Exception:
                raise primary_error
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
        if _looks_like_bot_challenge(body, final_url):
            return _browser_fetch(
                current,
                timeout=max(timeout, 45),
                max_bytes=max_bytes,
                accepted_types=accepted_types,
            )
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
    """Return robots-declared and common sitemap locations.

    Some hosts block ``robots.txt`` or redirect ``sitemap.xml`` differently for
    cloud data-center traffic. Trying the standard index variants directly
    prevents one failed discovery endpoint from collapsing a crawl to one page.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    locations = [urljoin(origin, path) for path in COMMON_SITEMAP_PATHS]
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
    normalized_path = path.rstrip("/") or "/"
    if any(path.endswith(extension) for extension in SKIP_EXTENSIONS):
        return False
    if any(normalized_path == prefix or normalized_path.startswith(f"{prefix}/") for prefix in SKIP_PATH_PREFIXES):
        return False
    if normalized_path.endswith("/feed"):
        return False
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if any(key.lower() in SKIP_QUERY_KEYS for key in query):
        return False
    if str(query.get("action", "")).lower() in SKIP_QUERY_ACTIONS:
        return False
    return True


def _discover_wordpress_urls(base_url: str, session: requests.Session) -> list[str]:
    """Discover public WordPress content through its read-only REST index.

    This is a fallback for WordPress sites whose HTML or sitemap requests are
    blocked for cloud crawlers. Only canonical public ``link`` values returned
    by the site's own API are accepted, and the normal same-site/safety filters
    still apply.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    rest_bases = ["pages", "posts"]
    try:
        types_text, _ = _fetch_text_or_xml(
            session, urljoin(origin, "/wp-json/wp/v2/types")
        )
        types = json.loads(types_text)
        if isinstance(types, dict):
            discovered_bases = [
                str(item.get("rest_base", "")).strip("/")
                for item in types.values()
                if isinstance(item, dict) and item.get("viewable") is not False
            ]
            rest_bases = list(
                dict.fromkeys(item for item in discovered_bases if item)
            ) or rest_bases
    except Exception:
        pass

    page_urls: list[str] = []
    for rest_base in rest_bases[:25]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", rest_base):
            continue
        for page_number in range(1, 21):
            endpoint = urljoin(
                origin,
                f"/wp-json/wp/v2/{rest_base}?per_page=100&page={page_number}&_fields=link,status",
            )
            try:
                body, _ = _fetch_text_or_xml(session, endpoint)
                rows = json.loads(body)
            except Exception:
                break
            if not isinstance(rows, list):
                break
            for row in rows:
                if not isinstance(row, dict) or row.get("status") not in (None, "publish"):
                    continue
                candidate = canonicalize_url(str(row.get("link", "")))
                if _is_candidate_url(candidate, base_url) and candidate not in page_urls:
                    page_urls.append(candidate)
                    if len(page_urls) >= CANDIDATE_LIMIT:
                        return page_urls
            if len(rows) < 100:
                break
    return page_urls


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


def _ai_order_candidates(
    candidates: list[PageCandidate],
    *,
    api_key: str,
    model: str,
    max_pages: int,
) -> list[PageCandidate]:
    """Use OpenAI to order candidates without ever dropping one."""
    if not api_key or not candidates:
        return candidates[:max_pages]

    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=4, timeout=180.0)
    rows = "\n".join(
        f"{index}\t{candidate.url}\t{candidate.title or '(title unknown)'}\t{candidate.source}"
        for index, candidate in enumerate(candidates)
    )
    prompt = f"""You are ordering pages for a whole-site WCAG accessibility audit.
The deterministic crawler found the numbered same-site URL candidates below.
The crawler has already removed administrative actions, feeds, downloads, and
obvious non-pages. Put the most important public pages first, but return EVERY
candidate index exactly once. Never invent an index or URL. Return JSON only as
{{"order":[integer indexes]}}.

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
    for value in parsed.get("order", []):
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(candidates) and index not in indexes:
            indexes.append(index)
    indexes.extend(index for index in range(len(candidates)) if index not in indexes)
    return [candidates[index] for index in indexes]


def _ai_web_discover_candidates(
    base_url: str,
    *,
    api_key: str,
    model: str,
    max_pages: int,
) -> list[str]:
    """Use OpenAI web search only when the target blocks every direct source."""
    if not api_key or not model.startswith(("gpt-", "o1", "o3", "o4")):
        return []

    from openai import OpenAI

    host = urlparse(base_url).hostname or base_url
    client = OpenAI(api_key=api_key, max_retries=4, timeout=180.0)
    response = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        reasoning={"effort": "low"},
        max_output_tokens=12_000,
        input=f"""Find the public HTML pages belonging to {base_url} for a complete
accessibility audit. Search the web for indexed pages on site:{host}, inspect
public sitemap results when available, and return up to {max_pages} canonical
same-site URLs. Exclude files, feeds, login/admin pages, previews, search-result
pages, and action URLs. Do not invent URLs. Return JSON only as
{{"urls":["https://..."]}}.
""",
    )
    parsed = _parse_json_object(response.output_text)
    urls: list[str] = []
    for value in parsed.get("urls", []):
        candidate = canonicalize_url(str(value))
        if _is_candidate_url(candidate, base_url) and candidate not in urls:
            urls.append(candidate)
            if len(urls) >= max_pages:
                break
    return urls


def discover_site_urls(
    base_url: str,
    *,
    api_key: str = "",
    model: str = "gpt-5.6-sol",
    max_pages: int = 200,
    session: requests.Session | None = None,
) -> DiscoveryResult:
    """Discover and order up to ``max_pages`` site pages.

    Discovery combines robots declarations, common recursive sitemap indexes,
    public WordPress REST metadata when available, and a bounded breadth-first
    traversal of same-site links. The model may reorder only URLs the
    deterministic crawler actually found. It cannot remove pages or add
    hallucinated URLs.
    """
    if not 1 <= max_pages <= 200:
        raise ValueError("max_pages must be between 1 and 200")
    normalized_base = validate_public_url(base_url)
    active_session = session or requests.Session()

    sitemap_urls, sitemap_count = _read_sitemaps(normalized_base, active_session)
    wordpress_urls = _discover_wordpress_urls(normalized_base, active_session)
    ai_web_urls: list[str] = []
    ai_web_used = False
    if len({normalized_base, *sitemap_urls, *wordpress_urls}) <= 1:
        try:
            ai_web_urls = _ai_web_discover_candidates(
                normalized_base,
                api_key=api_key,
                model=model,
                max_pages=max_pages,
            )
            ai_web_used = bool(ai_web_urls)
        except Exception as exc:
            print(f"  WARNING: AI web discovery failed ({exc}); continuing direct crawl")

    queue = deque([normalized_base, *sitemap_urls, *wordpress_urls, *ai_web_urls])
    candidates: dict[str, PageCandidate] = {
        normalized_base: PageCandidate(normalized_base, source="base")
    }
    for sitemap_url in sitemap_urls:
        candidates.setdefault(
            sitemap_url, PageCandidate(sitemap_url, source="sitemap")
        )
    for wordpress_url in wordpress_urls:
        candidates.setdefault(
            wordpress_url, PageCandidate(wordpress_url, source="cms")
        )
    for ai_web_url in ai_web_urls:
        candidates.setdefault(
            ai_web_url, PageCandidate(ai_web_url, source="ai-web")
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
    ai_used = bool(api_key and model.startswith(("gpt-", "o1", "o3", "o4")))
    try:
        selected = (
            _ai_order_candidates(
                ordered,
                api_key=api_key,
                model=model,
                max_pages=max_pages,
            )
            if ai_used
            else ordered
        )
    except Exception as exc:
        print(f"  WARNING: AI page ordering failed ({exc}); using crawler order")
        selected = ordered
        ai_used = False

    if normalized_base not in {item.url for item in selected}:
        selected = [base_candidate, *selected][:max_pages]
    capped = len(ordered) > max_pages
    source_counts: dict[str, int] = {}
    for candidate in ordered:
        source_counts[candidate.source] = source_counts.get(candidate.source, 0) + 1
    return DiscoveryResult(
        pages=selected[:max_pages],
        candidate_count=len(ordered),
        capped=capped,
        ai_used=ai_used,
        ai_web_used=ai_web_used,
        sitemap_count=sitemap_count,
        source_counts=source_counts,
    )
