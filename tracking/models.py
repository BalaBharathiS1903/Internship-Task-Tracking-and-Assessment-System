import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

code_validator = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_/-]*$", "Use letters, numbers, or - _ /.")


class Cohort(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=24, unique=True, validators=[code_validator])
    id_prefix = models.CharField(max_length=24, blank=True, validators=[code_validator])
    next_id = models.PositiveIntegerField(default=1, editable=False)

    def __str__(self):
        return self.name


class Domain(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=24, unique=True, validators=[code_validator])
    cohorts = models.ManyToManyField(Cohort, related_name="domains")

    def __str__(self):
        return self.name


class Team(models.Model):
    cohort = models.ForeignKey(Cohort, on_delete=models.PROTECT)
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=24, validators=[code_validator])
    description = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["cohort", "code"], name="unique_team_code")]

    def clean(self):
        if self.cohort_id and self.domain_id and not self.domain.cohorts.filter(pk=self.cohort_id).exists():
            raise ValidationError({"domain": "Enable this domain for the cohort first."})

    def __str__(self):
        return self.name


class Intern(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intern")
    reference = models.CharField(max_length=40, unique=True, validators=[code_validator])
    dit_id = models.CharField(max_length=40, validators=[code_validator])
    dit_key = models.CharField(max_length=40, unique=True, editable=False)
    phone = models.CharField(max_length=32, blank=True)
    college = models.CharField(max_length=160, blank=True)
    placement_role = models.CharField(max_length=100, blank=True)
    joined_on = models.DateField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.dit_id = self.dit_id.strip()
        self.dit_key = self.dit_id.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.dit_id} · {self.user.get_full_name() or self.user.username}"


class Enrollment(models.Model):
    intern = models.ForeignKey(Intern, on_delete=models.PROTECT, related_name="enrollments")
    cohort = models.ForeignKey(Cohort, on_delete=models.PROTECT)
    preferred_domain = models.ForeignKey(
        Domain, on_delete=models.PROTECT, null=True, blank=True, related_name="preferences"
    )
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True)
    is_lead = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["intern", "cohort"], name="unique_enrollment"),
            models.UniqueConstraint(fields=["team"], condition=Q(is_lead=True), name="one_lead_per_team"),
        ]

    def clean(self):
        if self.domain_id and self.cohort_id and not self.domain.cohorts.filter(pk=self.cohort_id).exists():
            raise ValidationError({"domain": "Select a domain enabled for this cohort."})
        if self.preferred_domain_id and self.cohort_id and not self.preferred_domain.cohorts.filter(
            pk=self.cohort_id
        ).exists():
            raise ValidationError({"preferred_domain": "Select a preference enabled for this cohort."})
        if self.team_id and (self.team.cohort_id != self.cohort_id or self.team.domain_id != self.domain_id):
            raise ValidationError({"team": "Select a team in this cohort and domain."})
        if self.is_lead and not self.team_id:
            raise ValidationError({"is_lead": "A team lead must belong to a team."})

    def __str__(self):
        return f"{self.intern} · {self.cohort}"


class Rubric(models.Model):
    name = models.CharField(max_length=100)
    version = models.PositiveIntegerField(default=1)
    published = models.BooleanField(default=False, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "version"], name="unique_rubric_version")]

    def __str__(self):
        return f"{self.name} v{self.version}"


class Criterion(models.Model):
    rubric = models.ForeignKey(Rubric, on_delete=models.PROTECT, related_name="criteria")
    code = models.CharField(max_length=24, validators=[code_validator])
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    weight = models.DecimalField(max_digits=5, decimal_places=2)
    minimum = models.DecimalField(max_digits=8, decimal_places=2)
    maximum = models.DecimalField(max_digits=8, decimal_places=2)
    step = models.DecimalField(max_digits=8, decimal_places=2, default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rubric", "code"], name="unique_criterion"),
            models.CheckConstraint(condition=Q(weight__gt=0, weight__lte=100, step__gt=0, maximum__gt=F("minimum")), name="valid_criterion_scale"),
        ]

    def clean(self):
        if self.minimum is not None and self.maximum is not None and self.step is not None and self.step > 0:
            if (self.maximum - self.minimum) % self.step:
                raise ValidationError({"step": "The increment must divide the score range."})

    def __str__(self):
        return self.name


