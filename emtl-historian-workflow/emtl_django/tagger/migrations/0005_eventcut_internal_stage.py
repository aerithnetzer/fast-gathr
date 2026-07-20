from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tagger", "0004_stageexecutionattempt_request_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stageoutput",
            name="stage",
            field=models.CharField(
                choices=[
                    ("summary_keywords", "Summary & Keyword"),
                    ("entity_registry", "Entity Registry"),
                    ("clause_parser", "Clause Parser"),
                    ("eventcut_extraction", "EventCut Extraction (internal)"),
                    ("occurrences_registry", "Occurrences Registry"),
                    ("tag_assembler", "Tag Assembler"),
                    ("key_narrative", "Key Narrative Tagger"),
                ],
                max_length=60,
            ),
        ),
        migrations.AlterField(
            model_name="stageexecutionattempt",
            name="disposition",
            field=models.CharField(
                choices=[
                    ("recorded_only", "Recorded for audit only"),
                    ("internal_applied", "Applied to internal workflow output"),
                    ("applied_to_checking", "Applied to checking StageOutput"),
                    ("invalid_not_applied", "Invalid and not applied"),
                    ("accepted_protected", "Accepted StageOutput protected"),
                ],
                default="recorded_only",
                max_length=40,
            ),
        ),
    ]
