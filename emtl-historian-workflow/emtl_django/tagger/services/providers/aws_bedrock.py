"""AWS Bedrock stage-generation provider.

Implements both provider interfaces used by the workflow:

* ``generate_text(system_prompt, user_prompt) -> ProviderResponse`` — the simple
  interface used by the generic stage runner for the five non-Entity stages.
* ``generate(provider_payload) -> BedrockProviderResult`` plus ``health()`` and
  ``tokenize_only()`` — the richer interface used by the Entity Registry runner.

Targets Anthropic Claude on Bedrock via the Messages API. On any AWS/botocore
error the client returns an ``unavailable``/``error`` result rather than
raising, so callers degrade gracefully.

Token usage (input/output) is surfaced in provenance for cost auditing.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..contracts import (
    ExecutionStatus,
    ProviderLabel,
    STAGE_CONTRACT_VERSION,
    STAGE_EXECUTION_RESULT_SCHEMA,
)
from .bedrock_dataclasses import (
    BedrockProviderHealthResult,
    BedrockProviderResult,
    BedrockTokenizationPreflightResult,
)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.1
DEFAULT_REGION = "us-east-1"
ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"

# Rough token estimate for preflight when no real tokenizer is available.
# Anthropic guidance is ~3.5-4 chars/token for English; 4 is conservative.
_CHARS_PER_TOKEN = 4


class BedrockProviderClient:
    provider_name = ProviderLabel.AWS_BEDROCK.value

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("EMTL_BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID
        self.region = region or os.getenv("AWS_REGION") or DEFAULT_REGION
        self.max_tokens = int(
            max_tokens or os.getenv("EMTL_BEDROCK_MAX_TOKENS") or DEFAULT_MAX_TOKENS
        )
        self.temperature = float(
            temperature
            if temperature is not None
            else os.getenv("EMTL_BEDROCK_TEMPERATURE") or DEFAULT_TEMPERATURE
        )
        self._client = None  # lazy; lets tests inject a mock

    # ── Bedrock client (lazy) ────────────────────────────────────────────────

    def _bedrock(self):
        if self._client is None:
            import boto3  # imported lazily so the module loads without boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    # ── Low-level invoke ─────────────────────────────────────────────────────

    def _invoke(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Call Bedrock and return a normalized dict:
        {status, text, input_tokens, output_tokens, stop_reason, error, code}.
        Never raises for AWS-side failures.
        """
        body = {
            "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt.strip():
            body["system"] = system_prompt

        try:
            response = self._bedrock().invoke_model(
                modelId=self.model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:  # botocore ClientError / BotoCoreError / etc.
            code = type(exc).__name__
            aws_code = ""
            resp = getattr(exc, "response", None)
            if isinstance(resp, dict):
                aws_code = str((resp.get("Error") or {}).get("Code") or "")
            # Throttling / model-not-ready are "unavailable"; access/validation
            # are "error" (config problems the operator must fix).
            unavailable = aws_code in {
                "ThrottlingException",
                "ServiceUnavailableException",
                "ModelNotReadyException",
                "ModelTimeoutException",
            }
            return {
                "status": ExecutionStatus.UNAVAILABLE.value
                if unavailable
                else ExecutionStatus.ERROR.value,
                "text": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "stop_reason": "",
                "error": f"{aws_code or code}: {exc}",
                "code": aws_code or code,
            }

        try:
            data = json.loads(response["body"].read())
        except (KeyError, ValueError) as exc:
            return {
                "status": ExecutionStatus.ERROR.value,
                "text": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "stop_reason": "",
                "error": f"malformed Bedrock response: {exc}",
                "code": "malformed_provider_response",
            }

        # Anthropic Messages response shape: {content:[{type:text,text:..}], usage:{..}}
        content = data.get("content") or []
        text = ""
        if isinstance(content, list):
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        usage = data.get("usage") or {}
        return {
            "status": ExecutionStatus.COMPLETED.value,
            "text": text,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "stop_reason": str(data.get("stop_reason") or ""),
            "error": "",
            "code": "",
        }

    def _provenance(self, invoke: dict[str, Any], *, operation: str) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "model": self.model_id,
            "region": self.region,
            "operation": operation,
            "real_chatbot_execution": invoke["status"] == ExecutionStatus.COMPLETED.value,
            "input_tokens": invoke["input_tokens"],
            "output_tokens": invoke["output_tokens"],
            "stop_reason": invoke["stop_reason"],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    # ── Interface A: simple generate_text (five non-Entity stages) ───────────

    def generate_text(self, system_prompt: str, user_prompt: str):
        """Return a stage_runner.ProviderResponse-compatible object."""
        from ..stage_runner import ProviderResponse

        invoke = self._invoke(system_prompt, user_prompt)
        provenance = self._provenance(invoke, operation="stage_generation")
        if invoke["status"] != ExecutionStatus.COMPLETED.value:
            return ProviderResponse(
                status=invoke["status"],
                text="",
                provider=self.provider_name,
                model=self.model_id,
                error=invoke["error"],
                errors=[{"code": invoke["code"], "message": invoke["error"]}],
                metadata={"provider_provenance": provenance},
                real_chatbot_execution=False,
            )
        if not invoke["text"].strip():
            return ProviderResponse(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                text="",
                provider=self.provider_name,
                model=self.model_id,
                error="Bedrock returned an empty completion.",
                errors=[{"code": "empty_completion", "message": "empty completion"}],
                metadata={"provider_provenance": provenance},
                real_chatbot_execution=True,
            )
        return ProviderResponse(
            status=ExecutionStatus.COMPLETED.value,
            text=invoke["text"],
            provider=self.provider_name,
            model=self.model_id,
            payload={},
            metadata={"provider_provenance": provenance},
            real_chatbot_execution=True,
        )

    # ── Interface B: rich client (Entity Registry) ───────────────────────────

    def generate(self, provider_payload: dict[str, Any]) -> BedrockProviderResult:
        prompt_package = provider_payload.get("prompt_package") or {}
        system_prompt = str(prompt_package.get("system_prompt") or "")
        user_prompt = str(prompt_package.get("user_prompt") or "")
        invoke = self._invoke(system_prompt, user_prompt)
        provenance = self._provenance(invoke, operation="entity_registry_generation")

        if invoke["status"] != ExecutionStatus.COMPLETED.value:
            return BedrockProviderResult(
                status=invoke["status"],
                provider=self.provider_name,
                model=self.model_id,
                error=invoke["error"],
                errors=[{"code": invoke["code"], "message": invoke["error"]}],
                metadata={"provider_provenance": provenance},
                real_chatbot_execution=False,
            )
        if not invoke["text"].strip():
            return BedrockProviderResult(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                provider=self.provider_name,
                model=self.model_id,
                error="Bedrock returned an empty Entity completion.",
                errors=[{"code": "empty_completion", "message": "empty completion"}],
                metadata={"provider_provenance": provenance},
                real_chatbot_execution=True,
            )
        return BedrockProviderResult(
            status=ExecutionStatus.COMPLETED.value,
            raw_output=invoke["text"],
            provider=self.provider_name,
            model=self.model_id,
            payload={},
            metadata={
                "provider_response_schema": STAGE_EXECUTION_RESULT_SCHEMA,
                "provider_contract_version": STAGE_CONTRACT_VERSION,
                "provider_provenance": provenance,
            },
            real_chatbot_execution=True,
        )

    def health(self) -> BedrockProviderHealthResult:
        """Report reachability. Bedrock is a managed service; we assert config
        presence and let the first invoke surface real access errors. Kept
        lightweight to avoid a per-request probe call (and its cost)."""
        return BedrockProviderHealthResult(
            ok=True,
            payload={
                "status": "ok",
                "provider": self.provider_name,
                "model": self.model_id,
                "region": self.region,
                "server_mode": "aws_bedrock_managed",
            },
        )

    def tokenize_only(
        self, provider_payload: dict[str, Any]
    ) -> BedrockTokenizationPreflightResult:
        """Estimate prompt tokens without a model call. Bedrock exposes no
        standalone tokenizer, so this is a char-based heuristic used only to
        record prompt size; it never contacts the model."""
        prompt_package = provider_payload.get("prompt_package") or {}
        system_prompt = str(prompt_package.get("system_prompt") or "")
        user_prompt = str(prompt_package.get("user_prompt") or "")
        char_count = len(system_prompt) + len(user_prompt)
        prompt_tokens = (char_count + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
        return BedrockTokenizationPreflightResult(
            status=ExecutionStatus.COMPLETED.value,
            provider=self.provider_name,
            model=self.model_id,
            payload={
                "token_counts": {
                    "prompt_tokens": prompt_tokens,
                    "estimated": True,
                    "chars_per_token": _CHARS_PER_TOKEN,
                }
            },
            validation={
                "prompt_truncated": False,
                "generation_enabled": False,
                "estimate_only": True,
            },
            provenance={
                "model_call_attempted": False,
                "model_loaded_for_request": False,
                "tokenizer": "char-heuristic",
            },
        )
