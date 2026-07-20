from unittest.mock import Mock, patch
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.conf import settings
from django.urls import reverse

from tagger.models import Document, NewIdProposal, StageOutput
from tagger.services.entity_review_handoff import build_entity_downstream_package
from tagger.services.live_workflow import (
    _normalize_chooser_decision,
    _choose_candidate,
    _resolve_chooser_candidate,
    RemoteQwen3LookupRunner,
    accept_remaining_entities,
    finalize_entity_review,
    save_entity_review_decision,
    save_assembler_output,
    save_occurrence_output,
    sync_eventcut_review_status,
)


class LiveWorkflowUiTests(TestCase):
    def test_uploaded_document_uses_live_route_without_fixture_outputs(self):
        response = self.client.post(
            reverse("tagger:home"),
            {"action": "upload_document", "document_file": SimpleUploadedFile("live.txt", b"First paragraph.\n\nSecond paragraph.")},
        )
        self.assertRedirects(response, reverse("tagger:home"))
        document = Document.objects.get(source_file="live.txt")
        self.assertTrue(document.metadata["live_workflow_enabled"])
        self.assertFalse(document.metadata["placeholder_outputs"])
        self.assertFalse(document.new_id_proposals.exists())
        self.assertTrue(all(row.status in {"not_started", "blocked"} for row in document.stage_outputs.all()))
        response = self.client.post(reverse("tagger:home"), {"action": "start_workflow"})
        self.assertRedirects(response, reverse("tagger:live_workbench"))

    @patch("tagger.live_views._provider_health", return_value={"ok": True, "model": "Qwen2.5-32B-Instruct"})
    def test_live_page_renders_complete_workflow(self, _health):
        document = self._live_document()
        response = self.client.get(reverse("tagger:live_workbench"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Live workflow")
        self.assertContains(response, "Provider:")
        self.assertContains(response, "Run Summary & Keywords")
        self.assertNotContains(response, "Run Clause Parser")
        self.assertNotContains(response, "Run Entity Bot")
        self.assertContains(response, "Event Extraction")
        self.assertContains(response, "Running workflow")
        self.assertNotContains(response, "sample fixture")
        self.assertNotContains(response, "Live GPU workflow")
        self.assertNotContains(response, "Run Qwen Entity Bot")

    def test_stage_run_buttons_appear_in_review_order(self):
        document = self._live_document()
        summary = document.stage_outputs.get(stage="summary_keywords")
        clause = document.stage_outputs.get(stage="clause_parser")
        summary.status = "accepted"
        summary.save()
        response = self.client.get(reverse("tagger:live_workbench"))
        self.assertContains(response, "Run Clause Parser")
        self.assertNotContains(response, "Run Entity Bot")
        clause.status = "accepted"
        clause.save()
        response = self.client.get(reverse("tagger:live_workbench"))
        self.assertContains(response, "Run Entity Bot")

    @patch("tagger.live_views.run_summary")
    def test_live_action_calls_real_service_boundary(self, run_summary_mock):
        self._live_document()
        response = self.client.post(reverse("tagger:live_workbench"), {"action": "run_summary"})
        self.assertRedirects(response, reverse("tagger:live_workbench"))
        run_summary_mock.assert_called_once()

    def test_save_source_text_resets_outputs_for_revised_input(self):
        document = self._live_document()
        summary = document.stage_outputs.get(stage="summary_keywords")
        summary.status = "checking"
        summary.raw_output = "old summary"
        summary.payload = {"old": True}
        summary.save()
        response = self.client.post(
            reverse("tagger:live_workbench"),
            {"action": "save_source", "source_text": "Revised first paragraph.\n\nRevised second paragraph."},
        )
        self.assertRedirects(response, reverse("tagger:live_workbench"))
        document.refresh_from_db()
        self.assertEqual(document.metadata["working_source_text"], "Revised first paragraph.\n\nRevised second paragraph.")
        self.assertEqual(document.clauses.count(), 2)
        summary.refresh_from_db()
        self.assertEqual(summary.status, "not_started")
        self.assertEqual(summary.raw_output, "")

    def test_entity_bulk_accept_preserves_edit_and_builds_downstream(self):
        document = self._live_document()
        stage = document.stage_outputs.get(stage="entity_registry")
        stage.status = "checking"
        stage.payload = {
            "entity_output": {"tags": [
                {"type": "P", "id": "P-1", "headword": "One", "raw_line": "P: One [P-1]"},
                {"type": "L", "id": "L-1", "headword": "Two", "raw_line": "L: Two [L-1]"},
            ]},
            "entity_review": {"attempt_id": 1, "state": "review_candidate"},
        }
        stage.provenance = {"entity_registry": {"approved_for_downstream": False}}
        stage.save()
        save_entity_review_decision(
            stage, row_index=0, decision="edited",
            edited_row={"type": "P", "id": "P-1", "headword": "One edited"},
        )
        stage.refresh_from_db()
        self.assertEqual(accept_remaining_entities(stage), 1)
        stage.refresh_from_db()
        finalize_entity_review(stage)
        stage.refresh_from_db()
        package = build_entity_downstream_package(stage)
        self.assertEqual([row["headword"] for row in package["reviewed_rows"]], ["One edited", "Two"])

    def test_entity_edit_is_rendered_as_current_row_with_status(self):
        document = self._live_document()
        stage = document.stage_outputs.get(stage="entity_registry")
        stage.status = "checking"
        stage.payload = {
            "entity_output": {"tags": [
                {"type": "INT", "id": "INT-0038", "headword": "Gift", "raw_line": "INT: Gift [INT-0038] | Trigger: gifte"},
            ]},
            "entity_review": {"state": "review_candidate"},
        }
        stage.save()
        self.client.post(reverse("tagger:live_workbench"), {
            "action": "entity_decision", "row_index": "0", "decision": "edited",
            "headword": "Gift", "stable_id": "INT-0099", "record_type": "INT",
        })
        response = self.client.get(reverse("tagger:live_workbench"))
        self.assertContains(response, "INT: Gift [INT-0099] | Trigger: gifte")
        self.assertContains(response, '<span class="entity-status status-edited">edited</span>', html=True)

    def test_user_entity_proposal_is_accepted_immediately(self):
        document = self._live_document()
        stage = document.stage_outputs.get(stage="entity_registry")
        stage.status = "checking"
        stage.payload = {"entity_output": {"tags": []}, "entity_review": {"state": "review_candidate"}}
        stage.save()
        self.client.post(reverse("tagger:live_workbench"), {
            "action": "propose_entity", "record_type": "INT", "headword": "Gift",
            "evidence_form": "gifte",
        })
        proposal = NewIdProposal.objects.get(document=document)
        self.assertEqual(proposal.status, NewIdProposal.Status.APPROVED)
        response = self.client.get(reverse("tagger:live_workbench"))
        self.assertContains(response, f"INT: Gift [{proposal.proposed_id}] | Form: gifte")

    def test_event_chooser_decision_aliases_are_normalized(self):
        self.assertEqual(
            _normalize_chooser_decision({"decision": "Select Candidate"}),
            "choose_candidate",
        )
        self.assertEqual(
            _normalize_chooser_decision({"decision": "None of the above"}),
            "none_of_these_fit",
        )

    def test_event_chooser_maps_candidate_by_normalized_id_rank_or_headword(self):
        candidates = [
            {"rank": 1, "event_id": "E-0012", "headword": "Give"},
            {"rank": 2, "event_id": "E-0044", "headword": "Receive"},
        ]
        self.assertEqual(
            _resolve_chooser_candidate(
                {"selected_candidate": {"event_id": "e 0012"}}, candidates
            )["event_id"],
            "E-0012",
        )
        self.assertEqual(
            _resolve_chooser_candidate(
                {"selected_candidate": {"rank": "Candidate 2"}}, candidates
            )["event_id"],
            "E-0044",
        )
        self.assertEqual(
            _resolve_chooser_candidate(
                {"selected_candidate": {"headword": "  GIVE "}}, candidates
            )["event_id"],
            "E-0012",
        )

    def test_event_chooser_safely_converts_out_of_list_selection_to_none_fit(self):
        client = Mock()
        client.generate.return_value = SimpleNamespace(
            status="completed",
            raw_output='{"decision":"choose_candidate","selected_candidate":{"event_id":"E-9999","headword":"Invented"},"reason":"best"}',
            error="",
        )
        result = _choose_candidate(
            client,
            {"event_cut_id": "EC-1", "event_cut_text": "gave a gift"},
            [{"rank": 1, "event_id": "E-0012", "headword": "Give", "definition": "", "llm_example": ""}],
            20,
        )
        self.assertEqual(result["decision"], "none_of_these_fit")
        self.assertIsNone(result["selected_candidate"])
        self.assertEqual(result["fallback_reason"], "chooser_out_of_list_candidate")

    def test_completed_event_review_becomes_accepted_export_data(self):
        document = self._live_document()
        stage = StageOutput.objects.create(
            document=document, stage="eventcut_extraction", display_title="Event Extraction",
            status="checking",
            payload={
                "parsed_event_cuts": [{
                    "valid": True, "event_cut_id": "eventcut-internal-1",
                    "clause_id": "1", "event_cut_text": "gave a gift",
                }],
                "headword_review_store": {"items": {"review-1": {
                    "item_id": "review-1", "clause_id": "001", "revision": 1,
                    "state": "accepted_existing_headword",
                    "event_cut": {"event_cut_id": "eventcut-internal-1", "text": "gave a gift"},
                    "assignment": {
                        "status": "accepted", "assignment_id": "assignment-1",
                        "event_cut_id": "eventcut-internal-1", "event_id": "E-0103",
                        "headword": "Gift", "accepted_by": "historian",
                    },
                }}},
            },
        )
        sync_eventcut_review_status(stage)
        stage.refresh_from_db()
        self.assertEqual(stage.status, "accepted")
        self.assertEqual(stage.payload["accepted_assignments"][0]["event_id"], "E-0103")
        self.assertEqual(stage.payload["accepted_assignments"][0]["clause_id"], "001")

    @patch("tagger.services.live_workflow.build_merged_event_occurrence_package", return_value={"contract_version": "merged"})
    @patch("tagger.services.live_workflow.validate_edited_occurrence_output")
    def test_occurrence_edit_is_revalidated_and_saved(self, validate_mock, _merge_mock):
        document = self._live_document()
        occurrence = document.stage_outputs.get(stage="occurrences_registry")
        clause = document.stage_outputs.get(stage="clause_parser")
        entity = document.stage_outputs.get(stage="entity_registry")
        occurrence.payload = {"event_assignment_package": {"contract_version": "event-assignment-downstream-v1"}}
        occurrence.save()
        validate_mock.return_value = {
            "valid": True, "issues": [], "parsed_clauses": [], "source_clauses": [],
        }
        save_occurrence_output(
            occurrence, "CLAUSE 001\nE: Gift [E-0103] | Trigger: gave",
            clause_output=clause, entity_output=entity,
        )
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "checking")
        self.assertTrue(occurrence.payload["human_edited"])

    def test_assembler_edit_preserves_occurrence_tags(self):
        document = self._live_document()
        occurrence = document.stage_outputs.get(stage="occurrences_registry")
        assembler = document.stage_outputs.get(stage="tag_assembler")
        occurrence.raw_output = "CLAUSE 001\nE: Gift [E-0103] | Trigger: gave"
        occurrence.save()
        save_assembler_output(
            assembler,
            "CLAUSE 001\nP: Alice [P-1]\nE: Gift [E-0103] | Trigger: gave",
            occurrence_output=occurrence,
        )
        assembler.refresh_from_db()
        self.assertEqual(assembler.status, "checking")
        self.assertTrue(assembler.payload["validation"]["valid"])

    @patch("tagger.services.live_workflow.time.sleep")
    @patch("tagger.services.live_workflow.subprocess.run")
    def test_remote_command_retries_transient_ssh_reset(self, run_mock, _sleep_mock):
        run_mock.side_effect = [
            SimpleNamespace(returncode=255, stderr="kex_exchange_identification: Connection reset", stdout=""),
            SimpleNamespace(returncode=0, stderr="", stdout="ok"),
        ]
        RemoteQwen3LookupRunner._run(["ssh", "remote-host", "true"])
        self.assertEqual(run_mock.call_count, 2)

    def _live_document(self):
        document = Document.objects.create(
            doc_id="live-ui-doc", title="Live UI", source_file="live.txt",
            metadata={"workflow_source": "uploaded_document", "working_source_text": "Source text", "live_workflow_enabled": True},
        )
        for stage, title in [
            ("summary_keywords", "Summary"), ("entity_registry", "Entity"),
            ("clause_parser", "Clause"), ("occurrences_registry", "Occurrence"),
            ("tag_assembler", "Assembler"), ("key_narrative", "Narrative"),
        ]:
            StageOutput.objects.create(document=document, stage=stage, display_title=title, status="blocked" if stage == "key_narrative" else "not_started")
        session = self.client.session
        session["active_document_pk"] = document.pk
        session["workflow_started"] = True
        session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        return document
