from __future__ import annotations

from django.test import TestCase

from tagger.models import Document, StageExecutionAttempt, StageOutput
from tagger.services.contracts import ExecutionStatus, StageExecutionResult
from tagger.services.entity_persistence import persist_entity_registry_attempt
from tagger.stage_config import missing_required_stages


def complete_bundle_files() -> list[dict[str, object]]:
    return [
        {
            "path": f"resource-{index}",
            "characters_loaded": index * 10,
            "characters_available": index * 10,
            "truncated": False,
        }
        for index in range(1, 4)
    ]


def stage_result(*, valid: bool, complete_source: bool = True, real_review_candidate: bool = False) -> StageExecutionResult:
    files = complete_bundle_files()
    if not complete_source:
        files[-1] = {**files[-1], "characters_loaded": 1, "truncated": True}
    validation = {
        "valid": valid,
        "registry_version": "entity-registry-test-version",
        "resource_hashes": {"Entity_List.xlsx": "hash"},
        "requires_human_review": real_review_candidate,
        "issues": [] if valid else [{
            "code": "semantic_review",
            "severity": "human_review" if real_review_candidate else "error",
        }],
    }
    return StageExecutionResult(
        status=ExecutionStatus.COMPLETED.value if valid else ExecutionStatus.VALIDATION_FAILED.value,
        raw_output="raw entity attempt",
        payload={
            "entity_validation": validation,
            "entity_output": {"tags": ([{"type": "P", "id": "P-0001", "headword": "Test"}] if real_review_candidate else [])},
        },
        provenance={
            "provider": "gpu_local",
            "model": "offline-test-model",
            "real_chatbot_execution": real_review_candidate,
            "model_call_completed": real_review_candidate,
            "bundle": {
                "loaded_files": files,
                "indexed_resources": [
                    {"source_file": f"registry-{index}", "source_hash": "hash", "fully_indexed": True}
                    for index in range(1, 4)
                ],
                "entity_candidate_package": {
                    "source_complete": True,
                    "provenance_complete": True,
                    "selection_policy_complete": True,
                    "mandatory_candidates_retained": True,
                    "candidate_package_complete": True,
                },
            },
        },
        provider="gpu_local",
        model="offline-test-model",
        validation={"entity_output": validation},
    )


class EntityAttemptPersistenceTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(
            doc_id="entity-attempt-test",
            title="Entity attempt test",
            metadata={"working_source_text": "offline source"},
        )
        self.stage_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            status=StageOutput.Status.LOADED,
            display_title="Entity Registry",
            raw_output="previous raw output",
            payload={"previous": True},
            provenance={"previous": True},
        )

    def test_valid_source_complete_attempt_is_recorded_and_applied_for_review(self) -> None:
        outcome = persist_entity_registry_attempt(
            stage_output=self.stage_output,
            result=stage_result(valid=True),
        )
        self.stage_output.refresh_from_db()
        attempt = StageExecutionAttempt.objects.get(pk=outcome.attempt_id)
        self.assertTrue(outcome.applied_to_stage_output)
        self.assertEqual(attempt.disposition, StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING)
        self.assertEqual(self.stage_output.status, StageOutput.Status.CHECKING)
        self.assertEqual(self.stage_output.raw_output, "raw entity attempt")
        self.assertTrue(self.stage_output.payload["entity_validation"]["valid"])

    def test_invalid_attempt_is_audited_without_mutating_stage_output(self) -> None:
        before = (
            self.stage_output.status,
            self.stage_output.raw_output,
            self.stage_output.payload,
            self.stage_output.provenance,
        )
        outcome = persist_entity_registry_attempt(
            stage_output=self.stage_output,
            result=stage_result(valid=False),
        )
        self.stage_output.refresh_from_db()
        after = (
            self.stage_output.status,
            self.stage_output.raw_output,
            self.stage_output.payload,
            self.stage_output.provenance,
        )
        attempt = StageExecutionAttempt.objects.get(pk=outcome.attempt_id)
        self.assertFalse(outcome.applied_to_stage_output)
        self.assertEqual(before, after)
        self.assertEqual(attempt.raw_output, "raw entity attempt")
        self.assertEqual(attempt.disposition, StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED)

    def test_parseable_real_human_review_output_becomes_checking_only(self) -> None:
        outcome = persist_entity_registry_attempt(
            stage_output=self.stage_output,
            result=stage_result(valid=False, real_review_candidate=True),
        )
        self.stage_output.refresh_from_db()
        attempt = StageExecutionAttempt.objects.get(pk=outcome.attempt_id)
        self.assertEqual(self.stage_output.status, StageOutput.Status.CHECKING)
        self.assertEqual(attempt.disposition, StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING)
        self.assertEqual(len(self.stage_output.payload["entity_registry"]), 1)
        review = self.stage_output.payload["entity_review"]
        self.assertEqual(review["state"], "review_candidate")
        self.assertFalse(review["deterministic_validation_valid"])
        self.assertTrue(review["requires_human_review"])
        self.assertFalse(review["approved_for_downstream"])
        self.assertNotEqual(self.stage_output.status, StageOutput.Status.ACCEPTED)
        accepted_stage_ids = {StageOutput.Stage.CLAUSE_PARSER}
        self.assertIn(
            StageOutput.Stage.ENTITY_REGISTRY,
            missing_required_stages(StageOutput.Stage.OCCURRENCES_REGISTRY, accepted_stage_ids),
        )
        accepted_stage_ids.add(StageOutput.Stage.ENTITY_REGISTRY)
        self.assertNotIn(
            StageOutput.Stage.ENTITY_REGISTRY,
            missing_required_stages(StageOutput.Stage.OCCURRENCES_REGISTRY, accepted_stage_ids),
        )

    def test_source_incomplete_attempt_is_not_applied(self) -> None:
        outcome = persist_entity_registry_attempt(
            stage_output=self.stage_output,
            result=stage_result(valid=True, complete_source=False),
        )
        self.stage_output.refresh_from_db()
        self.assertFalse(outcome.source_complete)
        self.assertFalse(outcome.applied_to_stage_output)
        self.assertEqual(self.stage_output.raw_output, "previous raw output")

    def test_accepted_stage_output_is_never_overwritten(self) -> None:
        self.stage_output.status = StageOutput.Status.ACCEPTED
        self.stage_output.save(update_fields=["status", "updated_at"])
        outcome = persist_entity_registry_attempt(
            stage_output=self.stage_output,
            result=stage_result(valid=True),
        )
        self.stage_output.refresh_from_db()
        attempt = StageExecutionAttempt.objects.get(pk=outcome.attempt_id)
        self.assertTrue(outcome.accepted_stage_output_protected)
        self.assertFalse(outcome.applied_to_stage_output)
        self.assertEqual(attempt.disposition, StageExecutionAttempt.Disposition.ACCEPTED_PROTECTED)
        self.assertEqual(self.stage_output.status, StageOutput.Status.ACCEPTED)
        self.assertEqual(self.stage_output.raw_output, "previous raw output")
