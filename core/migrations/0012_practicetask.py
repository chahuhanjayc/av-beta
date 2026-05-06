from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_checklistitem"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PracticeTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=160)),
                ("task_type", models.CharField(choices=[("GST", "GST"), ("TDS", "TDS"), ("ITR", "ITR"), ("MCA", "MCA"), ("AUDIT", "Audit"), ("NOTICE", "Notice"), ("DOCUMENT", "Document Chase"), ("BANK", "Banking"), ("OTHER", "Other")], default="OTHER", max_length=20)),
                ("priority", models.CharField(choices=[("low", "Low"), ("normal", "Normal"), ("high", "High"), ("critical", "Critical")], default="normal", max_length=20)),
                ("status", models.CharField(choices=[("open", "Open"), ("in_progress", "In Progress"), ("blocked", "Blocked"), ("done", "Done"), ("cancelled", "Cancelled")], default="open", max_length=20)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("period_start", models.DateField(blank=True, null=True)),
                ("period_end", models.DateField(blank=True, null=True)),
                ("reference", models.CharField(blank=True, help_text="Notice number, filing ref, or external task id.", max_length=120)),
                ("description", models.TextField(blank=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_practice_tasks", to=settings.AUTH_USER_MODEL)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="practice_tasks", to="core.company")),
                ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="completed_practice_tasks", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_practice_tasks", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["status", "due_date", "-priority", "company__name"],
            },
        ),
        migrations.AddIndex(
            model_name="practicetask",
            index=models.Index(fields=["company", "status", "due_date"], name="core_task_cmp_stat_due_idx"),
        ),
        migrations.AddIndex(
            model_name="practicetask",
            index=models.Index(fields=["assigned_to", "status", "due_date"], name="core_task_assignee_status_idx"),
        ),
    ]
