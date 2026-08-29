"""
AuditClient — thin wrappers around the Anthropic and OpenAI APIs.

Loads API keys from the project root .env file automatically.
"""

import json
import os
from pathlib import Path

import anthropic
import openai
from dotenv import load_dotenv
from google import genai

# Project root is two levels up from this file (processing_scripts/llm_client/)
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_FILE)

# OpenAI model prefixes used to detect provider from model ID
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4")

# Gemini model prefixes used to detect provider from model ID
_GEMINI_PREFIXES = ("gemini-",)


def is_openai_model(model: str) -> bool:
    """Return True if *model* looks like an OpenAI model ID."""
    return model.startswith(_OPENAI_PREFIXES)


def is_gemini_model(model: str) -> bool:
    """Return True if *model* looks like a Gemini model ID."""
    return model.startswith(_GEMINI_PREFIXES)


# Claude models (Opus 4.6+, Sonnet 4.6+, Fable/Mythos 5) that reject
# temperature/top_p/top_k (400 error) in favor of adaptive thinking + effort.
_NO_SAMPLING_PARAMS_MODELS = {
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}


def supports_temperature(model: str) -> bool:
    """Return False for Claude models that reject the `temperature` param."""
    return model not in _NO_SAMPLING_PARAMS_MODELS


class AuditClient:
    """
    Sends filled accessibility-audit prompts to the Claude API and returns
    parsed JSON responses.

    Parameters
    ----------
    model : str
        Claude model ID to use (default: claude-sonnet-5). Also accepts
        ``claude-opus-5`` and ``claude-fable-5``, Anthropic's most capable
        model. Newer Claude models (Opus 4.6+, Sonnet 4.6+, Fable/Mythos 5)
        reject a ``temperature`` param entirely (see ``supports_temperature``),
        which this client already accounts for.
    temperature : float
        Sampling temperature (default: 0.1 for consistent structured output).
        Silently omitted from the request for models that reject it.
    max_tokens : int
        Maximum tokens to generate per response (default: 8192).
    """

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                f"ANTHROPIC_API_KEY not found. Expected in {_ENV_FILE}"
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def call(self, prompt_text: str, payload: dict) -> tuple[dict, dict]:
        """
        Fill `{payload}` in *prompt_text* with JSON-serialised *payload*,
        send to the API, and return ``(parsed_response, usage)``.

        The response is expected to be a JSON object (possibly wrapped in a
        markdown code fence, which is stripped automatically).

        Returns
        -------
        response_json : dict
            Parsed JSON from the model's reply.
        usage : dict
            ``{"input_tokens": int, "output_tokens": int}``
        """
        filled = prompt_text.replace("{payload}", json.dumps(payload, separators=(",", ":")))

        request_kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": filled}],
        }
        if supports_temperature(self.model):
            request_kwargs["temperature"] = self.temperature

        message = self._client.messages.create(**request_kwargs)

        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }

        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()

        return json.loads(raw), usage


class OpenAIAuditClient:
    """
    Sends filled accessibility-audit prompts to the OpenAI API and returns
    parsed JSON responses.  Same interface as :class:`AuditClient`.

    Parameters
    ----------
    model : str
        OpenAI model ID to use (default: gpt-4o).
    temperature : float
        Sampling temperature (default: 0.1 for consistent structured output).
    max_tokens : int
        Maximum tokens to generate per response (default: 8192).
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                f"OPENAI_API_KEY not found. Expected in {_ENV_FILE}"
            )
        self._client = openai.OpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def call(self, prompt_text: str, payload: dict) -> tuple[dict, dict]:
        """
        Fill `{payload}` in *prompt_text* with JSON-serialised *payload*,
        send to the API, and return ``(parsed_response, usage)``.

        Returns
        -------
        response_json : dict
            Parsed JSON from the model's reply.
        usage : dict
            ``{"input_tokens": int, "output_tokens": int}``
        """
        filled = prompt_text.replace("{payload}", json.dumps(payload, separators=(",", ":")))

        if self.model.startswith("gpt-5"):
            response = self._client.responses.create(
                model=self.model,
                input=filled,
                max_output_tokens=self.max_tokens,
                reasoning={"effort": "low"},
            )
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            raw = response.output_text.strip()
        else:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[{"role": "user", "content": filled}],
            )
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }
            raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()

        return json.loads(raw), usage


class GeminiAuditClient:
    """
    Sends filled accessibility-audit prompts to the Gemini API and returns
    parsed JSON responses.  Same interface as :class:`AuditClient`.

    Parameters
    ----------
    model : str
        Gemini model ID to use (default: gemini-flash-latest). Prefer the
        ``-latest`` aliases over dated model IDs (e.g. ``gemini-2.5-flash``),
        which Google periodically retires for new API keys.
    temperature : float
        Sampling temperature (default: 0.1 for consistent structured output).
    max_tokens : int
        Maximum tokens to generate per response (default: 8192).
    """

    def __init__(
        self,
        model: str = "gemini-flash-latest",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                f"GEMINI_API_KEY not found. Expected in {_ENV_FILE}"
            )
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def call(self, prompt_text: str, payload: dict) -> tuple[dict, dict]:
        """
        Fill `{payload}` in *prompt_text* with JSON-serialised *payload*,
        send to the API, and return ``(parsed_response, usage)``.

        Returns
        -------
        response_json : dict
            Parsed JSON from the model's reply.
        usage : dict
            ``{"input_tokens": int, "output_tokens": int}``
        """
        filled = prompt_text.replace("{payload}", json.dumps(payload, separators=(",", ":")))

        response = self._client.models.generate_content(
            model=self.model,
            contents=filled,
            config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            },
        )

        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
        }

        raw = response.text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()

        return json.loads(raw), usage


def create_audit_client(
    model: str = "claude-sonnet-5",
    temperature: float = 0.1,
    max_tokens: int = 8192,
) -> AuditClient | OpenAIAuditClient | GeminiAuditClient:
    """Factory that returns the right client class based on the model ID."""
    if is_openai_model(model):
        return OpenAIAuditClient(model=model, temperature=temperature, max_tokens=max_tokens)
    if is_gemini_model(model):
        return GeminiAuditClient(model=model, temperature=temperature, max_tokens=max_tokens)
    return AuditClient(model=model, temperature=temperature, max_tokens=max_tokens)
