from __future__ import annotations

from django.contrib import admin

from .models import (
    Clause,
    Document,
    NewIdProposal,
    ReviewNote,
    StageExecutionAttempt,
    StageOutput,
    TagRecord,
)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("doc_id", "title", "document_type", "normalized_date")
    search_fields = ("doc_id", "title", "archival_reference")


@admin.register(Clause)
class ClauseAdmin(admin.ModelAdmin):
    list_display = ("document", "clause_id", "sequence")
    list_filter = ("document",)
    search_fields = ("clause_id", "text")


@admin.register(StageOutput)
class StageOutputAdmin(admin.ModelAdmin):
    list_display = ("document", "stage", "status", "display_title", "updated_at")
    list_filter = ("stage", "status")
    search_fields = ("document__doc_id", "display_title", "raw_output")


@admin.register(StageExecutionAttempt)
class StageExecutionAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "stage_output",
        "stage",
        "execution_status",
        "disposition",
        "provider",
        "created_at",
    )
    list_filter = ("stage", "execution_status", "disposition", "provider")
    search_fields = ("stage_output__document__doc_id", "raw_output", "error")


@admin.register(TagRecord)
class TagRecordAdmin(admin.ModelAdmin):
    list_display = ("document", "record_type", "headword", "stable_id", "needs_review")
    list_filter = ("record_type", "needs_review")
    search_fields = ("headword", "stable_id", "evidence_form")


@admin.register(NewIdProposal)
class NewIdProposalAdmin(admin.ModelAdmin):
    list_display = ("document", "proposed_id", "record_type", "headword", "status")
    list_filter = ("record_type", "status")
    search_fields = ("proposed_id", "headword", "evidence_form")


@admin.register(ReviewNote)
class ReviewNoteAdmin(admin.ModelAdmin):
    list_display = ("document", "created_by_label", "created_at")
    search_fields = ("note", "document__doc_id")
