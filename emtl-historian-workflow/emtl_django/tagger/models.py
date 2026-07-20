from __future__ import annotations

from django.db import models


class Document(models.Model):
    doc_id = models.CharField(max_length=120, unique=True)
    archival_reference = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=255)
    document_type = models.CharField(max_length=80, blank=True)
    normalized_date = models.CharField(max_length=120, blank=True)
    source_file = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["doc_id"]

    def __str__(self) -> str:
        return f"{self.doc_id} - {self.title}"


class Clause(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="clauses")
    clause_id = models.CharField(max_length=40)
    text = models.TextField()
    sequence = models.PositiveIntegerField()
    start_char = models.PositiveIntegerField(null=True, blank=True)
    end_char = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["document", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "clause_id"],
                name="unique_clause_per_document",
            )
        ]

    def __str__(self) -> str:
        return f"{self.document.doc_id}:{self.clause_id}"


class StageOutput(models.Model):
    class Stage(models.TextChoices):
        SUMMARY_KEYWORDS = "summary_keywords", "Summary & Keyword"
        ENTITY_REGISTRY = "entity_registry", "Entity Registry"
        CLAUSE_PARSER = "clause_parser", "Clause Parser"
        EVENTCUT_EXTRACTION = "eventcut_extraction", "EventCut Extraction (internal)"
        OCCURRENCES_REGISTRY = "occurrences_registry", "Occurrences Registry"
        TAG_ASSEMBLER = "tag_assembler", "Tag Assembler"
        KEY_NARRATIVE = "key_narrative", "Key Narrative Tagger"

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        LOADED = "loaded", "Loaded"
        CHECKING = "checking", "Checking"
        ACCEPTED = "accepted", "Accepted"
        NEEDS_RERUN = "needs_rerun", "Needs rerun"
        BLOCKED = "blocked", "Blocked"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="stage_outputs")
    stage = models.CharField(max_length=60, choices=Stage.choices)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.NOT_STARTED)
    display_title = models.CharField(max_length=180)
    payload = models.JSONField(default=dict, blank=True)
    raw_output = models.TextField(blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document", "stage", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "stage"],
                name="unique_stage_output_per_document",
            )
        ]
        indexes = [
            models.Index(fields=["document", "stage"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.document.doc_id} {self.get_stage_display()}"


class StageExecutionAttempt(models.Model):
    class Disposition(models.TextChoices):
        RECORDED_ONLY = "recorded_only", "Recorded for audit only"
        INTERNAL_APPLIED = "internal_applied", "Applied to internal workflow output"
        APPLIED_TO_CHECKING = "applied_to_checking", "Applied to checking StageOutput"
        INVALID_NOT_APPLIED = "invalid_not_applied", "Invalid and not applied"
        ACCEPTED_PROTECTED = "accepted_protected", "Accepted StageOutput protected"

    stage_output = models.ForeignKey(
        StageOutput,
        on_delete=models.CASCADE,
        related_name="execution_attempts",
    )
    request_id = models.CharField(max_length=160, null=True, blank=True, unique=True)
    stage = models.CharField(max_length=60)
    execution_status = models.CharField(max_length=40)
    disposition = models.CharField(
        max_length=40,
        choices=Disposition.choices,
        default=Disposition.RECORDED_ONLY,
    )
    provider = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=180, blank=True)
    raw_output = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    applied_to_stage_output = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["stage_output", "created_at"]),
            models.Index(fields=["stage", "execution_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.stage} attempt {self.id or 'unsaved'} ({self.execution_status})"


class TagRecord(models.Model):
    class RecordType(models.TextChoices):
        PERSON = "P", "Person"
        PERSON_FUNCTION = "PF", "Person function"
        RELATIONSHIP = "R", "Relationship"
        LOCATION = "L", "Location"
        INSTITUTION = "I", "Institution"
        THING = "T", "Thing"
        INTANGIBLE = "INT", "Intangible"
        TEMPORAL = "TE", "Temporal"
        EVENT = "EV", "Event"
        CONCEPT = "C", "Concept"
        OTHER = "OTHER", "Other"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="tag_records")
    clause = models.ForeignKey(
        Clause,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tag_records",
    )
    stage_output = models.ForeignKey(
        StageOutput,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tag_records",
    )
    record_type = models.CharField(max_length=20, choices=RecordType.choices)
    stable_id = models.CharField(max_length=120, blank=True, db_index=True)
    headword = models.CharField(max_length=255)
    evidence_form = models.CharField(max_length=255, blank=True)
    confidence_label = models.CharField(max_length=80, blank=True)
    needs_review = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document", "record_type", "headword"]
        indexes = [
            models.Index(fields=["document", "record_type"]),
            models.Index(fields=["needs_review"]),
        ]

    def __str__(self) -> str:
        return f"{self.record_type}: {self.headword}"


class NewIdProposal(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        NEEDS_EDIT = "needs_edit", "Needs edit"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="new_id_proposals")
    source_clause = models.ForeignKey(Clause, on_delete=models.SET_NULL, null=True, blank=True)
    proposed_id = models.CharField(max_length=120)
    record_type = models.CharField(max_length=40)
    headword = models.CharField(max_length=255)
    evidence_form = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.PENDING)
    reviewer_note = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document", "proposed_id"]
        indexes = [
            models.Index(fields=["document", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.proposed_id} {self.headword}"


class ReviewNote(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="review_notes")
    clause = models.ForeignKey(Clause, on_delete=models.SET_NULL, null=True, blank=True)
    stage_output = models.ForeignKey(StageOutput, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.TextField()
    requested_action = models.CharField(max_length=120, blank=True)
    created_by_label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Review note for {self.document.doc_id}"
