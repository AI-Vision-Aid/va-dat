#!/usr/bin/env python3
"""Serve the team website and run the accessibility audit API locally.

This server has two responsibilities:
  1. Serve static files (index.html, styles.css) from the project root.
  2. Handle POST /api/audit — receive raw HTML, run the pipeline, return JSON.

Usage:
    python entry_points/api_server.py
    python entry_points/api_server.py --port 8080

Then open http://localhost:8000 in your browser.

API keys can be supplied per-request in the POST body, or set in the .env
file / environment.  Per-request keys take priority.

  Anthropic models → field ``api_key``        / env ``ANTHROPIC_API_KEY``
  OpenAI models    → field ``openai_api_key`` / env ``OPENAI_API_KEY``
"""

import json
import os
import re
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

# ── Project root and sys.path ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from entry_points.run_pipeline import run_pipeline  # noqa: E402
from entry_points.generate_report import generate_report  # noqa: E402
from vision_aid.ingestion.file_crawler import fetch_page, fetch_pages_nested  # noqa: E402
from processing_scripts.llm_client.client import is_openai_model  # noqa: E402


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

def _resolve_api_key(data: dict, model: str) -> str:
    """Return the appropriate API key for *model* from the request body or env.

    OpenAI models use the ``openai_api_key`` field / ``OPENAI_API_KEY`` env.
    Anthropic models use the ``api_key`` field / ``ANTHROPIC_API_KEY`` env.
    Per-request keys take priority over environment variables.
    """
    if is_openai_model(model):
        return (
            data.get("openai_api_key", "").strip()
            or os.getenv("OPENAI_API_KEY", "")
        )
    return (
        data.get("api_key", "").strip()
        or os.getenv("ANTHROPIC_API_KEY", "")
    )


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
                api_result = data.get("api_result", {})
                parsed = None
                if api_result.get("success"):
                    parsed = _try_parse_json(api_result.get("response", ""))
                usage = api_result.get("usage", {})
                llm_results[name] = {
                    "checklist": data.get("checklist"),
                    "wcag_criteria": data.get("wcag_criteria", []),
                    "status": "success" if api_result.get("success") else "dry_run",
                    "parsed": parsed,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "duration_seconds": api_result.get("duration_seconds"),
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
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/styles.css":
            self._serve_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        else:
            self.send_error(404, "Not Found")

    def _serve_file(self, file_path: Path, content_type: str):
        if not file_path.exists():
            self.send_error(404, "Not Found")
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # POST ─────────────────────────────────────────────────────────────────────

    def do_POST(self):  # noqa: N802
        if self.path == "/api/audit":
            self._handle_audit()
        elif self.path == "/api/audit/url":
            self._handle_url_audit(nested=False)
        elif self.path == "/api/audit/url/nested":
            self._handle_url_audit(nested=True)
        elif self.path == "/api/validate-key":
            self._handle_validate_key()
        else:
            self.send_error(404, "Not Found")

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
        api_key = _resolve_api_key(data, model)

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

        model = data.get("model", "claude-haiku-4-5-20251001")
        api_key = _resolve_api_key(data, model)

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
            result["type"] = "result"
            send_event(result)
            return

        # ── Multi-page: stream progress, run pipeline per page, merge ─────
        try:
            html_content, crawl_tree = fetch_pages_nested(url)
        except Exception as exc:
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
        merged["type"] = "result"
        _send_event(merged)

    def _handle_validate_key(self):
        """Validate an API key with a lightweight call to the provider."""
        import urllib.request
        import urllib.error

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"valid": False, "error": "Invalid JSON"}, 400)
            return

        api_key = data.get("api_key", "").strip()
        provider = data.get("provider", "anthropic")

        if not api_key:
            self._send_json({"valid": False, "error": "No key provided"})
            return

        try:
            if provider == "openai":
                req = urllib.request.Request(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            with urllib.request.urlopen(req, timeout=10):
                self._send_json({"valid": True})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self._send_json(
                    {"valid": False, "error": "Invalid or unauthorized API key"}
                )
            else:
                self._send_json(
                    {"valid": False, "error": f"Provider returned HTTP {exc.code}"}
                )
        except Exception as exc:
            self._send_json({"valid": False, "error": str(exc)})

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
