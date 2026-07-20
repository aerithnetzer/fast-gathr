from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from tagger.models import Clause, Document, StageExecutionAttempt, StageOutput
from tagger.services.contracts import ProviderApiPayload, StageExecutionRequest
from tagger.services.entity_generation import (
    EntityControlledGenerationError,
    EntityControlledGenerationRunner,
)
from tagger.services.entity_registry import ENTITY_REGISTRY_RESOURCES
from tagger.services.providers.gpu_local import (
    GpuLocalProviderResult,
    GpuProviderHealthResult,
    GpuReadinessDiagnosticResult,
    GpuTokenizationPreflightResult,
)
from tagger.services.stage_runner import ChatbotStageRunner


HEADER = "DocID: controlled-entity-test\nDocument Type: deposition\n<END>"
BODY = "The advocate was alive in London and called an accomplice."
VALID_RAW = (
    HEADER
    + "\nSI: Advocate [SI-0368] | Trigger: advocate | Attribute: Alive [A-0005]"
    + "\nL: London [L-0150] | Trigger: London"
    + "\nTAGGER NOTES & QUESTIONS\nNotes\n- controlled structural test"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _component_hashes() -> dict[str, str]:
    return {
        "system_prompt": _sha("SYSTEM"),
        "instructions": _sha("INSTRUCTIONS"),
        "legal_boilerplate": _sha("LEGAL"),
        "candidate_model_visible": _sha("CANDIDATES"),
        "document_header": _sha(HEADER),
        "document_body": _sha(BODY),
    }


def _provider_payload() -> ProviderApiPayload:
    registry_hashes = {item.name: item.sha256 for item in ENTITY_REGISTRY_RESOURCES}
    component_hashes = _component_hashes()
    prompt_package = {
        "system_prompt": "SYSTEM",
        "user_prompt": "INSTRUCTIONS\nLEGAL\nCANDIDATES\n" + HEADER + "\n" + BODY,
        "prompt_character_count": len("SYSTEM")
        + len("INSTRUCTIONS\nLEGAL\nCANDIDATES\n" + HEADER + "\n" + BODY),
        "loaded_files": [
            {
                "path": name,
                "sha256": _sha(name),
                "characters_loaded": 10,
                "characters_available": 10,
                "truncated": False,
            }
            for name in ("system", "instructions", "legal")
        ],
        "source_completeness": {
            "source_complete": True,
            "candidate_package_complete": True,
            "resource_access_mode": "full_index_plus_bounded_candidates",
            "indexed_resource_count": 3,
        },
        "indexed_resources": [
            {
                "source_file": item.name,
                "source_hash": item.sha256,
                "fully_indexed": True,
            }
            for item in ENTITY_REGISTRY_RESOURCES
        ],
        "entity_candidate_package": {
            "registry_version": "registry-test-version",
            "resource_hashes": registry_hashes,
            "source_complete": True,
            "provenance_complete": True,
            "selection_policy_complete": True,
            "mandatory_candidates_retained": True,
            "candidate_package_complete": True,
            "vector_layer_enabled": False,
            "candidate_count": 3,
            "watch_candidate_count": 1,
            "candidate_provenance": [
                {
                    "candidate_key": "C0001",
                    "source_provenance": {
                        "source_file": "Entity_List.xlsx",
                        "source_hash": registry_hashes["Entity_List.xlsx"],
                        "registry_version": "registry-test-version",
                        "sheet": "Entity",
                        "row": 2,
                    },
                }
            ],
        },
        "prompt_component_summary": {
            "components": [
                {"name": name, "sha256": digest}
                for name, digest in component_hashes.items()
            ]
        },
        "tokenization_diagnostics": {
            "contract_version": "entity-tokenizer-component-comparison-v2"
        },
    }
    request = StageExecutionRequest(
        stage_id="entity_registry",
        stage_label="Entity Registry",
        provider="gpu_local",
        requested_provider="gpu_local",
        document_id="controlled-entity-test",
    )
    return ProviderApiPayload(
        request=request,
        inputs={"document_header": HEADER, "document_body": BODY, "upstream_outputs": {}},
        prompt_package=prompt_package,
        options={
            "max_output_tokens": 4096,
            "max_input_tokens": 27648,
            "required_input_safety_margin_tokens": 1024,
            "tokenization_only": True,
            "generation_enabled": False,
        },
    )


class FakePreflightRunner:
    def build_plan(self, *, document, stage_outputs):
        del document, stage_outputs
        return SimpleNamespace(provider_payload=_provider_payload())


class FakeClient:
    def __init__(self, *, tokenization=None, generation=None, health=None) -> None:
        self.health_result = health or GpuProviderHealthResult(
            ok=True,
            payload={
                "status": "ok",
                "provider_server_version": "test-provider-version",
                "server_mode": "transformers_local",
                "diagnostics": {
                    "model_path": "/models/Qwen2.5-32B-Instruct",
                    "allow_cpu_offload": False,
                    "device_map_profile": "qwen2_5_32b_long_context_v2",
                    "post_load_gate_contract": "snapshot-complete-v2",
                    "generation_ready_gate_contract": "token-aware-kv-v1",
                    "generation_runtime_margin_mib": 4096,
                    "vram_reserve_gib": 4.0,
                    "cuda_allocator": {
                        "configured_value": "expandable_segments:True",
                        "torch_cuda_support_confirmed": True,
                    },
                    "runtime": {
                        "model_loaded": True,
                        "readiness_evidence": _readiness_evidence(),
                    },
                },
            },
        )
        self.tokenization_result = tokenization or _valid_tokenization()
        self.generation_result = generation or _generation_result(VALID_RAW)
        self.health_calls = 0
        self.tokenize_calls = 0
        self.generate_calls = 0
        self.call_order = []

    def health(self):
        self.health_calls += 1
        self.call_order.append("health")
        return self.health_result

    def tokenize_only(self, payload):
        del payload
        self.tokenize_calls += 1
        self.call_order.append("tokenize")
        return self.tokenization_result

    def generate(self, payload):
        del payload
        self.generate_calls += 1
        self.call_order.append("generate")
        return self.generation_result


def _valid_tokenization() -> GpuTokenizationPreflightResult:
    component_hashes = _component_hashes()
    return GpuTokenizationPreflightResult(
        status="completed",
        provider="gpu_local",
        model="Qwen2.5-32B-Instruct",
        payload={
            "operation": "tokenization_only",
            "token_counts": {
                "prompt_tokens": 22417,
                "max_output_tokens": 4096,
                "model_context_limit": 32768,
                "target_prompt_tokens": 27648,
                "required_input_safety_margin_tokens": 1024,
                "actual_input_safety_margin_tokens": 6255,
            },
            "prompt": {"source_complete": True},
            "component_comparison": {
                "current": {
                    "label": "entity-bounded-knowledge-v2",
                    "prompt_tokens": 22417,
                    "components": [
                        {"name": name, "sha256": digest}
                        for name, digest in component_hashes.items()
                    ],
                }
            },
        },
        validation={
            "prompt_integrity_valid": True,
            "context_limit_respected": True,
            "target_prompt_budget_respected": True,
            "required_input_safety_margin_respected": True,
            "prompt_truncated": False,
            "generation_enabled": False,
        },
        provenance={
            "model_call_attempted": False,
            "model_loaded_for_request": False,
        },
    )


def _readiness_evidence() -> dict:
    gate = {
        "passed": True,
        "runtime_margin_mib": 4096,
        "prompt_tokens": 22417,
        "max_output_tokens": 4096,
        "same_process_reclaimable_estimate_used_for_decision": False,
    }
    common = {
        "status": "completed",
        "passed": True,
        "provider_server_version": "test-provider-version",
        "device_map_profile": "qwen2_5_32b_long_context_v2",
        "prompt_tokens": 22417,
        "max_output_tokens": 4096,
        "business_data_used": False,
        "post_load_vram_gate": {"passed": True},
        "generation_ready_vram_gate": gate,
        "generation_ready_vram_gate_cleanup": {"passed": True},
    }
    return {
        "load_only": {
            **common,
            "operation": "load_only",
            "model_call_attempted": False,
            "model_call_completed": False,
        },
        "synthetic_long_context": {
            **common,
            "operation": "synthetic_long_context",
            "model_call_attempted": True,
            "model_call_completed": True,
            "post_diagnostic_cleanup_ran": True,
            "post_synthetic_cleanup": {"passed": True},
            "post_cleanup_generation_ready_vram_gate": gate,
        },
    }


def _generation_result(raw_output: str) -> GpuLocalProviderResult:
    return GpuLocalProviderResult(
        status="completed",
        raw_output=raw_output,
        provider="gpu_local",
        model="Qwen2.5-32B-Instruct",
        payload={
            "diagnostics": {
                "token_counts": {
                    "prompt_tokens": 22417,
                    "generated_tokens": 200,
                    "max_new_tokens": 4096,
                },
                "timing": {"generation_attempt_seconds": 12.5, "request_seconds": 15.0},
                "completion_evidence": {
                    "finish_reason": "eos_token",
                    "eos_token_emitted": True,
                    "hit_max_new_tokens": False,
                    "generation_may_be_truncated": False,
                },
                "post_load_vram_gate": {
                    "passed": True,
                    "contract": "snapshot-complete-v2",
                },
                "generation_ready_vram_gate": {
                    "passed": True,
                    "contract": "token-aware-kv-v1",
                    "runtime_margin_mib": 4096,
                },
                "generation_ready_vram_gate_cleanup": {
                    "passed": True,
                    "contract": "synchronize-empty-cache-remeasure-v1",
                },
            }
        },
        validation={
            "prompt_truncated": False,
            "source_complete": True,
            "model_call_attempted": True,
            "model_call_completed": True,
        },
        metadata={
            "provider_provenance": {
                "real_chatbot_execution": True,
                "model_call_attempted": True,
                "model_call_completed": True,
                "request_lifecycle": {"state": "completed"},
                "model_config": {
                    "provider_server_version": "test-provider-version",
                    "device_map_profile": "qwen2_5_32b_long_context_v2",
                    "vram_reserve_gib": 4.0,
                    "generation_runtime_margin_mib": 4096,
                    "cuda_allocator": {
                        "configured_value": "expandable_segments:True"
                    },
                    "post_load_gate_contract": "snapshot-complete-v2",
                    "generation_ready_gate_contract": "token-aware-kv-v1",
                },
            }
        },
        real_chatbot_execution=True,
    )


class EntityControlledGenerationTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(
            doc_id="controlled-entity-test",
            title="Controlled Entity test",
            document_type="deposition",
            metadata={"working_source_text": HEADER + "\n" + BODY},
        )
        self.entity_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            status=StageOutput.Status.LOADED,
            display_title="Entity Registry",
            payload={"before": True},
            raw_output="before",
            provenance={"before": True},
        )
        self.clause_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.ACCEPTED,
            display_title="Clause Parser",
        )
        Clause.objects.create(
            document=self.document,
            clause_id="CLAUSE 001",
            text=BODY,
            sequence=1,
        )

    def runner(self, client: FakeClient) -> EntityControlledGenerationRunner:
        return EntityControlledGenerationRunner(
            preflight_runner=FakePreflightRunner(),
            client=client,
            expected_provider_version="test-provider-version",
        )

    def test_generation_without_confirmation_is_refused_before_network(self) -> None:
        client = FakeClient()
        with self.assertRaises(EntityControlledGenerationError):
            self.runner(client).run(
                document=self.document,
                stage_output=self.entity_output,
                confirm_real_generation=False,
            )
        self.assertEqual(client.health_calls, 0)
        self.assertEqual(client.generate_calls, 0)
        self.assertEqual(StageExecutionAttempt.objects.count(), 0)

    def test_failed_preflight_never_calls_generate_and_is_audited(self) -> None:
        failed = _valid_tokenization()
        failed = GpuTokenizationPreflightResult(
            **{
                **failed.__dict__,
                "status": "validation_failed",
                "validation": {**failed.validation, "prompt_truncated": True},
            }
        )
        client = FakeClient(tokenization=failed)
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="preflight-failure",
        )
        self.assertEqual(client.generate_calls, 0)
        self.assertEqual(summary["execution_status"], "validation_failed")
        self.assertEqual(StageExecutionAttempt.objects.count(), 1)
        self.entity_output.refresh_from_db()
        self.assertEqual(self.entity_output.raw_output, "before")

    def test_remote_prompt_hash_mismatch_never_calls_generate(self) -> None:
        failed = _valid_tokenization()
        comparison = dict(failed.payload["component_comparison"])
        current = dict(comparison["current"])
        components = [dict(item) for item in current["components"]]
        components[0]["sha256"] = "0" * 64
        current["components"] = components
        failed = GpuTokenizationPreflightResult(
            **{
                **failed.__dict__,
                "payload": {
                    **failed.payload,
                    "component_comparison": {**comparison, "current": current},
                },
            }
        )
        client = FakeClient(tokenization=failed)
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="hash-mismatch",
        )
        self.assertEqual(client.generate_calls, 0)
        self.assertEqual(summary["execution_status"], "validation_failed")

    def test_missing_provider_readiness_never_calls_generate(self) -> None:
        health = FakeClient().health_result
        payload = dict(health.payload)
        diagnostics = dict(payload["diagnostics"])
        runtime = dict(diagnostics["runtime"])
        runtime["readiness_evidence"] = {}
        diagnostics["runtime"] = runtime
        payload["diagnostics"] = diagnostics
        client = FakeClient(health=GpuProviderHealthResult(ok=True, payload=payload))
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="missing-readiness",
        )
        self.assertEqual(client.generate_calls, 0)
        self.assertEqual(client.call_order, ["health", "tokenize", "health"])
        self.assertEqual(summary["execution_status"], "validation_failed")
        self.assertTrue(
            any(
                item["code"] == "provider_readiness_gate_failed"
                for item in summary["errors"]
            )
        )

    def test_default_stage_runner_blocks_entity_generation(self) -> None:
        with patch(
            "tagger.services.stage_runner.GpuLocalProviderClient.generate"
        ) as generate:
            result = ChatbotStageRunner().run(
                stage_id="entity_registry",
                document=self.document,
                stage_outputs={},
                provider="gpu_local",
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.errors[0]["code"], "entity_generation_requires_controlled_command")
        generate.assert_not_called()

    def test_invalid_output_only_updates_attempt(self) -> None:
        client = FakeClient(generation=_generation_result(HEADER + "\nnot a registry tag"))
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="invalid-output",
        )
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(
            client.call_order,
            ["health", "tokenize", "health", "generate"],
        )
        self.assertFalse(summary["deterministic_validation_valid"])
        self.entity_output.refresh_from_db()
        self.assertEqual(self.entity_output.status, StageOutput.Status.LOADED)
        self.assertEqual(self.entity_output.raw_output, "before")

    def test_valid_output_enters_checking_and_preserves_audit_evidence(self) -> None:
        client = FakeClient()
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="valid-output",
        )
        self.entity_output.refresh_from_db()
        attempt = StageExecutionAttempt.objects.get(request_id="valid-output")
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(self.entity_output.status, StageOutput.Status.CHECKING)
        self.assertTrue(summary["deterministic_validation_valid"])
        self.assertTrue(summary["requires_human_review"])
        self.assertEqual(summary["prompt_tokens"], 22417)
        self.assertEqual(summary["generated_tokens"], 200)
        self.assertFalse(summary["generation_may_be_truncated"])
        self.assertTrue(summary["post_load_vram_gate"]["passed"])
        self.assertTrue(summary["generation_ready_vram_gate"]["passed"])
        self.assertTrue(attempt.provenance["tokenizer_preflight"]["token_counts"])
        self.assertTrue(attempt.provenance["bundle"]["entity_candidate_package"])
        self.assertEqual(
            attempt.provenance["accepted_clause_parser"]["stage_output_id"],
            self.clause_output.pk,
        )

    def test_output_ceiling_without_eos_is_not_applied(self) -> None:
        generation = _generation_result(VALID_RAW)
        diagnostics = dict(generation.payload["diagnostics"])
        diagnostics["token_counts"] = {
            **diagnostics["token_counts"],
            "generated_tokens": 4096,
        }
        diagnostics["completion_evidence"] = {
            "finish_reason": "max_new_tokens",
            "eos_token_emitted": False,
            "hit_max_new_tokens": True,
            "generation_may_be_truncated": True,
        }
        generation = GpuLocalProviderResult(
            **{
                **generation.__dict__,
                "payload": {"diagnostics": diagnostics},
            }
        )
        summary = self.runner(FakeClient(generation=generation)).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="possibly-truncated",
        )
        self.assertTrue(summary["generation_may_be_truncated"])
        self.assertFalse(summary["deterministic_validation_valid"])
        self.entity_output.refresh_from_db()
        self.assertEqual(self.entity_output.status, StageOutput.Status.LOADED)

    def test_accepted_output_is_protected_before_network(self) -> None:
        self.entity_output.status = StageOutput.Status.ACCEPTED
        self.entity_output.save(update_fields=["status"])
        client = FakeClient()
        with self.assertRaises(EntityControlledGenerationError):
            self.runner(client).run(
                document=self.document,
                stage_output=self.entity_output,
                confirm_real_generation=True,
            )
        self.assertEqual(client.health_calls, 0)
        self.assertEqual(client.generate_calls, 0)

    def test_timeout_is_audited_without_stage_output_mutation(self) -> None:
        timeout = GpuLocalProviderResult(
            status="unavailable",
            provider="gpu_local",
            model="not-configured",
            error="timed out; remote completion unknown",
            errors=[{"code": "gpu_local_timeout", "message": "timed out"}],
            metadata={
                "request_lifecycle": {
                    "state": "timed_out",
                    "remote_completion_known": False,
                }
            },
        )
        client = FakeClient(generation=timeout)
        summary = self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="timeout-output",
        )
        self.assertEqual(summary["execution_status"], "unavailable")
        self.entity_output.refresh_from_db()
        self.assertEqual(self.entity_output.status, StageOutput.Status.LOADED)

    def test_failed_provider_response_persists_nested_gate_and_oom_evidence(self) -> None:
        generation = GpuLocalProviderResult(
            status="error",
            provider="gpu_local",
            model="Qwen2.5-32B-Instruct",
            payload={
                "diagnostics": {
                    "provider_server_version": "test-provider-version",
                    "device_map_profile": "qwen2_5_32b_long_context_v2",
                    "vram_reserve_gib": 4.0,
                    "generation_runtime_margin_mib": 4096,
                    "cuda_allocator": {
                        "configured_value": "expandable_segments:True"
                    },
                    "post_load_gate_contract": "snapshot-complete-v2",
                    "generation_ready_gate_contract": "token-aware-kv-v1",
                    "execution_diagnostics": {
                        "phase": {
                            "model_loaded": True,
                            "tokenized": True,
                            "generation_started": True,
                            "generation_completed": False,
                        },
                        "model_call_attempted": True,
                        "model_call_completed": False,
                        "token_counts": {
                            "prompt_tokens": 22417,
                            "max_new_tokens": 4096,
                        },
                        "post_load_vram_gate": {
                            "passed": True,
                            "contract": "snapshot-complete-v2",
                        },
                        "generation_ready_vram_gate": {
                            "passed": True,
                            "contract": "token-aware-kv-v1",
                            "runtime_margin_mib": 4096,
                        },
                        "memory_snapshot_at_failure": [
                            {"index": 0, "free_mib": 1120.0}
                        ],
                        "cuda_cache_cleanup": {
                            "empty_cache_called": True,
                            "model_unloaded": False,
                            "other_processes_signalled": False,
                        },
                    },
                }
            },
            validation={
                "model_call_attempted": True,
                "model_call_completed": False,
                "prompt_truncated": False,
                "source_complete": True,
            },
            errors=[{"code": "gpu_out_of_memory", "message": "synthetic OOM"}],
            metadata={
                "provider_provenance": {
                    "model_call_attempted": True,
                    "model_call_completed": False,
                    "real_chatbot_execution": False,
                }
            },
        )
        summary = self.runner(FakeClient(generation=generation)).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="nested-oom-evidence",
        )
        attempt = StageExecutionAttempt.objects.get(
            request_id="nested-oom-evidence"
        )
        self.assertTrue(summary["post_load_vram_gate"]["passed"])
        self.assertTrue(summary["generation_ready_vram_gate"]["passed"])
        self.assertEqual(summary["memory_snapshot_at_failure"][0]["index"], 0)
        stored = attempt.provenance["provider_response"]
        self.assertTrue(stored["post_load_vram_gate"]["passed"])
        self.assertTrue(stored["generation_ready_vram_gate"]["passed"])
        self.assertTrue(stored["cuda_cache_cleanup"]["empty_cache_called"])

    def test_duplicate_request_is_rejected_before_second_network_call(self) -> None:
        client = FakeClient(generation=_generation_result(HEADER + "\ninvalid"))
        self.runner(client).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="duplicate-request",
        )
        first_counts = (client.health_calls, client.tokenize_calls, client.generate_calls)
        with self.assertRaises(EntityControlledGenerationError):
            self.runner(client).run(
                document=self.document,
                stage_output=self.entity_output,
                confirm_real_generation=True,
                request_id="duplicate-request",
            )
        self.assertEqual(
            (client.health_calls, client.tokenize_calls, client.generate_calls),
            first_counts,
        )

    def test_three_prior_attempts_are_immutable_when_next_attempt_is_created(self) -> None:
        priors = []
        for number in (1, 2, 3):
            priors.append(StageExecutionAttempt.objects.create(
            stage_output=self.entity_output,
            request_id=f"attempt-{number}",
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            execution_status="error",
            disposition=StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED,
            provider="gpu_local",
            model="Qwen2.5-32B-Instruct",
            raw_output="",
            payload={"attempt": number},
            provenance={"evidence": f"preserve-{number}"},
            validation={"valid": False},
            error="oom",
        ))
        before = [
            {
                "request_id": prior.request_id,
                "status": prior.execution_status,
                "payload": prior.payload,
                "provenance": prior.provenance,
                "error": prior.error,
            }
            for prior in priors
        ]
        self.runner(FakeClient(generation=_generation_result(HEADER + "\ninvalid"))).run(
            document=self.document,
            stage_output=self.entity_output,
            confirm_real_generation=True,
            request_id="attempt-four",
        )
        for prior in priors:
            prior.refresh_from_db()
        self.assertEqual([
            {
                "request_id": prior.request_id,
                "status": prior.execution_status,
                "payload": prior.payload,
                "provenance": prior.provenance,
                "error": prior.error,
            }
            for prior in priors
        ],
            before,
        )

    def test_command_requires_confirmation_and_summary_does_not_leak_prompt(self) -> None:
        with self.assertRaises(CommandError):
            call_command(
                "entity_registry_gpu_generate",
                doc_id=self.document.doc_id,
            )
        safe_summary = {
            "execution_status": "completed",
            "lifecycle_status": "checking",
            "attempt_id": 1,
            "stage_output_id": self.entity_output.pk,
        }
        output = io.StringIO()
        with patch(
            "tagger.management.commands.entity_registry_gpu_generate.EntityControlledGenerationRunner"
        ) as runner_class:
            runner_class.return_value.run.return_value = safe_summary
            call_command(
                "entity_registry_gpu_generate",
                doc_id=self.document.doc_id,
                confirm_real_generation=True,
                stdout=output,
            )
        rendered = output.getvalue()
        self.assertNotIn(BODY, rendered)
        self.assertNotIn("SYSTEM", rendered)
        self.assertNotIn("candidate_provenance", rendered)

    def test_readiness_commands_are_explicit_and_do_not_require_document_data(self) -> None:
        diagnostic = GpuReadinessDiagnosticResult(
            status="completed",
            operation="synthetic_long_context",
            passed=True,
            provider_server_version="test-version",
            model="Qwen2.5-32B-Instruct",
            payload={
                "diagnostics": {
                    "business_data_used": False,
                    "prompt_tokens": 22417,
                    "max_output_tokens": 4096,
                    "post_load_vram_gate": {"passed": True},
                    "generation_ready_vram_gate": {"passed": True},
                    "model_call_attempted": True,
                    "model_call_completed": True,
                }
            },
            provenance={"device_map_profile": "qwen2_5_32b_long_context_v2"},
        )
        with self.assertRaises(CommandError):
            call_command(
                "gpu_provider_synthetic_long_context_diagnostic",
                prompt_tokens=22417,
            )
        output = io.StringIO()
        with patch(
            "tagger.management.commands.gpu_provider_synthetic_long_context_diagnostic.run_readiness_diagnostic",
            return_value=diagnostic,
        ) as run:
            call_command(
                "gpu_provider_synthetic_long_context_diagnostic",
                prompt_tokens=22417,
                confirm_synthetic_diagnostic=True,
                stdout=output,
            )
        call = run.call_args.kwargs
        self.assertEqual(call["operation"], "synthetic_long_context")
        self.assertEqual(call["prompt_tokens"], 22417)
        self.assertNotIn(BODY, output.getvalue())
        self.assertNotIn("candidate_provenance", output.getvalue())
