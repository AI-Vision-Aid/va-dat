#!/usr/bin/env python3
"""Serve the team website and run the accessibility audit API locally.

This server has two responsibilities:
  1. Serve static files (index.html, styles.css) from the project root.
  2. Handle POST /api/audit — receive raw HTML, run the pipeline, return JSON.

Usage:
    python entry_points/api_server.py
    python entry_points/api_server.py --port 8080

Then open http://localhost:8000 in your browser.

Public API keys can be supplied per request. Saved server credentials are used
only for requests carrying a valid administrator cookie.

  Anthropic models → field ``api_key``
  OpenAI models    → field ``openai_api_key``
"""

import json
import hashlib
import hmac
import os
import re
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

# ── Project root and sys.path ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from entry_points.run_pipeline import run_pipeline  # noqa: E402
from entry_points.generate_report import generate_report  # noqa: E402
from vision_aid.ingestion.file_crawler import fetch_page, fetch_pages_nested  # noqa: E402
from processing_scripts.llm_client.client import is_openai_model, is_gemini_model  # noqa: E402
from vision_aid.site_audit.crawler import validate_public_url  # noqa: E402
from vision_aid.site_audit.jobs import get_coordinator  # noqa: E402
from vision_aid.site_audit.url_list import decode_uploaded_urls  # noqa: E402


# ── Multi-page splitting ─────────────────────────────────────────────────────

_PAGE_MARKER = re.compile(r"<!--\s*PAGE:\s*(.*?)\s*-->")


def split_pages(html: str) -> list[tuple[str, str]]:
    """Split concatenated HTML from ``fetch_pages_nested`` into per-page chunks.

    Returns a list of ``(url, html)`` tuples.  If no PAGE markers are found
    the entire string is returned as a single page with url ``"unknown"``.
    """
    markers = list(_PAGE_MARKER.finditer(html))
    if not markers:
        return [("unknown", html)]

    pages: list[tuple[str, str]] = []
    for i, m in enumerate(markers):
        url = m.group(1)
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(html)
        pages.append((url, html[start:end].strip()))
    return pages

STATIC_DIR = PROJECT_ROOT  # index.html and styles.css live at the repo root


# ── Key resolution ────────────────────────────────────────────────────────────

def _resolve_api_key(
    data: dict,
    model: str,
    *,
    saved_openai_api_key: str = "",
) -> str:
    """Return a request key, with an explicit admin-only OpenAI fallback.

    Public requests never inherit provider credentials from the process
    environment. The caller may supply the saved OpenAI key only after it has
    independently verified the admin session.
    """
    if is_openai_model(model):
        return (
            data.get("openai_api_key", "").strip()
            or str(saved_openai_api_key or "").strip()
        )
    if is_gemini_model(model):
        return data.get("gemini_api_key", "").strip()
    return data.get("api_key", "").strip()


# ── Audit logic ───────────────────────────────────────────────────────────────

