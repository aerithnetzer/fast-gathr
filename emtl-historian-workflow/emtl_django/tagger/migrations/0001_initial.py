# Generated for the fixture-backed EMTL Tagger Workbench prototype.

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Document",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("doc_id", models.CharField(max_length=120, unique=True)),
                ("archival_reference", models.CharField(blank=True, max_length=255)),
                ("title", models.CharField(max_length=255)),
                ("document_type", models.CharField(blank=True, max_length=80)),
                ("normalized_date", models.CharField(blank=True, max_length=120)),
                ("source_file", models.CharField(blank=True, max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["doc_id"]},
        ),
        migrations.CreateModel(
            name="Clause",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("clause_id", models.CharField(max_length=40)),
                ("text", models.TextField()),
                ("sequence", models.PositiveIntegerField()),
                ("start_char", models.PositiveIntegerField(blank=True, null=True)),
                ("end_char", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "document",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="clauses", to="tagger.document"),
                ),
            ],
            options={"ordering": ["document", "sequence"]},
        ),
        migrations.CreateModel(
            name="StageOutput",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "stage",
                    models.CharField(
                        choices=[
                            ("summary_keywords", "Summary & Keyword"),
                            ("entity_registry", "Entity Registry"),
                            ("clause_parser", "Clause Parser"),
                            ("occurrences_registry", "Occurrences Registry"),
                            ("tag_assembler", "Tag Assembler"),
                            ("key_narrative", "Key Narrative Tagger"),
                        ],
                        max_length=60,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("fixture", "Fixture"),
                            ("imported", "Imported"),
                            ("reviewed", "Reviewed"),
                            ("needs_review", "Needs review"),
                        ],
                        default="fixture",
                        max_length=40,
                    ),
                ),
                ("display_title", models.CharField(max_length=180)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("raw_output", models.TextField(blank=True)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "document",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stage_outputs", to="tagger.document"),
                ),
            ],
            options={
                "ordering": ["document", "stage", "-updated_at"],
                "indexes": [
                    models.Index(fields=["document", "stage"], name="tagger_stag_documen_899cc2_idx"),
                    models.Index(fields=["status"], name="tagger_stag_status_48bb5a_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TagRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "record_type",
                    models.CharField(
                        choices=[
                            ("P", "Person"),
                            ("PF", "Person function"),
                            ("R", "Relationship"),
                            ("L", "Location"),
                            ("I", "Institution"),
                            ("T", "Thing"),
                            ("INT", "Intangible"),
                            ("TE", "Temporal"),
                            ("EV", "Event"),
                            ("C", "Concept"),
                            ("OTHER", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                ("stable_id", models.CharField(blank=True, db_index=True, max_length=120)),
                ("headword", models.CharField(max_length=255)),
                ("evidence_form", models.CharField(blank=True, max_length=255)),
                ("confidence_label", models.CharField(blank=True, max_length=80)),
                ("needs_review", models.BooleanField(default=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "clause",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tag_records", to="tagger.clause"),
                ),
                (
                    "document",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tag_records", to="tagger.document"),
                ),
                (
                    "stage_output",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tag_records", to="tagger.stageoutput"),
                ),
            ],
            options={
                "ordering": ["document", "record_type", "headword"],
                "indexes": [
                    models.Index(fields=["document", "record_type"], name="tagger_tagr_documen_935d10_idx"),
                    models.Index(fields=["needs_review"], name="tagger_tagr_needs_r_037643_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="NewIdProposal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("proposed_id", models.CharField(max_length=120)),
                ("record_type", models.CharField(max_length=40)),
                ("headword", models.CharField(max_length=255)),
                ("evidence_form", models.CharField(blank=True, max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("needs_edit", "Needs edit"),
                        ],
                        default="pending",
                        max_length=40,
                    ),
                ),
                ("reviewer_note", models.TextField(blank=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "document",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="new_id_proposals", to="tagger.document"),
                ),
                (
                    "source_clause",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="tagger.clause"),
                ),
            ],
            options={
                "ordering": ["document", "proposed_id"],
                "indexes": [
                    models.Index(fields=["document", "status"], name="tagger_newi_documen_fd0ffa_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReviewNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("note", models.TextField()),
                ("requested_action", models.CharField(blank=True, max_length=120)),
                ("created_by_label", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clause",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="tagger.clause"),
                ),
                (
                    "document",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="review_notes", to="tagger.document"),
                ),
                (
                    "stage_output",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="tagger.stageoutput"),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["document", "created_at"], name="tagger_revi_documen_7b4000_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="clause",
            constraint=models.UniqueConstraint(fields=("document", "clause_id"), name="unique_clause_per_document"),
        ),
    ]
