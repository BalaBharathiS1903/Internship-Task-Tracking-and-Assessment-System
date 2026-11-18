from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from . import models as m
from .access import intern_profile, is_coordinator

FILE_EXTENSIONS = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".txt", ".csv"}
MAX_ARTIFACT_BYTES = 15 * 1024 * 1024


def audit(actor, verb, target, detail=""):
    m.AuditEvent.objects.create(actor=actor, verb=verb, target=str(target)[:100], detail=detail[:300])


def save_checked(record):
    record.full_clean()
    record.save()
    return record


def allow_coordinator(actor):
    if not is_coordinator(actor):
        raise PermissionDenied


@transaction.atomic
def register_intern(actor, *, reference, name, cohort, dit_id="", email="", domain=None,
                    preferred_domain=None, team=None, mode="manual"):
    allow_coordinator(actor)
    reference = reference.strip().upper()
    cohort = m.Cohort.objects.select_for_update().get(pk=cohort.pk)
    if mode == "auto":
        suggestions = {item.pk: item for item in (preferred_domain, team.domain if team else None) if item}
        if len(suggestions) != 1:
            raise ValidationError("Automatic domain matching requires one consistent preference or team domain. Choose a domain manually to resolve it.")
        domain = next(iter(suggestions.values()))
    if domain is None:
        raise ValidationError("Select a domain in manual mode.")
    if m.Intern.objects.filter(reference=reference).exists():
        raise ValidationError("This roster reference already exists. Open its profile to make changes.")
    if not name.strip():
        raise ValidationError("Enter the intern's full name.")
    if dit_id and m.Intern.objects.filter(dit_key=dit_id.strip().upper()).exists():
        raise ValidationError("This DIT ID is already registered.")
    if not dit_id:
        if not cohort.id_prefix:
            raise ValidationError("Supply the official DIT ID. Local issuance is disabled for this cohort.")
        while True:
            dit_id = f"{cohort.id_prefix}{cohort.next_id:04d}"
            cohort.next_id += 1
            if not m.Intern.objects.filter(dit_key=dit_id.upper()).exists():
                break
        cohort.save(update_fields=["next_id"])
    first, _, last = name.strip().partition(" ")
    user = get_user_model()(username=f"intern-{reference.lower().replace('/', '-')}", first_name=first, last_name=last, email=email)
    user.set_unusable_password()
    save_checked(user)
    intern = save_checked(m.Intern(user=user, reference=reference, dit_id=dit_id))
    enrollment = save_checked(m.Enrollment(
        intern=intern, cohort=cohort, domain=domain, preferred_domain=preferred_domain, team=team,
    ))
    audit(actor, "roster.created", reference, mode)
    return enrollment


@transaction.atomic
def update_enrollment(actor, enrollment, *, name, email, domain, team, is_lead, reason):
    allow_coordinator(actor)
    m.Cohort.objects.select_for_update().get(pk=enrollment.cohort_id)
    enrollment = m.Enrollment.objects.select_related("intern__user").get(pk=enrollment.pk)
    if is_lead and not team:
        raise ValidationError("Assign a team before designating a lead.")
    if is_lead:
        for previous in m.Enrollment.objects.filter(team=team, is_lead=True).exclude(pk=enrollment.pk):
            previous.is_lead = False
            previous.save(update_fields=["is_lead"])
            audit(actor, "lead.revoked", previous.pk)
    previous_team, previous_domain = enrollment.team_id, enrollment.domain_id
    enrollment.team, enrollment.domain, enrollment.is_lead = team, domain, is_lead
    save_checked(enrollment)
    user = enrollment.intern.user
    user.first_name, _, user.last_name = name.strip().partition(" ")
    user.email = email
    save_checked(user)
    audit(actor, "roster.updated", enrollment.intern.reference,
          f"team {previous_team} → {enrollment.team_id}; domain {previous_domain} → {domain.pk}; {reason}")


