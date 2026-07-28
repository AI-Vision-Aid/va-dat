#!/usr/bin/env python3
"""Run the element-specific accessibility audit pipeline.

This script orchestrates the full pipeline:
  1. Programmatic checks (free, no API calls)
  2. Extract structured payloads from HTML
  3. Slice payloads into targeted prompts
  4. Call the LLM for each prompt (or save prompts in --dry-run mode)
  5. Save results as raw JSON

Usage:
    python entry_points/run_pipeline.py --html test_files/home.html
    python entry_points/run_pipeline.py --html test_files/home.html --dry-run
    python entry_points/run_pipeline.py --html test_files/home.html --include-summaries
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Ensure project root is on sys.path so we can import processing_scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from processing_scripts.llm.registry import PROMPT_REGISTRY, PromptSpec
from processing_scripts.llm.slicers import get_slicer, is_empty_slice
from processing_scripts.llm.templates import fill_template
from processing_scripts.llm_preprocessing.semantic_checklist_01 import (
    extract as cl01_extract,
)
from processing_scripts.llm_preprocessing.forms_checklist_02 import (
    extract as cl02_extract,
)
from processing_scripts.llm_preprocessing.nontext_checklist_03 import (
    extract as cl03_extract,
)
from processing_scripts.llm.filters import (
    apply_cl01_filters,
    apply_cl02_filters,
    apply_cl03_filters,
    build_filter_flags,
)
from processing_scripts.programmatic.semantic_checklist_01 import audit_html_file
from processing_scripts.programmatic.forms_checklist_02 import audit_forms
from processing_scripts.programmatic.nontext_checklist_03 import audit_nontext


# Pricing per million tokens: (input, output)
MODEL_PRICING = {
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-haiku-4": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
}


def get_pricing(model: str) -> tuple[float, float] | None:
    """Look up per-million-token pricing for a model.

    Matches on model prefix so dated model IDs (e.g.
    claude-sonnet-4-20250514) resolve correctly.
    Returns (input_cost, output_cost) or None if unknown.
    """
    for prefix, pricing in MODEL_PRICING.items():
        if model.startswith(prefix):
            return pricing
    return None


def compute_cost(input_tokens: int, output_tokens: int, model: str) -> float | None:
    """Return the estimated dollar cost for a run, or None if pricing is unknown."""
    pricing = get_pricing(model)
    if pricing is None:
        return None
    input_cost, output_cost = pricing
    return (input_tokens * input_cost + output_tokens * output_cost) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token for English/HTML)."""
    return max(1, len(text) // 4)


class PipelineClient:
    """Thin wrapper around the Anthropic, OpenAI, or Gemini API.

    Detects the provider from the model ID and uses the appropriate SDK.
    Returns the same dict shape regardless of provider so downstream code
    (``generate_report.py``, etc.) works unchanged.
    """

    def __init__(self, api_key: str, model: str, max_tokens: int = 8192):
        from processing_scripts.llm_client.client import is_openai_model, is_gemini_model

        self.model = model
        self.max_tokens = max_tokens
        self._is_openai = is_openai_model(model)
        self._is_gemini = is_gemini_model(model)

        if self._is_openai:
            import openai
            self._client = openai.OpenAI(api_key=api_key)
        elif self._is_gemini:
            from google import genai
            self._client = genai.Client(api_key=api_key)
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)

    def call(self, prompt: str) -> dict:
        """Send *prompt* to the API and return a result dict.

        Returns a dict with keys ``success``, ``response``, ``model``,
        ``usage``, ``stop_reason``, and ``duration_seconds``.
        """
        start = time.time()

        try:
            if self._is_openai:
                return self._call_openai(prompt, start)
            if self._is_gemini:
                return self._call_gemini(prompt, start)
            return self._call_anthropic(prompt, start)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "duration_seconds": round(time.time() - start, 2),
            }

    def _call_anthropic(self, prompt: str, start: float) -> dict:
        """Call the Anthropic Messages API."""
        message = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = "".join(
            block.text for block in message.content if block.type == "text"
        )

        return {
            "success": True,
            "response": response_text,
            "model": message.model,
            "usage": {
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            },
            "stop_reason": message.stop_reason,
            "duration_seconds": round(time.time() - start, 2),
        }

    def _call_openai(self, prompt: str, start: float) -> dict:
        """Call the OpenAI Chat Completions API."""
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )

        return {
            "success": True,
            "response": response.choices[0].message.content,
            "model": response.model,
            "usage": {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            },
            "stop_reason": response.choices[0].finish_reason,
            "duration_seconds": round(time.time() - start, 2),
        }

    def _call_gemini(self, prompt: str, start: float) -> dict:
        """Call the Gemini generateContent API."""
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "temperature": 0.1,
                "max_output_tokens": self.max_tokens,
            },
        )

        return {
            "success": True,
            "response": response.text,
            "model": self.model,
            "usage": {
                "input_tokens": response.usage_metadata.prompt_token_count,
                "output_tokens": response.usage_metadata.candidates_token_count,
            },
            "stop_reason": response.candidates[0].finish_reason.name
            if response.candidates
            else None,
            "duration_seconds": round(time.time() - start, 2),
        }


