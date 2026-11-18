from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from . import models as m
from . import notifications
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


def allocate_team(actor, cohort, domain):
    teams = m.Team.objects.filter(cohort=cohort)
    team = teams.filter(domain=domain).annotate(member_count=Count("enrollment")).order_by("member_count", "code", "pk").first()
    if team:
        return team
    code = domain.code.upper()
    sequence = 1
    while teams.filter(code__iexact=code).exists():
        sequence += 1
        suffix = f"-{sequence}"
        code = f"{domain.code[:24 - len(suffix)].upper()}{suffix}"
    team = save_checked(m.Team(cohort=cohort, domain=domain, code=code, name=domain.name))
    audit(actor, "team.auto_created", team.pk)
    return team


@transaction.atomic
def register_intern(actor, *, reference, name, cohort, dit_id="", email="", domain=None,
                    preferred_domain=None, team=None, mode="manual", college="", phone="",
                    placement_role="", joined_on=None, notify=True):
    allow_coordinator(actor)
    reference = reference.strip().upper()
    cohort = m.Cohort.objects.select_for_update().get(pk=cohort.pk)
    if mode == "auto":
        suggestions = {item.pk: item for item in (domain, preferred_domain, team.domain if team else None) if item}
        if len(suggestions) != 1:
            raise ValidationError("Automatic domain matching requires one consistent preference or team domain. Choose a domain manually to resolve it.")
        domain = next(iter(suggestions.values()))
    if domain is None:
        raise ValidationError("Select a domain in manual mode.")
    if not cohort.domains.filter(pk=domain.pk).exists():
        raise ValidationError("Select a domain enabled for this internship batch.")
    if m.Intern.objects.filter(reference=reference).exists():
        raise ValidationError("This intern reference already exists. Open the intern’s profile to make changes.")
    if not name.strip():
        raise ValidationError("Enter the intern's full name.")
    if dit_id and m.Intern.objects.filter(dit_key=dit_id.strip().upper()).exists():
        raise ValidationError("This DIT ID is already registered.")
    if not dit_id:
        if not cohort.id_prefix:
            raise ValidationError("Supply the official DIT ID. Local issuance is disabled for this internship batch.")
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
    intern = save_checked(m.Intern(user=user, reference=reference, dit_id=dit_id,
                                  college=" ".join(college.split()), phone=phone, placement_role=placement_role,
                                  joined_on=joined_on))
    if mode == "auto" and not team:
        team = allocate_team(actor, cohort, domain)
    enrollment = save_checked(m.Enrollment(
        intern=intern, cohort=cohort, domain=domain, preferred_domain=preferred_domain, team=team,
    ))
    audit(actor, "roster.created", reference, mode)
    if notify:
        notifications.queue_welcome(intern)
    return enrollment


@transaction.atomic
def update_enrollment(actor, enrollment, *, name, email, domain, team, is_lead, reason,
                      college=None, phone="", placement_role="", joined_on=None):
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
    previous_email = user.email
    user.first_name, _, user.last_name = name.strip().partition(" ")
    user.email = email
    save_checked(user)
    if college is not None:
        intern = enrollment.intern
        intern.college = " ".join(college.split())
        intern.phone, intern.placement_role, intern.joined_on = phone, placement_role, joined_on
        save_checked(intern)
    if previous_email != email and (enrollment.intern.must_change_password or not user.has_usable_password()):
        if email:
            notifications.queue_welcome(enrollment.intern)
        else:
            user.set_unusable_password()
            user.save(update_fields=["password"])
            enrollment.intern.must_change_password = False
            enrollment.intern.save(update_fields=["must_change_password"])
    audit(actor, "roster.updated", enrollment.intern.reference,
          f"team {previous_team} → {enrollment.team_id}; domain {previous_domain} → {domain.pk}; {reason}")


@transaction.atomic
def assign_task(actor, *, enrollment, existing=None, notify=True, **values):
    allow_coordinator(actor)
    if existing is None and not enrollment.team_id:
        raise ValidationError("Assign the intern to a team first.")
    assignment = existing or m.Assignment(enrollment=enrollment, team=enrollment.team, domain=enrollment.domain)
    for field, value in values.items():
        setattr(assignment, field, value)
    save_checked(assignment)
    audit(actor, "assignment.updated" if existing else "assignment.created", assignment.reference)
    if notify and existing is None:
        notifications.queue_assignment(assignment, enrollment.intern.user)
    return assignment


def validate_artifact(upload):
    if Path(upload.name).suffix.lower() not in FILE_EXTENSIONS:
        raise ValidationError("Choose a supported file such as PDF, DOCX, XLSX, PNG, or ZIP.")
    if not 0 < upload.size <= MAX_ARTIFACT_BYTES:
        raise ValidationError("Choose a nonempty submission file up to 15 MB.")


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
    note = note.strip()
    if not note or len(note) > 4000:
        raise ValidationError("Enter work details using up to 4,000 characters.")
    if not (artifact or staged):
        raise ValidationError("Upload a document for the coordinator to verify.")
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
def review_submission(actor, submission, *, decision, feedback="", revision_due=None, notify=True):
    allow_coordinator(actor)
    assignment = m.Assignment.objects.select_for_update().select_related("enrollment__intern__user").get(pk=submission.assignment_id)
    if assignment.cancelled or assignment.submissions.first() != submission:
        raise ValidationError("Open the latest submission on an active assignment.")
    if m.SubmissionReview.objects.filter(submission=submission).exists():
        raise ValidationError("This submission has already been reviewed.")
    if decision not in m.SubmissionReview.Decision.values:
        raise ValidationError("Choose Completed or Needs revision.")
    feedback = feedback.strip()
    if len(feedback) > 4000:
        raise ValidationError("Use up to 4,000 characters for review comments.")
    if decision == m.SubmissionReview.Decision.REVISION:
        if not feedback:
            raise ValidationError("Explain what the intern needs to revise.")
        if not revision_due or revision_due < timezone.localdate():
            raise ValidationError("Set a revision deadline for today or later.")
    review = save_checked(m.SubmissionReview(submission=submission, coordinator=actor, decision=decision, feedback=feedback))
    assignment.revision_due = revision_due if decision == m.SubmissionReview.Decision.REVISION else None
    assignment.save(update_fields=["revision_due"])
    audit(actor, "submission.reviewed", assignment.reference, review.get_decision_display())
    if notify:
        notifications.queue_review(review, assignment.enrollment.intern.user)
    return review


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