class Assignment(models.Model):
    reference = models.CharField(max_length=40, unique=True, validators=[code_validator])
    task_code = models.CharField(max_length=40, validators=[code_validator])
    title = models.CharField(max_length=150)
    instructions = models.TextField(blank=True)
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT)
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT)
    rubric = models.ForeignKey(Rubric, on_delete=models.PROTECT)
    assigned_date = models.DateField()
    due_date = models.DateField()
    revision_due = models.DateField(null=True, blank=True)
    cancelled = models.BooleanField(default=False)
    started = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(due_date__gte=F("assigned_date")), name="ordered_dates"),
        ]

    def clean(self):
        if self.assigned_date and self.due_date and self.due_date < self.assigned_date:
            raise ValidationError({"due_date": "Due date cannot precede the assignment date."})
        if self.rubric_id and not self.rubric.published:
            raise ValidationError({"rubric": "Publish this rubric version before assigning it."})

    def __str__(self):
        return f"{self.reference} · {self.title}"


def private_path(instance, filename):
    return f"submissions/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class Submission(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="submissions")
    version = models.PositiveIntegerField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    submitted_at = models.DateTimeField()
    deadline = models.DateField()
    link = models.URLField(max_length=500, blank=True)
    artifact = models.FileField(upload_to=private_path, blank=True)
    artifact_name = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["assignment", "version"], name="unique_submission_version")]


class Evaluation(models.Model):
    class Decision(models.TextChoices):
        PASSED = "passed", "Passed"
        REVISION = "revision", "Needs revision"

    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="evaluation")
    coordinator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=12, choices=Decision.choices, blank=True)
    total = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    imported_notes = models.TextField(blank=True)


class Score(models.Model):
    evaluation = models.ForeignKey(Evaluation, on_delete=models.CASCADE, related_name="scores")
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT)
    value = models.DecimalField(max_digits=8, decimal_places=2)
    comment = models.TextField(blank=True)
    feedback = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["evaluation", "criterion"], name="unique_score")]


class AttendanceSession(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    reference = models.CharField(max_length=40, unique=True, validators=[code_validator])
    date = models.DateField()
    closed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.reference} · {self.team} · {self.date}"


class Attendance(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        ABSENT = "absent", "Absent"
        LATE = "late", "Late"
        EXCUSED = "excused", "Excused"
        UNMARKED = "unmarked", "Unmarked"

    session = models.ForeignKey(AttendanceSession, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "enrollment"], name="unique_attendance")]


class TeamMessage(models.Model):
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    sender = models.ForeignKey(Intern, on_delete=models.PROTECT, related_name="sent_messages")
    recipient = models.ForeignKey(Intern, on_delete=models.PROTECT, related_name="received_messages")
    body = models.CharField(max_length=1500)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)


class ImportBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    rows = models.JSONField(default=list)
    filename = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)


def staged_path(instance, filename):
    return f"staged/{instance.batch_id}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class StagedArtifact(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="artifacts")
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to=staged_path)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    verb = models.CharField(max_length=60)
    target = models.CharField(max_length=100)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class FollowUpRun(models.Model):
    date = models.DateField(unique=True)
    items = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class EmailNotification(models.Model):
    class Kind(models.TextChoices):
        WELCOME = "welcome", "Intern login"
        ASSIGNMENT = "assignment", "Task assignment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    recipient = models.EmailField()
    assignment = models.ForeignKey(Assignment, null=True, blank=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=180, blank=True)