@transaction.atomic
def assign_task(actor, *, enrollment, existing=None, **values):
    allow_coordinator(actor)
    if existing is None and not enrollment.team_id:
        raise ValidationError("Assign the intern to a team first.")
    assignment = existing or m.Assignment(enrollment=enrollment, team=enrollment.team, domain=enrollment.domain)
    if existing and existing.submissions.exists() and values["rubric"].pk != existing.rubric_id:
        raise ValidationError("The rubric is fixed after the first submission.")
    for field, value in values.items():
        setattr(assignment, field, value)
    save_checked(assignment)
    audit(actor, "assignment.updated" if existing else "assignment.created", assignment.reference)
    return assignment


def validate_artifact(upload):
    if Path(upload.name).suffix.lower() not in FILE_EXTENSIONS:
        raise ValidationError("Choose a supported file such as PDF, DOCX, XLSX, PNG, or ZIP.")
    if upload.size > MAX_ARTIFACT_BYTES:
        raise ValidationError("Each submission file must be 15 MB or smaller.")


@transaction.atomic
def submit(actor, assignment_id, *, link="", note="", artifact=None, staged=None, dry_run=False,
           submitted_at=None, reason=""):
    assignment = m.Assignment.objects.select_for_update().get(pk=assignment_id)
    offline = submitted_at is not None
    if offline:
        allow_coordinator(actor)
        if not reason.strip() or not assignment.assigned_date <= timezone.localdate(submitted_at) <= timezone.localdate():
            raise ValidationError("Use a verified date between assignment and today, and enter a reason.")
    elif assignment.enrollment.intern.user_id != actor.pk:
        raise PermissionDenied
    current = assignment.submissions.first()
    if assignment.cancelled or assignment.assigned_date > timezone.localdate() or (current and not assignment.revision_due):
        raise ValidationError("This assignment is closed for submissions.")
    if current and current.deadline == assignment.revision_due:
        review = getattr(current, "evaluation", None)
        if not (review and review.decision == m.Evaluation.Decision.REVISION and review.published_at):
            raise ValidationError("This revision has already been submitted.")
    if not (link or artifact or staged):
        raise ValidationError("Provide a file or an HTTPS link.")
    if link:
        URLValidator(schemes=["https"])(link)
    if artifact:
        validate_artifact(artifact)
    submitted = m.Submission(
        assignment=assignment, version=current.version + 1 if current else 1,
        recorded_by=actor, submitted_at=submitted_at or timezone.now(), deadline=assignment.revision_due or assignment.due_date,
        link=link, note=note, artifact_name=artifact.name if artifact else (staged.name if staged else ""),
    )
    if staged:
        submitted.artifact.name = staged.file.name
    submitted.full_clean()
    if artifact:
        submitted.artifact = artifact
    if not dry_run:
        try:
            submitted.save()
            assignment.revision_due = None
            assignment.save(update_fields=["revision_due"])
            audit(actor, "submission.offline" if offline else "submission.saved", assignment.reference, reason)
        except Exception:
            if artifact and submitted.artifact._committed:
                submitted.artifact.delete(save=False)
            raise
    return submitted


@transaction.atomic
def publish_rubric(actor, rubric):
    allow_coordinator(actor)
    rubric = m.Rubric.objects.select_for_update().get(pk=rubric.pk)
    criteria = list(rubric.criteria.all())
    if sum(item.weight for item in criteria) != Decimal("100"):
        raise ValidationError("Criterion weights must total exactly 100%.")
    for item in criteria:
        item.full_clean()
    rubric.published = True
    rubric.save(update_fields=["published"])
    audit(actor, "rubric.published", rubric)


@transaction.atomic
def add_criterion(actor, rubric, **values):
    allow_coordinator(actor)
    rubric = m.Rubric.objects.select_for_update().get(pk=rubric.pk)
    if rubric.published:
        raise ValidationError("Create a new rubric version to change published criteria.")
    return save_checked(m.Criterion(rubric=rubric, **values))


