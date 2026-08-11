"""
llm_client — LLM API clients for the VisionAid accessibility audit pipeline.

Supports both Anthropic (Claude) and OpenAI (GPT) models.

Usage
-----
    from llm_client import create_audit_client, load_all_prompts, run_all

    client = create_audit_client(model="claude-sonnet-5")   # or "gpt-4o"
    prompts = load_all_prompts(PROMPTS_DIR)
    report  = run_all(client, prompts, all_slices)
"""

from .client import AuditClient, OpenAIAuditClient, create_audit_client, is_openai_model
from .prompt_loader import load_prompts, load_all_prompts
from .runner import run_checklist, run_all

__all__ = [
    "AuditClient",
    "OpenAIAuditClient",
    "create_audit_client",
    "is_openai_model",
    "load_prompts",
    "load_all_prompts",
    "run_checklist",
    "run_all",
]
