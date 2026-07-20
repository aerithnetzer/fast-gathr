from django.test import TestCase

from tagger.models import Document, StageOutput
from tagger.services.workflow_orchestrator import build_orchestration_plan, resume_orchestration
from tagger.stage_config import dependency_closure


class WorkflowOrchestratorTests(TestCase):
    def setUp(self):
        self.document = Document.objects.create(doc_id="orchestrator-doc", title="Orchestrator")

    def test_dependency_closure_is_minimal_and_transitive(self):
        self.assertEqual(
            dependency_closure({"tag_assembler"}),
            {"clause_parser", "entity_registry", "occurrences_registry", "tag_assembler"},
        )
        self.assertEqual(dependency_closure({"summary_keywords"}), {"summary_keywords"})

    def test_resume_reuses_accepted_and_stops_at_review(self):
        StageOutput.objects.create(
            document=self.document, stage="clause_parser", status="accepted", display_title="Clauses"
        )
        StageOutput.objects.create(
            document=self.document,
            stage="entity_registry",
            status="checking",
            display_title="Entities",
        )
        plan = resume_orchestration(
            document=self.document, requested_stages={"occurrences_registry"}
        )
        self.assertEqual(plan.auto_included_stages, ["entity_registry", "clause_parser"])
        self.assertEqual(plan.next_stage_id, "entity_registry")
        self.assertEqual(plan.next_action, "await_human_review")
        self.document.refresh_from_db()
        snapshot = self.document.metadata["workflow_orchestrator"]
        self.assertEqual(snapshot["plan_fingerprint"], plan.plan_fingerprint)
        self.assertEqual(snapshot["resume_count"], 1)

    def test_accepted_entity_without_review_contract_is_not_reused(self):
        entity = StageOutput.objects.create(
            document=self.document,
            stage="entity_registry",
            status="accepted",
            display_title="Entities",
        )
        plan = build_orchestration_plan(
            document=self.document,
            requested_stages={"entity_registry"},
            stage_outputs={"entity_registry": entity},
        )
        self.assertEqual(plan.next_action, "run")
