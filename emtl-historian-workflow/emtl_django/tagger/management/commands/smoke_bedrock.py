"""Smoke-test the Bedrock provider with a fixed prompt.

Usage:
    python manage.py smoke_bedrock

Exits 0 on a successful completion, 1 otherwise. Prints the model, region,
token usage, and the completion text. Use this to confirm IAM permissions,
model access, and region before running a real stage through the UI.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from tagger.services.providers.aws_bedrock import BedrockProviderClient


class Command(BaseCommand):
    help = "Invoke Bedrock with a canned prompt to verify connectivity/access."

    def handle(self, *args, **options):
        client = BedrockProviderClient()
        self.stdout.write(f"model:  {client.model_id}")
        self.stdout.write(f"region: {client.region}")

        response = client.generate_text(
            system_prompt="You are a terse assistant.",
            user_prompt="Reply with exactly one short sentence confirming you are working.",
        )

        prov = (response.metadata or {}).get("provider_provenance") or {}
        self.stdout.write(f"status: {response.status}")
        self.stdout.write(
            f"tokens: in={prov.get('input_tokens')} out={prov.get('output_tokens')}"
        )
        if response.status == "completed":
            self.stdout.write(self.style.SUCCESS(f"completion: {response.text}"))
            return
        self.stdout.write(self.style.ERROR(f"error: {response.error}"))
        raise SystemExit(1)
