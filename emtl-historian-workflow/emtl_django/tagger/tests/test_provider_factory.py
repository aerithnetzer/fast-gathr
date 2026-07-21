from unittest.mock import patch

from django.test import SimpleTestCase

from tagger.services.providers.aws_bedrock import BedrockProviderClient
from tagger.services.providers.factory import (
    ProviderIntegrationRequired,
    stage_generation_client,
)
from tagger.services.providers.gpu_local import GpuLocalProviderClient


class StageProviderFactoryTests(SimpleTestCase):
    def test_local_provider_is_selected_from_one_environment_variable(self):
        with patch.dict("os.environ", {"EMTL_STAGE_PROVIDER": "gpu_local"}):
            self.assertIsInstance(stage_generation_client(), GpuLocalProviderClient)

    def test_unconfigured_provider_fails_with_integration_instruction(self):
        with patch.dict("os.environ", {"EMTL_STAGE_PROVIDER": "unconfigured"}):
            with self.assertRaisesRegex(ProviderIntegrationRequired, "No live stage provider"):
                stage_generation_client()

    def test_aws_bedrock_selection_returns_bedrock_client(self):
        with patch.dict("os.environ", {"EMTL_STAGE_PROVIDER": "aws_bedrock"}):
            self.assertIsInstance(stage_generation_client(), BedrockProviderClient)

    def test_external_api_selection_still_requires_integration(self):
        with patch.dict("os.environ", {"EMTL_STAGE_PROVIDER": "external_api"}):
            with self.assertRaisesRegex(ProviderIntegrationRequired, "providers/factory.py"):
                stage_generation_client()
