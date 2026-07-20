from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tagger", "0003_stageexecutionattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="stageexecutionattempt",
            name="request_id",
            field=models.CharField(
                blank=True,
                max_length=160,
                null=True,
                unique=True,
            ),
        ),
    ]
