# Generated migration adding model execution attempt persistence.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tagger", "0002_alter_stageoutput_status_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="StageExecutionAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("stage", models.CharField(max_length=60)),
                ("execution_status", models.CharField(max_length=40)),
                (
                    "disposition",
                    models.CharField(
                        choices=[
                            ("recorded_only", "Recorded for audit only"),
                            ("applied_to_checking", "Applied to checking StageOutput"),
                            ("invalid_not_applied", "Invalid and not applied"),
                            ("accepted_protected", "Accepted StageOutput protected"),
                        ],
                        default="recorded_only",
                        max_length=40,
                    ),
                ),
                ("provider", models.CharField(blank=True, max_length=80)),
                ("model", models.CharField(blank=True, max_length=180)),
                ("raw_output", models.TextField(blank=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                ("validation", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("applied_to_stage_output", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "stage_output",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="execution_attempts",
                        to="tagger.stageoutput",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="stageexecutionattempt",
            index=models.Index(
                fields=["stage_output", "created_at"],
                name="tagger_stag_stage_o_907150_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="stageexecutionattempt",
            index=models.Index(
                fields=["stage", "execution_status"],
                name="tagger_stag_stage_374a9e_idx",
            ),
        ),
    ]
