"""Unit tests for the Bedrock provider (mocked boto3)."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from tagger.services.contracts import ExecutionStatus, ProviderLabel
from tagger.services.providers.aws_bedrock import BedrockProviderClient


def _bedrock_body(text: str, *, in_tokens: int = 10, out_tokens: int = 5) -> dict:
    payload = {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        "stop_reason": "end_turn",
    }
    return {"body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


class BedrockProviderTests(SimpleTestCase):
    def _client_with_mock(self, mock_bedrock) -> BedrockProviderClient:
        client = BedrockProviderClient(model_id="test-model", region="us-east-1")
        client._client = mock_bedrock
        return client

    def test_generate_text_completed(self):
        mock = MagicMock()
        mock.invoke_model.return_value = _bedrock_body("Hello, working.")
        client = self._client_with_mock(mock)

        resp = client.generate_text("sys", "user")

        self.assertEqual(resp.status, ExecutionStatus.COMPLETED.value)
        self.assertEqual(resp.text, "Hello, working.")
        self.assertEqual(resp.provider, ProviderLabel.AWS_BEDROCK.value)
        self.assertTrue(resp.real_chatbot_execution)
        self.assertEqual(
            resp.metadata["provider_provenance"]["input_tokens"], 10
        )

    def test_generate_text_empty_completion_is_validation_failed(self):
        mock = MagicMock()
        mock.invoke_model.return_value = _bedrock_body("   ")
        client = self._client_with_mock(mock)

        resp = client.generate_text("sys", "user")

        self.assertEqual(resp.status, ExecutionStatus.VALIDATION_FAILED.value)

    def test_throttling_is_unavailable(self):
        exc = Exception("throttled")
        exc.response = {"Error": {"Code": "ThrottlingException"}}
        mock = MagicMock()
        mock.invoke_model.side_effect = exc
        client = self._client_with_mock(mock)

        resp = client.generate_text("sys", "user")

        self.assertEqual(resp.status, ExecutionStatus.UNAVAILABLE.value)
        self.assertFalse(resp.real_chatbot_execution)

    def test_access_denied_is_error(self):
        exc = Exception("no access")
        exc.response = {"Error": {"Code": "AccessDeniedException"}}
        mock = MagicMock()
        mock.invoke_model.side_effect = exc
        client = self._client_with_mock(mock)

        resp = client.generate_text("sys", "user")

        self.assertEqual(resp.status, ExecutionStatus.ERROR.value)

    def test_entity_generate_completed(self):
        mock = MagicMock()
        mock.invoke_model.return_value = _bedrock_body("P: Smith [P-1]")
        client = self._client_with_mock(mock)

        result = client.generate(
            {"prompt_package": {"system_prompt": "s", "user_prompt": "u"}}
        )

        self.assertEqual(result.status, ExecutionStatus.COMPLETED.value)
        self.assertEqual(result.raw_output, "P: Smith [P-1]")
        self.assertTrue(result.real_chatbot_execution)

    def test_health_ok(self):
        client = BedrockProviderClient(model_id="test-model", region="us-east-1")
        health = client.health()
        self.assertTrue(health.ok)
        self.assertEqual(health.payload["status"], "ok")

    def test_tokenize_only_estimates_without_call(self):
        mock = MagicMock()
        client = self._client_with_mock(mock)

        tok = client.tokenize_only(
            {"prompt_package": {"system_prompt": "abcd", "user_prompt": "efgh"}}
        )

        self.assertEqual(tok.status, ExecutionStatus.COMPLETED.value)
        # 8 chars / 4 = 2 tokens
        self.assertEqual(tok.payload["token_counts"]["prompt_tokens"], 2)
        self.assertFalse(tok.provenance["model_call_attempted"])
        mock.invoke_model.assert_not_called()