def save_json(obj: object, path: Path) -> None:
    """Write an object as formatted JSON to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def run_pipeline(
    html_path: str,
    output_dir: Path,
    api_key: str | None,
    model: str,
    dry_run: bool,
    include_summaries: bool,
    progress_callback=None,
) -> dict:
    """Execute the full element-specific accessibility audit pipeline.

    Returns a summary dict with run metadata and per-prompt results.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path_str = str(html_path)

    # ── Step 0: Programmatic checks ──────────────────────────────────────────
    print("Step 0: Running programmatic checks...")
    sem_findings = audit_html_file(html_path_str)
    form_findings = audit_forms(html_path_str)
    ntext_findings = audit_nontext(html_path_str)
    programmatic_findings = sem_findings + form_findings + ntext_findings
    save_json(programmatic_findings, output_dir / "programmatic_findings.json")
    print(
        f"  CL01: {len(sem_findings)} | CL02: {len(form_findings)} | "
        f"CL03: {len(ntext_findings)} | Total: {len(programmatic_findings)}"
    )
    if progress_callback:
        progress_callback({
            "type": "progress",
            "stage": "programmatic_complete",
            "message": f"Programmatic checks complete — {len(programmatic_findings)} issue(s) found",
        })

    # ── Step 1: Extract structured payloads ──────────────────────────────────
    print("Step 1: Extracting structured payloads...")
    cl01_payload = cl01_extract(html_path_str)
    cl02_payload = cl02_extract(html_path_str)
    cl03_payload = cl03_extract(html_path_str)

    payloads = {"CL01": cl01_payload, "CL02": cl02_payload, "CL03": cl03_payload}

    # Save payloads for inspection
    for name, payload in payloads.items():
        payload_path = output_dir / "payloads" / f"{name.lower()}_payload.json"
        save_json(payload, payload_path)

    cl01_tokens = estimate_tokens(json.dumps(cl01_payload))
    cl02_tokens = estimate_tokens(json.dumps(cl02_payload))
    cl03_tokens = estimate_tokens(json.dumps(cl03_payload))
    print(
        f"  CL01: ~{cl01_tokens:,} tokens | "
        f"CL02: ~{cl02_tokens:,} tokens | "
        f"CL03: ~{cl03_tokens:,} tokens"
    )

    # ── Step 1.5: Apply Pass 1 filters ───────────────────────────────────────
    print("Step 1.5: Applying Pass 1 filters to payloads...")
    filter_flags = build_filter_flags(sem_findings, form_findings, ntext_findings)
    payloads["CL01"] = apply_cl01_filters(payloads["CL01"], filter_flags)
    payloads["CL02"] = apply_cl02_filters(payloads["CL02"], filter_flags)
    payloads["CL03"] = apply_cl03_filters(payloads["CL03"], filter_flags)

    active_filters = [k for k, v in filter_flags.items()
                      if k != "skip_prompts" and v]
    print(f"  Active filters: {active_filters or '(none)'}")
    if filter_flags["skip_prompts"]:
        print(f"  Prompts skipped by filter: {filter_flags['skip_prompts']}")
    if progress_callback:
        progress_callback({
            "type": "progress",
            "stage": "extraction_complete",
            "message": "Content extracted and filtered",
        })

    # ── Step 2: Slice, fill, and call ────────────────────────────────────────
    print("Step 2: Processing prompts...")

    results = []
    skipped = []
    total_input_tokens = 0
    total_output_tokens = 0

    # Count prompts expected to run (for progress reporting)
    total_llm_prompts = sum(
        1 for s in PROMPT_REGISTRY
        if not (s.is_summary and not include_summaries)
        and s.name not in filter_flags["skip_prompts"]
    )
    llm_completed = 0

    # Initialise client once (only needed for live runs)
    client = None
    if not dry_run:
        client = PipelineClient(api_key=api_key, model=model)

    for spec in PROMPT_REGISTRY:
        # Skip summaries unless requested
        if spec.is_summary and not include_summaries:
            skipped.append({"name": spec.name, "reason": "summary (not requested)"})
            continue

        # Skip prompts excluded by Pass 1 filters
        if spec.name in filter_flags["skip_prompts"]:
            skipped.append({"name": spec.name, "reason": "Pass 1 filter (programmatic finding)"})
            print(f"  [{spec.name}] SKIPPED (Pass 1 filter)")
            continue

        # Slice the payload
        slicer_fn = get_slicer(spec.payload_slicer)
        payload_json = slicer_fn(payloads[spec.checklist])

        # Skip if empty
        if spec.skip_if_empty and is_empty_slice(payload_json):
            skipped.append({"name": spec.name, "reason": "empty payload"})
            print(f"  [{spec.name}] SKIPPED (empty payload)")
            continue

        llm_completed += 1

        # Fill the prompt template
        prompt_text = fill_template(spec, payload_json)
        prompt_tokens = estimate_tokens(prompt_text)

        result_entry = {
            "name": spec.name,
            "checklist": spec.checklist,
            "wcag_criteria": spec.wcag_criteria,
            "prompt_tokens_est": prompt_tokens,
        }

        if dry_run:
            # Save just the prompt text
            prompt_dir = output_dir / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            save_json(
                {
                    "prompt_name": spec.name,
                    "checklist": spec.checklist,
                    "wcag_criteria": spec.wcag_criteria,
                    "prompt_index": spec.prompt_index,
                    "prompt_tokens_est": prompt_tokens,
                    "prompt_text": prompt_text,
                    "payload_slice": payload_json,
                },
                prompt_dir / f"{spec.name}.json",
            )
            result_entry["status"] = "dry_run"
            print(f"  [{spec.name}] SAVED prompt (~{prompt_tokens:,} tokens)")
        else:
            # Call the API
            print(f"  [{spec.name}] Calling {model} (~{prompt_tokens:,} tokens)...", end="", flush=True)
            api_result = client.call(prompt_text)

            prompt_dir = output_dir / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            save_json(
                {
                    "prompt_name": spec.name,
                    "checklist": spec.checklist,
                    "wcag_criteria": spec.wcag_criteria,
                    "prompt_index": spec.prompt_index,
                    "prompt_text": prompt_text,
                    "payload_slice": payload_json,
                    "api_result": api_result,
                },
                prompt_dir / f"{spec.name}.json",
            )

            if api_result["success"]:
                in_tok = api_result["usage"]["input_tokens"]
                out_tok = api_result["usage"]["output_tokens"]
                total_input_tokens += in_tok
                total_output_tokens += out_tok
                result_entry["status"] = "success"
                result_entry["input_tokens"] = in_tok
                result_entry["output_tokens"] = out_tok
                result_entry["duration_seconds"] = api_result["duration_seconds"]
                print(
                    f" OK ({api_result['duration_seconds']}s, "
                    f"{in_tok:,} in / {out_tok:,} out)"
                )
            else:
                result_entry["status"] = "error"
                result_entry["error"] = api_result["error"]
                print(f" FAILED: {api_result['error']}")

        results.append(result_entry)
        if progress_callback:
            progress_callback({
                "type": "progress",
                "stage": "llm_progress",
                "completed": llm_completed,
                "total": total_llm_prompts,
                "prompt_name": spec.name,
                "message": f"LLM analysis: {spec.name} ({llm_completed}/{total_llm_prompts})",
            })

    # ── Step 3: Write manifest ───────────────────────────────────────────────
    manifest = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "html_file": str(html_path),
        "model": model,
        "dry_run": dry_run,
        "include_summaries": include_summaries,
        "programmatic_findings_count": len(programmatic_findings),
        "programmatic_findings_by_checker": {
            "CL01_semantic": len(sem_findings),
            "CL02_forms": len(form_findings),
            "CL03_nontext": len(ntext_findings),
        },
        "pass1_filters_active": [
            k for k, v in filter_flags.items()
            if k != "skip_prompts" and v
        ],
        "prompts_executed": [r for r in results if r.get("status") != "dry_run"],
        "prompts_dry_run": [r for r in results if r.get("status") == "dry_run"],
        "prompts_skipped": skipped,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost_usd": compute_cost(total_input_tokens, total_output_tokens, model),
    }
    save_json(manifest, output_dir / "manifest.json")

    return manifest


