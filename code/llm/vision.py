"""Screenshot transcription with a vision model, cached on disk.

The model only transcribes what is on screen into a fixed JSON schema
(structured outputs). Interpreting the transcription (identifiers, states,
categories) is deterministic code in audit/evidence/screenshots.py.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from llm.cache import JsonCache, UsageLedger, UsageRecord, cache_key

PROMPT_PATH = Path(__file__).parent / "prompts" / "screenshot_extraction.md"
PROMPT_VERSION = "screenshot-v1"
DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV = "DELETION_AUDIT_VISION_MODEL"

_PAIR = {
    "type": "object",
    "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
    "required": ["field", "value"],
    "additionalProperties": False,
}
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "page_title": {"type": "string"},
        "application": {"type": "string"},
        "captured_at_text": _NULLABLE_STRING,
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": _NULLABLE_STRING,
                    "label": _NULLABLE_STRING,
                    "kind_hint": {"type": "string"},
                    "status_text": _NULLABLE_STRING,
                    "identifiers": {"type": "array", "items": _PAIR},
                    "fields": {"type": "array", "items": _PAIR},
                    "related_references": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["resource_id", "label", "kind_hint", "status_text", "identifiers",
                             "fields", "related_references"],
                "additionalProperties": False,
            },
        },
        "other_identifiers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["page_title", "application", "captured_at_text", "records",
                 "other_identifiers"],
    "additionalProperties": False,
}


class VisionUnavailable(Exception):
    """No cached transcription and no way to call the model."""


class VisionError(Exception):
    """The model call failed or returned something unusable."""


def validate(result) -> dict:
    """Minimal structural check of a transcription (the API enforces the schema;
    this guards cached files and test doubles)."""
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        raise VisionError("transcription is not an object with a records list")
    for record in result["records"]:
        if not isinstance(record, dict):
            raise VisionError("a record is not an object")
        for key in ("identifiers", "fields", "related_references"):
            if not isinstance(record.get(key, []), list):
                raise VisionError(f"record field {key} is not a list")
    return result


class ScreenshotTranscriber:
    def __init__(self, cache: JsonCache | None = None, usage: UsageLedger | None = None,
                 client=None, model: str | None = None, allow_api: bool = True):
        self.cache = cache or JsonCache(namespace="screenshots")
        self.usage = usage if usage is not None else UsageLedger()
        self.model = model or os.environ.get(MODEL_ENV, DEFAULT_MODEL)
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.allow_api = allow_api
        self._client = client

    def key(self, image: bytes) -> str:
        return cache_key(PROMPT_VERSION, self.model, self.prompt, image)

    def transcribe(self, image: bytes, subject: str = "") -> dict:
        key = self.key(image)
        cached = self.cache.get(key)
        if cached is not None:
            self.usage.add(UsageRecord("screenshot_extraction", cached.get("model", self.model),
                                       0, 0, cache_hit=True, subject=subject))
            return validate(cached["result"])
        if not self.allow_api:
            raise VisionUnavailable("no cached transcription and API calls are disabled")
        client = self._client or self._make_client()
        result, usage = self._call(client, image)
        self.usage.add(UsageRecord("screenshot_extraction", self.model, usage["input_tokens"],
                                   usage["output_tokens"], cache_hit=False, subject=subject))
        self.cache.put(key, {"model": self.model, "prompt_version": PROMPT_VERSION,
                             "subject": subject, "usage": usage, "result": result})
        return result

    def _make_client(self):
        try:
            import anthropic
        except ImportError:
            raise VisionUnavailable("the 'anthropic' package is not installed") from None
        try:
            # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
            self._client = anthropic.Anthropic(max_retries=3, timeout=300.0)
        except Exception as exc:  # the SDK raises if no credential source exists
            raise VisionUnavailable(f"no Anthropic credentials available ({exc})") from None
        return self._client

    def _call(self, client, image: bytes) -> tuple[dict, dict]:
        try:
            response = self._request(client, image)
        except (VisionError, VisionUnavailable):
            raise
        except Exception as exc:
            raise _map_api_error(exc) from None
        return _parse_response(response)

    def _request(self, client, image: bytes):
        # fallbacks="default" re-runs a policy-declined request on Anthropic's
        # recommended fallback model inside the same call.
        return client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.standard_b64encode(image).decode("ascii")}},
                {"type": "text", "text": self.prompt},
            ]}],
        )


def _map_api_error(exc: Exception) -> Exception:
    """Translate SDK errors, most specific first. Works without the SDK installed
    (an injected test client), where any failure is a VisionError."""
    try:
        import anthropic
    except ImportError:
        return VisionError(f"request failed ({exc})")
    if isinstance(exc, anthropic.AuthenticationError):
        return VisionUnavailable(f"authentication failed ({exc.message})")
    if isinstance(exc, anthropic.RateLimitError):
        return VisionError(f"rate limited after retries ({exc.message})")
    if isinstance(exc, anthropic.APIStatusError):
        return VisionError(f"API error {exc.status_code}: {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return VisionError(f"connection failed ({exc})")
    return VisionError(f"request failed ({type(exc).__name__}: {exc})")


def _parse_response(response) -> tuple[dict, dict]:
    if response.stop_reason == "refusal":
        raise VisionError("the model declined to transcribe this image")
    if response.stop_reason == "max_tokens":
        raise VisionError("transcription was cut off at max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise VisionError("response contained no text block")
    try:
        result = validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise VisionError(f"response was not valid JSON ({exc.msg})") from None
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    return result, usage
