from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chatbot_bundle import project_root
from .contracts import ProviderApiPayload, ProviderLabel, StageExecutionRequest
from .entity_knowledge import EntityKnowledgeRetriever
from .providers.gpu_local import GpuLocalProviderClient, GpuTokenizationPreflightResult
from .providers.factory import local_controlled_generation_client
from .stage_runner import (
    ChatbotStageRunner,
    _document_header_and_body,
    _prompt_source_completeness,
    _provider_api_inputs,
)


ENTITY_GPU_PREFLIGHT_CONTRACT = "entity-gpu-tokenization-preflight-v2"
ENTITY_CONTEXT_LIMIT = 32_768
ENTITY_MAX_OUTPUT_TOKENS = 4_096
ENTITY_INPUT_BUDGET = ENTITY_CONTEXT_LIMIT - ENTITY_MAX_OUTPUT_TOKENS
ENTITY_INPUT_SAFETY_MARGIN_TOKENS = 1_024
ENTITY_TARGET_PROMPT_TOKENS = ENTITY_INPUT_BUDGET - ENTITY_INPUT_SAFETY_MARGIN_TOKENS


class EntityGenerationDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class EntityGpuPreflightPlan:
    provider_payload: ProviderApiPayload
    report: dict[str, Any]


class EntityGpuPreflightRunner:
    """Entity-specific GPU handoff with generation deliberately unavailable."""

    generation_enabled = False

    def __init__(
        self,
        *,
        retriever: EntityKnowledgeRetriever | None = None,
        client: GpuLocalProviderClient | None = None,
    ) -> None:
        self.retriever = retriever or EntityKnowledgeRetriever()
        self.client = client or local_controlled_generation_client()

    def build_plan(
        self,
        *,
        document: Any,
        stage_outputs: dict[str, Any],
    ) -> EntityGpuPreflightPlan:
        runner = ChatbotStageRunner(entity_knowledge_retriever=self.retriever)
        bundle = runner.manifest.get_stage("entity_registry")
        header, body = _document_header_and_body(document)
        system_prompt, user_prompt, package_provenance = runner._build_prompt_package(
            bundle=bundle,
            document_header=header,
            source_body=body,
            stage_outputs=stage_outputs,
            correction="",
            include_tokenization_diagnostics=True,
        )
        source_completeness = _prompt_source_completeness(
            bundle=bundle,
            loaded_files=package_provenance.get("loaded_files", []),
            package_provenance=package_provenance,
        )
        if not source_completeness.get("source_complete"):
            raise EntityGenerationDisabledError(
                "Entity prompt package is not source-complete; tokenization handoff is blocked."
            )
        request = StageExecutionRequest(
            stage_id="entity_registry",
            stage_label=bundle.stage_label,
            provider=ProviderLabel.GPU_LOCAL.value,
            requested_provider=ProviderLabel.GPU_LOCAL.value,
            document_id=str(getattr(document, "doc_id", "")),
            document_title=str(getattr(document, "title", "")),
            document_type=str(getattr(document, "document_type", "")),
            accepted_upstream_stage_ids=(),
            source_character_count=len(body),
            prompt_character_count=len(system_prompt) + len(user_prompt),
            metadata={
                "manifest": str(runner.manifest.path.relative_to(project_root())),
                "expected_output_type": bundle.expected_output_type,
                "operation": "tokenization_only",
                "generation_enabled": False,
            },
        )
        payload = ProviderApiPayload(
            request=request,
            inputs=_provider_api_inputs(
                document_header=header,
                document_body=body,
                stage_outputs={},
            ),
            prompt_package={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "prompt_character_count": len(system_prompt) + len(user_prompt),
                "loaded_files": package_provenance.get("loaded_files", []),
                "source_completeness": source_completeness,
                "indexed_resources": package_provenance.get("indexed_resources", []),
                "entity_candidate_package": package_provenance.get("entity_candidate_package", {}),
                "prompt_component_summary": package_provenance.get("prompt_component_summary", {}),
                "tokenization_diagnostics": package_provenance.get("tokenization_diagnostics", {}),
                "system_prompt_resolution": package_provenance.get("system_prompt_resolution", {}),
            },
            options={
                "max_output_tokens": ENTITY_MAX_OUTPUT_TOKENS,
                "max_input_tokens": ENTITY_TARGET_PROMPT_TOKENS,
                "required_input_safety_margin_tokens": ENTITY_INPUT_SAFETY_MARGIN_TOKENS,
                "tokenization_only": True,
                "generation_enabled": False,
            },
        )
        prompt_characters = len(system_prompt) + len(user_prompt)
        report = {
            "contract_version": ENTITY_GPU_PREFLIGHT_CONTRACT,
            "stage_id": "entity_registry",
            "document_id": request.document_id,
            "provider": ProviderLabel.GPU_LOCAL.value,
            "operation": "tokenization_only",
            "generation_enabled": False,
            "provider_called": False,
            "prompt_character_count": prompt_characters,
            "prompt_tokens": None,
            "token_count_source": "qwen_tokenizer_preflight_required",
            "context_limit": ENTITY_CONTEXT_LIMIT,
            "input_budget_tokens": ENTITY_INPUT_BUDGET,
            "required_input_safety_margin_tokens": ENTITY_INPUT_SAFETY_MARGIN_TOKENS,
            "target_prompt_tokens": ENTITY_TARGET_PROMPT_TOKENS,
            "max_output_tokens": ENTITY_MAX_OUTPUT_TOKENS,
            "source_completeness": source_completeness,
            "entity_candidate_package": package_provenance.get("entity_candidate_package", {}),
            "prompt_component_summary": package_provenance.get("prompt_component_summary", {}),
            "redacted_provider_payload": payload.as_redacted_dict(),
        }
        return EntityGpuPreflightPlan(provider_payload=payload, report=report)

    def tokenize_only(self, plan: EntityGpuPreflightPlan) -> GpuTokenizationPreflightResult:
        return self.client.tokenize_only(plan.provider_payload.as_dict())

    def generate(self, *_args: Any, **_kwargs: Any) -> None:
        raise EntityGenerationDisabledError(
            "Entity generation is disabled in the tokenization-only runner."
        )
