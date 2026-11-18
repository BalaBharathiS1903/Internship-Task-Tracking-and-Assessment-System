import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def preserve_published_reviews(apps, schema_editor):
    reviews = apps.get_model("tracking", "SubmissionReview").objects.using(schema_editor.connection.alias)
    scores = apps.get_model("tracking", "Score").objects.using(schema_editor.connection.alias)
    reviews.filter(published_at__isnull=True).delete()
    reviews.exclude(decision__in=["passed", "revision"]).delete()
    for review in reviews.iterator():
        comments = scores.filter(evaluation_id=review.pk).exclude(feedback="").order_by("pk")
        review.feedback = "\n\n".join(comments.values_list("feedback", flat=True))
        if review.decision == "passed":
            review.decision = "completed"
        review.save(update_fields=["decision", "feedback"])


class Migration(migrations.Migration):
    dependencies = [("tracking", "0007_welcome_password_fingerprint")]

    operations = [
        migrations.RenameModel("Evaluation", "SubmissionReview"),
        migrations.AddField("submissionreview", "feedback", models.TextField(blank=True)),
        migrations.RunPython(preserve_published_reviews),
        migrations.DeleteModel("Score"),
        migrations.RemoveField("assignment", "rubric"),
        migrations.DeleteModel("Criterion"),
        migrations.DeleteModel("Rubric"),
        migrations.RemoveField("submissionreview", "total"),
        migrations.RemoveField("submissionreview", "reviewed_at"),
        migrations.RemoveField("submissionreview", "imported_notes"),
        migrations.AlterField("submissionreview", "submission", models.OneToOneField(
            on_delete=django.db.models.deletion.PROTECT, related_name="review", to="tracking.submission",
        )),
        migrations.AlterField("submissionreview", "decision", models.CharField(
            choices=[("completed", "Completed"), ("revision", "Needs revision")], max_length=12,
        )),
        migrations.AlterField("submissionreview", "published_at", models.DateTimeField(default=django.utils.timezone.now)),
        migrations.AlterField("emailnotification", "kind", models.CharField(
            choices=[("welcome", "Intern login"), ("assignment", "Task assignment"), ("review", "Submission status")], max_length=12,
        )),
        migrations.AddField("emailnotification", "submission", models.ForeignKey(
            blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="tracking.submission",
        )),
    ]