def weighted_score(criteria, scores):
    if any(item.pk not in scores for item in criteria):
        return None
    score = sum(item.weight * (scores[item.pk] - item.minimum) / (item.maximum - item.minimum) for item in criteria)
    return Decimal(score).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def assess(actor, submission, entries, *, decision="", revision_due=None):
    allow_coordinator(actor)
    assignment = m.Assignment.objects.select_for_update().get(pk=submission.assignment_id)
    if assignment.cancelled or assignment.submissions.first() != submission:
        raise ValidationError("Open the latest submission on an active assignment.")
    criteria = {item.pk: item for item in assignment.rubric.criteria.all()}
    evaluation, _ = m.Evaluation.objects.get_or_create(submission=submission, defaults={"coordinator": actor})
    if evaluation.published_at:
        raise ValidationError("This result has already been published.")
    for criterion_id, values in entries.items():
        criterion = criteria.get(criterion_id)
        if criterion is None:
            raise ValidationError("The criterion is outside this submission's rubric.")
        value, comment, feedback = values
        if value < criterion.minimum or value > criterion.maximum or (value - criterion.minimum) % criterion.step:
            raise ValidationError(f"{criterion.name}: use {criterion.minimum}–{criterion.maximum} in increments of {criterion.step}.")
        fraction = (value - criterion.minimum) / (criterion.maximum - criterion.minimum)
        band = "Meets most criteria" if fraction >= Decimal("0.75") else "Review the criterion and identify improvements"
        draft = f"{criterion.name} — {band}. {criterion.description} {comment}".strip()
        score, _ = m.Score.objects.get_or_create(
            evaluation=evaluation, criterion=criterion, defaults={"value": value},
        )
        score.value, score.comment, score.feedback = value, comment, feedback or draft
        save_checked(score)
    evaluation.total = weighted_score(list(criteria.values()), dict(evaluation.scores.values_list("criterion_id", "value")))
    evaluation.coordinator = actor
    if decision:
        if decision not in m.Evaluation.Decision.values or evaluation.total is None:
            raise ValidationError("Enter every required score before publishing a valid decision.")
        if decision == m.Evaluation.Decision.REVISION and (not revision_due or revision_due < timezone.localdate()):
            raise ValidationError("Set a revision deadline for today or later.")
        evaluation.decision = decision
        evaluation.published_at = timezone.now()
        assignment.revision_due = revision_due if decision == m.Evaluation.Decision.REVISION else None
        assignment.save(update_fields=["revision_due"])
    evaluation.save()
    audit(actor, "evaluation.published" if decision else "evaluation.saved", assignment.reference)
    return evaluation


@transaction.atomic
def mark_attendance(actor, session, enrollment, status, reason=""):
    allow_coordinator(actor)
    session = m.AttendanceSession.objects.select_for_update().get(pk=session.pk)
    prior = m.Attendance.objects.filter(session=session, enrollment=enrollment).first()
    if enrollment.team_id != session.team_id and not prior:
        raise ValidationError("This intern is outside the session team.")
    if (session.closed or prior and prior.status not in (status, m.Attendance.Status.UNMARKED)) and not reason.strip():
        raise ValidationError("Enter a reason when correcting attendance or marking a closed session.")
    record = prior or m.Attendance(session=session, enrollment=enrollment)
    record.status = status
    save_checked(record)
    audit(actor, "attendance.saved", session.reference, reason)


@transaction.atomic
def send_team_message(actor, member_id, body):
    profile = intern_profile(actor)
    if not profile:
        raise PermissionDenied
    target = m.Enrollment.objects.filter(pk=member_id, team__isnull=False).first()
    if not target:
        raise PermissionDenied
    m.Cohort.objects.select_for_update().get(pk=target.cohort_id)
    recipient = m.Enrollment.objects.get(pk=target.pk)
    if recipient.intern_id == profile.pk or not recipient.team_id or not m.Enrollment.objects.filter(
        intern=profile, team_id=recipient.team_id, is_lead=True,
    ).exists():
        raise PermissionDenied
    return save_checked(m.TeamMessage(team=recipient.team, sender=profile, recipient=recipient.intern, body=body.strip()))