def print_summary(manifest: dict, show_cost: bool = False) -> None:
    """Print a human-readable summary of the pipeline run."""
    print("\n" + "=" * 60)
    print("PIPELINE RUN SUMMARY")
    print("=" * 60)

    executed = manifest.get("prompts_executed", [])
    dry_run_prompts = manifest.get("prompts_dry_run", [])
    skipped = manifest.get("prompts_skipped", [])

    if manifest["dry_run"]:
        print(f"Mode: DRY RUN (no API calls)")
        print(f"Prompts generated: {len(dry_run_prompts)}")
    else:
        success = [r for r in executed if r.get("status") == "success"]
        failed = [r for r in executed if r.get("status") == "error"]
        print(f"Prompts executed: {len(executed)}")
        print(f"  Successful: {len(success)}")
        print(f"  Failed: {len(failed)}")

        if manifest["total_input_tokens"] > 0:
            print(f"\nToken usage:")
            print(f"  Input:  {manifest['total_input_tokens']:,}")
            print(f"  Output: {manifest['total_output_tokens']:,}")

            if show_cost:
                model = manifest.get("model", "")
                cost = compute_cost(
                    manifest["total_input_tokens"],
                    manifest["total_output_tokens"],
                    model,
                )
                if cost is not None:
                    print(f"\nEstimated cost:")
                    pricing = get_pricing(model)
                    print(f"  Model: {model}")
                    print(f"  Rate:  ${pricing[0]:.2f} / 1M input, ${pricing[1]:.2f} / 1M output")
                    print(f"  Total: ${cost:.4f}")
                else:
                    print(f"\nCost: unknown pricing for model '{model}'")
                    print(f"  Known models: {', '.join(MODEL_PRICING.keys())}")

    print(f"Prompts skipped: {len(skipped)}")
    for s in skipped:
        print(f"  - {s['name']}: {s['reason']}")

    print(f"\nProgrammatic findings: {manifest['programmatic_findings_count']}")
    print(f"Results saved to: {manifest.get('output_dir', 'output/')}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run the element-specific accessibility audit pipeline."
    )
    parser.add_argument(
        "--html", type=str, required=True, help="Path to HTML file to analyze"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./output",
        help="Directory to save results (default: ./output)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-20250514",
        help="Model to use (default: claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate prompts without calling the API",
    )
    parser.add_argument(
        "--include-summaries",
        action="store_true",
        help="Include the three summary prompts (CL01-7, CL02-6, CL03-8)",
    )
    parser.add_argument(
        "--show-cost",
        action="store_true",
        help="Print estimated dollar cost of the run based on model pricing",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help="Path to .env file (default: .env)",
    )
    args = parser.parse_args()

    # Load environment
    load_dotenv(args.env_file)

    from processing_scripts.llm_client.client import is_openai_model, is_gemini_model

    if is_openai_model(args.model):
        api_key = os.getenv("OPENAI_API_KEY")
        key_name = "OPENAI_API_KEY"
    elif is_gemini_model(args.model):
        api_key = os.getenv("GEMINI_API_KEY")
        key_name = "GEMINI_API_KEY"
    else:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        key_name = "ANTHROPIC_API_KEY"

    if not api_key and not args.dry_run:
        print(f"Error: {key_name} not found in environment.")
        print(f"Add it to {args.env_file} or set it as an environment variable.")
        sys.exit(1)

    # Validate HTML path
    html_path = Path(args.html)
    if not html_path.exists():
        print(f"Error: HTML file not found: {html_path}")
        sys.exit(1)

    output_dir = Path(args.output_dir)

    print(f"HTML file: {html_path}")
    print(f"Output dir: {output_dir}")
    print(f"Model: {args.model}")
    print(f"Dry run: {args.dry_run}")
    print(f"Include summaries: {args.include_summaries}")
    print()

    manifest = run_pipeline(
        html_path=html_path,
        output_dir=output_dir,
        api_key=api_key,
        model=args.model,
        dry_run=args.dry_run,
        include_summaries=args.include_summaries,
    )
    manifest["output_dir"] = str(output_dir)
    print_summary(manifest, show_cost=args.show_cost)


if __name__ == "__main__":
    main()