def run_audit(html_content: str, api_key: str, model: str, progress_callback=None) -> dict:
    """Write *html_content* to a temp file, run the pipeline, return results.

    If *api_key* is empty, the pipeline runs in dry-run mode (programmatic
    checks only, no LLM calls).
    """
    dry_run = not api_key
    tmp_dir = tempfile.mkdtemp(prefix="visionaid_audit_")
    try:
        html_path = Path(tmp_dir) / "input.html"
        html_path.write_text(html_content, encoding="utf-8")
        output_dir = Path(tmp_dir) / "output"

        manifest = run_pipeline(
            html_path=str(html_path),
            output_dir=output_dir,
            api_key=api_key if api_key else None,
            model=model,
            dry_run=dry_run,
            include_summaries=False,
            progress_callback=progress_callback,
        )

        # Read programmatic findings
        prog_path = output_dir / "programmatic_findings.json"
        programmatic_findings = (
            json.loads(prog_path.read_text(encoding="utf-8"))
            if prog_path.exists()
            else []
        )

        # Read per-prompt LLM results
        llm_results = {}
        prompts_dir = output_dir / "prompts"
        if prompts_dir.exists():
            for prompt_file in sorted(prompts_dir.glob("*.json")):
                data = json.loads(prompt_file.read_text(encoding="utf-8"))
                name = data.get("prompt_name", prompt_file.stem)
                # ``api_result`` is only absent when the pipeline actually ran
                # in dry-run mode (no key). If it's present but unsuccessful,
                # a real API call was attempted and failed — that's an error,
                # not a dry run, and must not be silently treated as one.
                api_result = data.get("api_result")
                parsed = None
                if api_result is None:
                    status = "dry_run"
                elif api_result.get("success"):
                    status = "success"
                    parsed = _try_parse_json(api_result.get("response", ""))
                else:
                    status = "error"
                usage = (api_result or {}).get("usage", {})
                llm_results[name] = {
                    "checklist": data.get("checklist"),
                    "wcag_criteria": data.get("wcag_criteria", []),
                    "status": status,
                    "error": (api_result or {}).get("error"),
                    "parsed": parsed,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "duration_seconds": (api_result or {}).get("duration_seconds"),
                }

        # Generate CSV report (only meaningful when LLM ran)
        csv_content = None
        if not dry_run:
            try:
                if progress_callback:
                    progress_callback({
                        "type": "progress",
                        "stage": "report_generating",
                        "message": "Generating report…",
                    })
                report_dir = Path(tmp_dir) / "reports"
                report_path = generate_report(
                    output_dir, report_dir, api_key=api_key, model=model
                )
                csv_content = report_path.read_text(encoding="utf-8")
                if progress_callback:
                    progress_callback({
                        "type": "progress",
                        "stage": "report_complete",
                        "message": "Report ready",
                    })
            except Exception as csv_err:
                print(f"  Warning: CSV generation failed: {csv_err}")

        return {
            "success": True,
            "programmatic_findings": programmatic_findings,
            "llm_results": llm_results,
            "csv_report": csv_content,
            "skipped_prompts": manifest.get("prompts_skipped", []),
            "summary": {
                "programmatic_count": manifest.get("programmatic_findings_count", 0),
                "programmatic_by_checker": manifest.get(
                    "programmatic_findings_by_checker", {}
                ),
                "llm_prompts_run": len(llm_results),
                "llm_prompts_skipped": len(manifest.get("prompts_skipped", [])),
                "total_input_tokens": manifest.get("total_input_tokens", 0),
                "total_output_tokens": manifest.get("total_output_tokens", 0),
                "estimated_cost_usd": manifest.get("estimated_cost_usd"),
                "model": model,
                "dry_run": dry_run,
            },
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _try_parse_json(text: str):
    """Parse JSON from an LLM response, stripping markdown code fences."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    cleaned = m.group(1).strip() if m else text.strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {"raw": text}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class AuditHandler(BaseHTTPRequestHandler):
    """Handle static file serving and the /api/audit endpoint."""

    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write(f"[{self.address_string()}] {fmt % args}\n")

    # CORS helpers ─────────────────────────────────────────────────────────────

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802
        """Handle CORS preflight requests."""
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # GET ──────────────────────────────────────────────────────────────────────

    def do_GET(self):  # noqa: N802
        request_url = urlparse(self.path)
        path = request_url.path
        analytics_page = path in {
            "/analytics",
            "/analytics/full-site",
            "/analytics/url-list",
        }
        analytics_report = re.fullmatch(
            r"/analytics/reports/\d{4}-\d{2}-\d{2}", path
        )
        analytics_api = path == "/api/admin/analytics"
        protected_job_api = re.fullmatch(
            r"/api/site-audits/[A-Za-z0-9_-]+(?:/report)?", path
        )
        if (
            analytics_page
            or analytics_report
            or analytics_api
            or protected_job_api
        ) and not self._admin_access_allowed():
            if analytics_api or protected_job_api:
                self._send_json(
                    {"success": False, "error": "Administrator sign-in required"},
                    401,
                )
            else:
                self._serve_login_page(path)
            return

        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/login":
            requested_next = parse_qs(request_url.query).get("next", ["/"])[0]
            self._serve_login_page(requested_next)
        elif path == "/analytics":
            self._serve_file(
                STATIC_DIR / "analytics.html",
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
        elif path in {"/analytics/full-site", "/analytics/url-list"}:
            self._serve_file(
                STATIC_DIR / "index.html",
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
        elif path == "/api/admin/analytics":
            self._handle_admin_analytics()
        elif analytics_report:
            self._handle_daily_monitor_report(path)
        elif path == "/styles.css":
            self._serve_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        elif path == "/api/health":
            self._handle_health()
        elif path == "/api/site-audit-config":
            self._handle_site_audit_config()
        elif re.fullmatch(r"/api/site-audits/[A-Za-z0-9_-]+/report", path):
            self._handle_site_audit_report(path)
        elif re.fullmatch(r"/api/site-audits/[A-Za-z0-9_-]+", path):
            self._handle_site_audit_status(path)
        else:
            self.send_error(404, "Not Found")

    def _serve_file(
        self,
        file_path: Path,
        content_type: str,
        *,
        cache_control: str = "",
    ):
        if not file_path.exists():
            self.send_error(404, "Not Found")
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # POST ─────────────────────────────────────────────────────────────────────

    def do_POST(self):  # noqa: N802
        if self.path == "/api/login":
            self._handle_login()
        elif self.path == "/api/logout":
            self._handle_logout()
        elif self.path.startswith("/api/internal/site-audits/"):
            operation = self.path.rsplit("/", 1)[-1]
            if operation not in {"discover", "page", "finalize", "daily-monitor"}:
                self.send_error(404, "Not Found")
                return
            self._handle_internal_site_audit(operation)
        elif (
            self.path == "/api/site-audits"
            or self.path == "/api/audit/url/nested"
            or re.fullmatch(r"/api/site-audits/[A-Za-z0-9_-]+/resend", self.path)
        ) and not self._admin_access_allowed():
            self._send_json(
                {"success": False, "error": "Administrator sign-in required"},
                401,
            )
        elif self.path == "/api/audit":
            self._handle_audit()
        elif self.path == "/api/audit/url":
            self._handle_url_audit(nested=False)
        elif self.path == "/api/audit/url/nested":
            self._handle_url_audit(nested=True)
        elif self.path == "/api/validate-key":
            self._handle_validate_key()
        elif self.path == "/api/site-audits":
            self._handle_create_site_audit()
        elif re.fullmatch(r"/api/site-audits/[A-Za-z0-9_-]+/resend", self.path):
            self._handle_site_audit_resend(self.path)
        else:
            self.send_error(404, "Not Found")

    def _admin_cookie(self) -> str:
        password = os.getenv("DAT_SITE_PASSWORD", "").strip()
        if not password:
            return ""
        return hashlib.sha256(f"vision-aid-dat:{password}".encode("utf-8")).hexdigest()

    def _admin_access_allowed(self) -> bool:
        """Return whether the administrator cookie is valid."""
        expected = self._admin_cookie()
        if not expected:
            return False
        cookies = self.headers.get("Cookie", "")
        supplied = ""
        for item in cookies.split(";"):
            name, _, value = item.strip().partition("=")
            if name == "dat_admin":
                supplied = value
                break
        return bool(supplied and hmac.compare_digest(expected, supplied))

    @staticmethod
    def _safe_admin_return_path(next_path: str) -> str:
        allowed_paths = {
            "/",
            "/analytics",
            "/analytics/full-site",
            "/analytics/url-list",
        }
        return next_path if next_path in allowed_paths else "/"

    def _serve_login_page(self, next_path: str = "/"):
        safe_next_path = self._safe_admin_return_path(next_path)
        page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vision Aid DAT administrator sign in</title>
<style>body{font-family:Arial,sans-serif;background:#eef4fb;color:#172033;margin:0;display:grid;min-height:100vh;place-items:center}.card{background:white;border:1px solid #c8d5e6;border-radius:12px;padding:32px;max-width:420px;width:calc(100% - 48px);box-shadow:0 10px 30px #17345c22}label{display:block;font-weight:700;margin:18px 0 6px}input,button{box-sizing:border-box;width:100%;padding:12px;font:inherit;border-radius:7px}input{border:1px solid #8194ac}button{margin-top:16px;background:#184b8a;color:white;border:0;font-weight:700;cursor:pointer}.error{color:#a32121}</style></head>
<body><main class="card"><p>Vision Aid Digital Accessibility Testing</p><h1>Administrator sign in</h1>
<form id="login"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Login</button><p id="error" class="error" role="alert"></p></form><p><a href="/">Back to the public audit tool</a></p></main>
<script>const nextPath=__NEXT_PATH__;document.getElementById('login').addEventListener('submit',async(e)=>{e.preventDefault();const error=document.getElementById('error');error.textContent='';const res=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});if(res.ok){location.assign(nextPath);return;}error.textContent='Incorrect password.';});</script></body></html>"""
        body = page.replace("__NEXT_PATH__", json.dumps(safe_next_path)).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self):
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        expected_password = os.getenv("DAT_SITE_PASSWORD", "").strip()
        supplied = str(data.get("password", ""))
        if not expected_password or not hmac.compare_digest(expected_password, supplied):
            self._send_json({"success": False, "error": "Incorrect password"}, 401)
            return
        body = json.dumps({"success": True}).encode("utf-8")
        secure_flag = "; Secure" if os.getenv("K_SERVICE") or self.headers.get(
            "X-Forwarded-Proto", ""
        ).lower() == "https" else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"dat_admin={self._admin_cookie()}; Path=/; Max-Age=28800; HttpOnly; SameSite=Strict{secure_flag}",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_logout(self):
        body = json.dumps({"success": True}).encode("utf-8")
        secure_flag = "; Secure" if os.getenv("K_SERVICE") or self.headers.get(
            "X-Forwarded-Proto", ""
        ).lower() == "https" else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"dat_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure_flag}",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _start_ndjson_stream(self):
        """Send NDJSON response headers and return a send_event callable."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self._cors_headers()
        self.end_headers()

        def send_event(obj: dict) -> None:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        return send_event

    @staticmethod
    def _record_sync_usage(**kwargs) -> None:
        """Record privacy-safe usage totals without making an audit depend on telemetry."""
        try:
            get_coordinator().record_usage_event(**kwargs)
        except Exception as exc:
            print(f"  Usage event recording failed ({type(exc).__name__})")

    def _handle_audit(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "error": "Invalid JSON body"}, 400)
            return

        html_content = data.get("html_content", "").strip()
        if not html_content:
            self._send_json(
                {"success": False, "error": "html_content is required"}, 400
            )
            return

        model = data.get("model", "claude-haiku-4-5-20251001")
        api_key = _resolve_api_key(
            data,
            model,
            saved_openai_api_key=(
                get_coordinator().api_key if self._admin_access_allowed() else ""
            ),
        )

        print(
            f"  Audit request: {len(html_content):,} chars, "
            f"model={model}, api_key={'set' if api_key else 'not set'}"
        )

        send_event = self._start_ndjson_stream()
        send_event({
            "type": "progress",
            "stage": "starting",
            "message": "Starting audit…",
        })
        result = run_audit(html_content, api_key, model, progress_callback=send_event)
        self._record_sync_usage(
            audit_mode="html_upload",
            model=model,
            result=result,
        )
        result["type"] = "result"
        send_event(result)

    def _handle_url_audit(self, nested: bool):
        """Fetch HTML from a URL (and optionally its nested links) then audit."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "error": "Invalid JSON body"}, 400)
            return

        url = data.get("url", "").strip()
        if not url:
            self._send_json({"success": False, "error": "url is required"}, 400)
            return
        try:
            url = validate_public_url(url)
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return

        model = data.get("model", "claude-haiku-4-5-20251001")
        api_key = _resolve_api_key(
            data,
            model,
            saved_openai_api_key=(
                get_coordinator().api_key if self._admin_access_allowed() else ""
            ),
        )

        print(
            f"  URL audit request ({'nested' if nested else 'single'}): {url}, "
            f"model={model}, api_key={'set' if api_key else 'not set'}"
        )

        if not nested:
            send_event = self._start_ndjson_stream()
            send_event({
                "type": "progress",
                "stage": "fetching",
                "message": f"Fetching {url}…",
            })
            try:
                html_content = fetch_page(url)
            except Exception as exc:
                result = {"type": "result", "success": False, "error": f"Failed to fetch URL: {exc}"}
                self._record_sync_usage(
                    audit_mode="single_url",
                    model=model,
                    result=result,
                    base_url=url,
                )
                send_event(result)
                return
            print(f"  [url_audit] Fetched {len(html_content):,} chars from {url}")
            lower = html_content[:2000].lower()
            if any(marker in lower for marker in ("just a moment", "cf-browser-verification", "ray id", "enable javascript and cookies")):
                print("  [url_audit] WARNING: response looks like a bot-challenge page, not real content")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html_content[:4000], re.IGNORECASE | re.DOTALL)
            print(f"  [url_audit] <title>: {title_match.group(1).strip() if title_match else '(not found)'}")
            send_event({
                "type": "progress",
                "stage": "fetch_complete",
                "message": "Page fetched — starting analysis…",
            })
            result = run_audit(html_content, api_key, model, progress_callback=send_event)
            self._record_sync_usage(
                audit_mode="single_url",
                model=model,
                result=result,
                base_url=url,
            )
            result["type"] = "result"
            send_event(result)
            return

        # ── Multi-page: stream progress, run pipeline per page, merge ─────
        try:
            html_content, crawl_tree = fetch_pages_nested(url)
        except Exception as exc:
            self._record_sync_usage(
                audit_mode="legacy_crawl",
                model=model,
                result={"success": False},
                base_url=url,
            )
            self._send_json({"success": False, "error": f"Failed to fetch URL: {exc}"}, 502)
            return

        pages = split_pages(html_content)
        total_pages = len(pages)
        print(f"  Split into {total_pages} page(s)")

        _send_event = self._start_ndjson_stream()

        _send_event({
            "type": "progress",
            "stage": "crawl_complete",
            "total_pages": total_pages,
            "message": f"Found {total_pages} page(s) to audit",
        })

        merged = {
            "success": True,
            "crawl_tree": crawl_tree,
            "page_results": {},
            "programmatic_findings": [],
            "llm_results": {},
            "csv_report": None,
            "skipped_prompts": [],
            "pages_audited": [],
            "summary": {
                "programmatic_count": 0,
                "programmatic_by_checker": {},
                "llm_prompts_run": 0,
                "llm_prompts_skipped": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "estimated_cost_usd": None,
                "model": model,
                "dry_run": not api_key,
                "pages": total_pages,
            },
        }

        total_cost = 0.0
        csv_parts: list[str] = []
        for page_idx, (page_url, page_html) in enumerate(pages, start=1):
            _send_event({
                "type": "progress",
                "stage": "auditing_page",
                "page": page_idx,
                "total_pages": total_pages,
                "page_url": page_url,
                "message": f"Auditing page {page_idx}/{total_pages}: {page_url}",
            })

            print(f"  Auditing: {page_url}")
            page_result = run_audit(page_html, api_key, model, progress_callback=_send_event)

            if not page_result.get("success"):
                print(f"    FAILED: {page_result.get('error')}")
                _send_event({
                    "type": "progress",
                    "stage": "page_error",
                    "page": page_idx,
                    "page_url": page_url,
                    "message": f"Failed: {page_result.get('error', 'unknown')}",
                })
                continue

            merged["pages_audited"].append(page_url)

            # Store per-page results for the tree view
            merged["page_results"][page_url] = {
                "programmatic_findings": page_result.get("programmatic_findings", []),
                "llm_results": page_result.get("llm_results", {}),
                "summary": page_result.get("summary", {}),
            }

            page_csv = page_result.get("csv_report")
            if page_csv:
                if not csv_parts:
                    csv_parts.append(page_csv.rstrip("\n"))
                else:
                    lines = page_csv.split("\n", 1)
                    if len(lines) > 1 and lines[1].strip():
                        csv_parts.append(lines[1].rstrip("\n"))

            for finding in page_result.get("programmatic_findings", []):
                finding["page_url"] = page_url
            merged["programmatic_findings"].extend(
                page_result.get("programmatic_findings", [])
            )

            for name, result_data in page_result.get("llm_results", {}).items():
                result_data["page_url"] = page_url
                key = f"{name}|{page_url}"
                merged["llm_results"][key] = result_data

            merged["skipped_prompts"].extend(
                page_result.get("skipped_prompts", [])
            )

            page_summary = page_result.get("summary", {})
            merged["summary"]["programmatic_count"] += page_summary.get(
                "programmatic_count", 0
            )
            merged["summary"]["llm_prompts_run"] += page_summary.get(
                "llm_prompts_run", 0
            )
            merged["summary"]["llm_prompts_skipped"] += page_summary.get(
                "llm_prompts_skipped", 0
            )
            merged["summary"]["total_input_tokens"] += page_summary.get(
                "total_input_tokens", 0
            )
            merged["summary"]["total_output_tokens"] += page_summary.get(
                "total_output_tokens", 0
            )
            if page_summary.get("estimated_cost_usd") is not None:
                total_cost += page_summary["estimated_cost_usd"]

            _send_event({
                "type": "progress",
                "stage": "page_complete",
                "page": page_idx,
                "total_pages": total_pages,
                "page_url": page_url,
                "message": f"Completed page {page_idx}/{total_pages}",
            })

        if total_cost > 0:
            merged["summary"]["estimated_cost_usd"] = round(total_cost, 6)

        if csv_parts:
            merged["csv_report"] = "\n".join(csv_parts) + "\n"

        # Final event: the full merged result
        self._record_sync_usage(
            audit_mode="legacy_crawl",
            model=model,
            result=merged,
            base_url=url,
            pages_total=total_pages,
            pages_completed=len(merged["pages_audited"]),
            pages_failed=total_pages - len(merged["pages_audited"]),
        )
        merged["type"] = "result"
        _send_event(merged)

    def _handle_validate_key(self):
        """Validate an entered or saved API key without returning the key."""
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._send_json({"valid": False, "error": str(exc)}, 400)
            return

        provider = data.get("provider", "anthropic")
        default_models = {
            "openai": "gpt-5.6-sol",
            "gemini": "gemini-flash-latest",
            "anthropic": "claude-haiku-4-5-20251001",
        }
        model = str(data.get("model") or default_models.get(provider, "")).strip()
        try:
            valid, message = get_coordinator().verify_requested_key(
                model=model,
                api_key=str(data.get("api_key", "")),
                use_saved=bool(data.get("use_saved")),
                refresh=True,
                allow_saved_key=self._admin_access_allowed(),
            )
            self._send_json(
                {
                    "valid": valid,
                    "message": message if valid else "",
                    "error": "" if valid else message,
                    "model": model,
                }
            )
        except ValueError as exc:
            self._send_json({"valid": False, "error": str(exc)}, 400)

    def _read_json_body(self, *, max_bytes: int = 64 * 1024) -> dict:
        """Read and parse one bounded JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > max_bytes:
            raise ValueError("A JSON request body is required")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _handle_health(self):
        """Return non-secret service health and async feature readiness."""
        coordinator = get_coordinator()
        self._send_json(
            {
                "status": "ok",
                "service": "vision-aid-dat",
                "async_site_audit_configured": coordinator.configured,
                "async_model": coordinator.model,
                "max_site_pages": 200,
                "credential_override_encryption_configured": bool(
                    coordinator.credential_encryption_key
                ),
            }
        )

    def _handle_site_audit_config(self):
        """Return saved-key verification state without exposing credentials."""
        query = parse_qs(urlparse(self.path).query)
        model = str((query.get("model") or [""])[0]).strip() or None
        refresh = str((query.get("refresh") or [""])[0]).lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            config = get_coordinator().public_config(
                model=model,
                refresh=refresh,
                allow_saved_key=self._admin_access_allowed(),
            )
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        self._send_json({"success": True, "config": config})

    def _handle_create_site_audit(self):
        """Create a durable crawl or uploaded URL-list audit and return immediately."""
        try:
            data = self._read_json_body(max_bytes=3 * 1024 * 1024)
            audit_mode = str(data.get("audit_mode") or "crawl").strip().lower()
            source_file_name = ""
            uploaded_urls = None
            if audit_mode == "url_list":
                source_file_name, uploaded_urls = decode_uploaded_urls(
                    data.get("url_file_name", ""),
                    data.get("url_file_base64", ""),
                )
            job = get_coordinator().create_job(
                base_url=data.get("url", ""),
                email=data.get("email", ""),
                model=data.get("model", ""),
                api_key=data.get("api_key", ""),
                audit_mode=audit_mode,
                uploaded_urls=uploaded_urls,
                source_file_name=source_file_name,
                allow_saved_key=self._admin_access_allowed(),
            )
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        except Exception as exc:
            print(f"  Site audit creation failed: {exc}")
            self._send_json(
                {"success": False, "error": "Could not queue the site audit"}, 503
            )
            return
        self._send_json({"success": True, "job": job}, 202)

    def _handle_site_audit_status(self, path: str):
        job_id = path.rstrip("/").rsplit("/", 1)[-1]
        coordinator = get_coordinator()
        job = coordinator.get_job(job_id)
        if not job:
            self._send_json({"success": False, "error": "Job not found"}, 404)
            return
        self._send_json({"success": True, "job": coordinator.public_job(job)})

    def _handle_site_audit_report(self, path: str):
        job_id = path.split("/")[-2]
        report = get_coordinator().report_bytes(job_id)
        if report is None:
            self._send_json({"success": False, "error": "Report is not ready"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition", 'attachment; filename="DAT-whole-site-report.zip"'
        )
        self.send_header("Content-Length", str(len(report)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(report)

    def _handle_site_audit_resend(self, path: str):
        job_id = path.split("/")[-2]
        try:
            job = get_coordinator().resend_report(job_id)
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        except Exception as exc:
            print(f"  Site audit email resend failed: {exc}")
            self._send_json(
                {"success": False, "error": "The mail server did not accept the resend"},
                503,
            )
            return
        self._send_json({"success": True, "job": job})

    def _handle_admin_analytics(self):
        """Return privacy-safe administrator analytics and recent reports."""
        try:
            coordinator = get_coordinator()
            self._send_json(
                {
                    "success": True,
                    "summary": coordinator.analytics_summary(),
                    "reports": coordinator.list_daily_monitor_reports(limit=30),
                }
            )
        except Exception as exc:
            print(f"  Administrator analytics failed: {exc}")
            self._send_json(
                {"success": False, "error": "Analytics are temporarily unavailable"},
                503,
            )

    def _handle_daily_monitor_report(self, path: str):
        """Serve one retained private daily report to an administrator."""
        report_date = path.rstrip("/").rsplit("/", 1)[-1]
        try:
            body = get_coordinator().daily_monitor_report_bytes(report_date)
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        except Exception as exc:
            print(f"  Daily monitor report retrieval failed: {exc}")
            self._send_json(
                {"success": False, "error": "The daily report is unavailable"},
                503,
            )
            return
        if body is None:
            self.send_error(404, "Report not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _handle_internal_site_audit(self, operation: str):
        """Handle authenticated callbacks from the dedicated Cloud Tasks queue."""
        coordinator = get_coordinator()
        supplied_token = self.headers.get("X-DAT-Job-Token", "")
        if not coordinator.internal_token_valid(supplied_token):
            self._send_json({"success": False, "error": "Not found"}, 404)
            return
        try:
            data = self._read_json_body()
            if operation == "daily-monitor":
                result = coordinator.run_daily_monitor(
                    schedule_time=(
                        self.headers.get("X-CloudScheduler-ScheduleTime", "")
                        or str(data.get("schedule_time") or "")
                    ),
                    send_email=bool(data.get("send_email", True)),
                    force=bool(data.get("force", False)),
                )
            elif operation == "discover":
                job_id = data.get("job_id", "")
                result = coordinator.run_discovery(job_id)
            elif operation == "page":
                job_id = data.get("job_id", "")
                retry_count = int(self.headers.get("X-CloudTasks-TaskRetryCount", "0"))
                result = coordinator.run_page(
                    job_id=job_id,
                    page_id=data.get("page_id", ""),
                    retry_count=retry_count,
                    audit_callable=run_audit,
                )
            else:
                job_id = data.get("job_id", "")
                result = coordinator.finalize(job_id)
        except ValueError as exc:
            self._send_json({"success": False, "error": str(exc)}, 400)
            return
        except Exception as exc:
            print(f"  Internal site audit {operation} failed: {exc}")
            self._send_json({"success": False, "error": "Task failed; retrying"}, 500)
            return
        self._send_json({"success": True, "result": result})

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """Start the HTTP server.

    Host and port can be set via CLI flags or environment variables:
      HOST  (default: 127.0.0.1, use 0.0.0.0 for cloud deployment)
      PORT  (default: 8000, set automatically by Render/Railway)
    """
    import argparse

    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Run the accessibility audit web server."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", 8000)),
        help="Port to listen on (default: $PORT or 8000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("HOST", "127.0.0.1"),
        help="Host to bind to (default: $HOST or 127.0.0.1; use 0.0.0.0 for deployment)",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AuditHandler)
    display_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print("Accessibility Audit Server")
    print(f"  URL  : http://{display_host}:{args.port}")
    print(f"  API  : POST http://{display_host}:{args.port}/api/audit")
    print("  Press Ctrl+C to stop.")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
