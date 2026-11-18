from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from openpyxl import Workbook

from . import forms as f, models as m, reporting as r, services as s, workbooks as w
from .access import coordinator_required, intern_profile, intern_required, is_coordinator, lead_memberships, own_assignments, team_assignments


def form_error(form, exc):
    if isinstance(exc, ValidationError):
        for message in exc.messages:
            form.add_error(None, message)
    else:
        form.add_error(None, "A conflicting record already exists. Refresh and try again.")


def render_form(request, form, title, back, button="Save changes", **context):
    return render(request, "form.html", dict(form=form, title=title, back=back, button=button, **context))


def query_scope(request):
    queryset = m.Assignment.objects.all()
    if not is_coordinator(request.user):
        if not intern_profile(request.user):
            raise PermissionDenied
        queryset = own_assignments(request.user, queryset)
    for field, lookup in (("cohort", "team__cohort_id"), ("domain", "domain_id"), ("team", "team_id")):
        raw = request.GET.get(field, "")
        if raw.isdecimal() and len(raw) < 16:
            queryset = queryset.filter(**{lookup: int(raw)})
    search = request.GET.get("q", "").strip()[:100]
    if search:
        queryset = queryset.filter(Q(reference__icontains=search) | Q(title__icontains=search) |
                                   Q(enrollment__intern__dit_id__icontains=search))
    return queryset


def filters():
    return {"cohorts": m.Cohort.objects.order_by("name"), "domains": m.Domain.objects.order_by("name"),
            "teams": m.Team.objects.order_by("name")}


@login_required
def dashboard(request):
    items = r.rows(query_scope(request))
    coordinator = is_coordinator(request.user)
    context = {
        "items": items[:7], "counts": r.totals(items), "team_totals": r.by_team(items) if coordinator else [],
        "today": timezone.localdate(),
    }
    if coordinator:
        context.update(filters())
        context["overdue_items"] = [item for item in items if item["overdue"]][:5]
    else:
        profile = intern_profile(request.user)
        context["unread"] = profile.received_messages.filter(read_at__isnull=True).count()
    return render(request, "dashboard.html", context)


@coordinator_required
def registry(request):
    return render(request, "registry.html", {
        **filters(), "rubrics": m.Rubric.objects.order_by("name", "version"),
    })


SETUP = {
    "cohort": (m.Cohort, ("name", "code", "id_prefix"), "New cohort", "registry"),
    "domain": (m.Domain, ("name", "code", "cohorts"), "New domain", "registry"),
    "team": (m.Team, ("name", "code", "cohort", "domain"), "New team", "registry"),
    "rubric": (m.Rubric, ("name", "version"), "New rubric version", "registry"),
    "session": (m.AttendanceSession, ("reference", "team", "date"), "New attendance session", "attendance"),
}


@coordinator_required
def setup_record(request, kind):
    if kind not in SETUP:
        raise Http404
    model, fields, title, back = SETUP[kind]
    factory = f.model_form(model, fields, {"date": f.DateInput()} if kind == "session" else None)
    form = factory(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                item = form.save()
                if kind == "session":
                    m.Attendance.objects.bulk_create([
                        m.Attendance(session=item, enrollment_id=enrollment_id, status=m.Attendance.Status.UNMARKED)
                        for enrollment_id in m.Enrollment.objects.filter(team=item.team).values_list("pk", flat=True)
                    ])
                s.audit(request.user, f"{kind}.created", item)
            messages.success(request, "Record created.")
            return redirect("rubric", pk=item.pk) if kind == "rubric" else redirect(back)
        except IntegrityError as exc:
            form_error(form, exc)
    return render_form(request, form, title, reverse(back), "Create record")


@coordinator_required
def roster(request):
    enrollments = m.Enrollment.objects.select_related("intern__user", "team", "domain", "cohort").order_by("intern__dit_id")
    term = request.GET.get("q", "")[:100]
    if term:
        enrollments = enrollments.filter(Q(intern__dit_id__icontains=term) | Q(intern__user__first_name__icontains=term) |
                                         Q(intern__user__last_name__icontains=term))
    return render(request, "roster.html", {"enrollments": enrollments})


@coordinator_required
def new_intern(request):
    form = f.RosterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            enrollment = s.register_intern(request.user, **form.cleaned_data)
            messages.success(request, f"Intern registered as {enrollment.intern.dit_id}. Share the activation link privately.")
            return redirect("edit_intern", pk=enrollment.pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Register an intern", reverse("roster"), "Register intern",
                       description="Enter an approved roster record and select how its domain is assigned.")


@coordinator_required
@never_cache
def edit_intern(request, pk):
    enrollment = get_object_or_404(m.Enrollment.objects.select_related("intern__user", "cohort", "domain", "team"), pk=pk)
    user = enrollment.intern.user
    form = f.EnrollmentForm(request.POST or None, enrollment=enrollment, initial={
        "name": user.get_full_name(), "email": user.email, "domain": enrollment.domain, "team": enrollment.team,
        "is_lead": enrollment.is_lead,
    })
    if request.method == "POST" and form.is_valid():
        try:
            s.update_enrollment(request.user, enrollment, **form.cleaned_data)
            messages.success(request, "Intern record updated.")
            return redirect("roster")
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    invite = ""
    if not user.has_usable_password():
        invite = request.build_absolute_uri(reverse("activate", args=[
            urlsafe_base64_encode(force_bytes(user.pk)), default_token_generator.make_token(user),
        ]))
    return render_form(request, form, "Edit intern", reverse("roster"), invite=invite,
                       description=f"{enrollment.intern.dit_id} · {enrollment.cohort}")


@never_cache
def activate(request, uidb64, token):
    try:
        user = get_user_model().objects.get(pk=urlsafe_base64_decode(uidb64).decode(), is_active=True)
    except (ValueError, OverflowError, get_user_model().DoesNotExist):
        raise Http404 from None
    if user.has_usable_password() or not hasattr(user, "intern") or not default_token_generator.check_token(user, token):
        raise Http404
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked = get_user_model().objects.select_for_update().get(pk=user.pk)
            if locked.has_usable_password() or not default_token_generator.check_token(locked, token):
                raise Http404
            locked.set_password(form.cleaned_data["new_password1"])
            locked.save(update_fields=["password"])
        messages.success(request, f"Account activated. Sign in as {user.username}.")
        return redirect("login")
    return render_form(request, form, "Activate your intern account", reverse("login"), "Set password",
                       description=f"Your sign-in username is {user.username}.")


@intern_required
def profile(request):
    intern = intern_profile(request.user)
    memberships = intern.enrollments.select_related("cohort", "domain", "team", "preferred_domain").prefetch_related("cohort__domains")
    if request.method == "POST":
        membership = get_object_or_404(memberships, pk=request.POST.get("enrollment", "0"))
        domain = get_object_or_404(membership.cohort.domains, pk=request.POST.get("domain", "0"))
        m.Enrollment.objects.filter(pk=membership.pk).update(preferred_domain=domain)
        messages.success(request, "Domain preference saved for the Coordinator.")
        return redirect("profile")
    return render(request, "profile.html", {"memberships": memberships})


@login_required
def tasks(request):
    items = r.rows(query_scope(request))
    state = request.GET.get("state")
    if state in ("pending", "overdue", "complete", "missing_submission"):
        items = [item for item in items if item[state]]
    return render(request, "tasks.html", {"items": items, **(filters() if is_coordinator(request.user) else {})})


def task_for(user, pk):
    queryset = m.Assignment.objects.all()
    if not is_coordinator(user):
        queryset = own_assignments(user, queryset)
    return get_object_or_404(r.records(queryset), pk=pk)


@coordinator_required
def assignment_form(request, pk=None):
    assignment = task_for(request.user, pk) if pk else None
    initial = {name: getattr(assignment, name) for name in f.AssignmentForm.base_fields
               if assignment and hasattr(assignment, name)}
    form = f.AssignmentForm(request.POST or None, initial=initial)
    if assignment:
        form.fields["reference"].disabled = True
        form.fields["enrollment"].queryset = m.Enrollment.objects.filter(pk=assignment.enrollment_id)
        form.fields["enrollment"].disabled = True
        form.fields["reason"].required = True
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        reason = values.pop("reason")
        values["reference"] = values["reference"].upper()
        try:
            with transaction.atomic():
                locked = m.Assignment.objects.select_for_update().get(pk=pk) if pk else None
                item = s.assign_task(request.user, existing=locked, **values)
                if reason:
                    s.audit(request.user, "assignment.reason", item.reference, reason)
            messages.success(request, "Assignment saved.")
            return redirect("task", pk=item.pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Edit assignment" if pk else "Assign a task", reverse("tasks"))


@login_required
def task_detail(request, pk):
    assignment = task_for(request.user, pk)
    item = r.describe(assignment, timezone.localdate())
    published_scores = m.Score.objects.filter(
        evaluation__submission__assignment=assignment, evaluation__published_at__isnull=False,
    ).select_related("criterion", "evaluation")
    by_submission = {}
    for score in published_scores:
        by_submission.setdefault(score.evaluation.submission_id, []).append(score)
    timeline = [(entry, by_submission.get(entry.pk, [])) for entry in assignment.versions]
    return render(request, "task.html", {"item": item, "assignment": assignment, "timeline": timeline})


@intern_required
@require_POST
def start_task(request, pk):
    assignment = get_object_or_404(own_assignments(request.user, m.Assignment.objects), pk=pk)
    if not assignment.cancelled and not assignment.submissions.exists():
        m.Assignment.objects.filter(pk=pk).update(started=True)
    return redirect("task", pk=pk)


@intern_required
def submit_task(request, pk):
    assignment = get_object_or_404(own_assignments(request.user, m.Assignment.objects), pk=pk)
    form = f.SubmissionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            s.submit(request.user, pk, **form.cleaned_data)
            messages.success(request, "Submission saved under your verified DIT ID.")
            return redirect("task", pk=pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Submit work", reverse("task", args=[pk]), "Submit work",
                       description=assignment.title)


@coordinator_required
def record_offline(request, pk):
    assignment = task_for(request.user, pk)
    form = f.OfflineSubmissionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        values["submitted_at"] = timezone.make_aware(datetime.combine(values.pop("submitted_date"), time.min))
        try:
            s.submit(request.user, pk, **values)
            messages.success(request, "Verified offline submission recorded.")
            return redirect("task", pk=pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Record offline submission", reverse("task", args=[pk]), "Record submission",
                       description=assignment.title)


@login_required
@never_cache
def download_artifact(request, pk):
    submissions = m.Submission.objects.all()
    if not is_coordinator(request.user):
        submissions = submissions.filter(assignment__enrollment__intern__user=request.user)
    item = get_object_or_404(submissions, pk=pk)
    if not item.artifact:
        raise Http404
    try:
        return FileResponse(item.artifact.open("rb"), as_attachment=True, filename=item.artifact_name)
    except FileNotFoundError:
        raise Http404 from None


@login_required
def assessments(request):
    items = [item for item in r.rows(query_scope(request)) if item["submission"]]
    if not is_coordinator(request.user):
        items = [item for item in items if item["published"]]
    elif request.GET.get("queue") == "review":
        items = [item for item in items if item["status"] == "Awaiting review"]
    elif request.GET.get("queue") == "incomplete":
        items = [item for item in items if item["incomplete_evaluation"]]
    return render(request, "assessments.html", {"items": items})


@coordinator_required
def evaluate(request, pk):
    submission = get_object_or_404(m.Submission.objects.select_related("assignment__rubric", "assignment__enrollment__intern"), pk=pk)
    evaluation = getattr(submission, "evaluation", None)
    if evaluation and evaluation.published_at:
        return redirect("task", pk=submission.assignment_id)
    criteria = list(submission.assignment.rubric.criteria.all())
    scores = {item.criterion_id: item for item in evaluation.scores.all()} if evaluation else {}
    form = f.EvaluationForm(request.POST or None, criteria=criteria, scores=scores)
    if request.method == "POST" and form.is_valid():
        entries = {}
        for item in criteria:
            value = form.cleaned_data[f"score_{item.pk}"]
            if value is not None:
                entries[item.pk] = (value, form.cleaned_data[f"comment_{item.pk}"], form.cleaned_data[f"feedback_{item.pk}"])
        try:
            result = s.assess(request.user, submission, entries, decision=form.cleaned_data["decision"],
                              revision_due=form.cleaned_data["revision_due"])
            messages.success(request, "Result published." if result.published_at else "Draft saved. Review the feedback before publishing.")
            return redirect("assessments") if result.published_at else redirect("evaluate", pk=pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Review submission", reverse("assessments"), "Save assessment",
                       description=f"{submission.assignment} · Submission {submission.version}",
                       evidence=submission, total=evaluation.total if evaluation else None)


@coordinator_required
def rubric(request, pk):
    item = get_object_or_404(m.Rubric, pk=pk)
    return render(request, "rubric.html", {"rubric": item, "criteria": item.criteria.all()})


@coordinator_required
def criterion(request, pk):
    item = get_object_or_404(m.Rubric, pk=pk)
    factory = f.model_form(m.Criterion, ("code", "name", "description", "weight", "minimum", "maximum", "step"))
    form = factory(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            s.add_criterion(request.user, item, **form.cleaned_data)
            messages.success(request, "Criterion added.")
            return redirect("rubric", pk=pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Add rubric criterion", reverse("rubric", args=[pk]))


@coordinator_required
@require_POST
def publish_rubric(request, pk):
    item = get_object_or_404(m.Rubric, pk=pk)
    try:
        s.publish_rubric(request.user, item)
        messages.success(request, "Rubric published and available for assignments.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("rubric", pk=pk)


@login_required
def attendance(request):
    if is_coordinator(request.user):
        sessions = m.AttendanceSession.objects.select_related("team").order_by("-date")
        return render(request, "attendance.html", {"sessions": sessions})
    if not intern_profile(request.user):
        raise PermissionDenied
    entries = list(m.Attendance.objects.filter(enrollment__intern__user=request.user).select_related("session__team").order_by("-session__date"))
    return render(request, "attendance.html", {"entries": entries, "summary": r.attendance_summary(entries)})


@coordinator_required
def mark_session(request, pk):
    session = get_object_or_404(m.AttendanceSession.objects.select_related("team"), pk=pk)
    form = f.AttendanceForm(request.POST or None, session=session)
    if request.method == "POST" and request.POST.get("action") == "close":
        session.closed = True
        session.save(update_fields=["closed"])
        s.audit(request.user, "attendance.closed", session.reference)
        messages.success(request, "Session closed.")
        return redirect("mark_session", pk=pk)
    if request.method == "POST" and form.is_valid():
        try:
            s.mark_attendance(request.user, session, **form.cleaned_data)
            messages.success(request, "Attendance saved.")
            return redirect("mark_session", pk=pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    members = m.Enrollment.objects.filter(Q(team=session.team) | Q(attendance__session=session)).select_related("intern__user").distinct()
    marks = {entry.enrollment_id: entry for entry in m.Attendance.objects.filter(session=session)}
    return render(request, "session.html", {"session": session, "form": form,
                  "marks": [(member, marks.get(member.pk)) for member in members]})


@intern_required
def team(request):
    leads = lead_memberships(request.user)
    if not leads.exists():
        raise PermissionDenied
    members = m.Enrollment.objects.filter(team_id__in=leads.values("team_id")).exclude(
        intern=intern_profile(request.user),
    ).select_related("intern__user", "team").order_by("intern__user__first_name")
    items = r.rows(team_assignments(request.user, m.Assignment.objects.all()))
    return render(request, "team.html", {"members": members, "items": items, "counts": r.totals(items)})


@intern_required
def message_member(request, pk):
    leads = lead_memberships(request.user)
    member = get_object_or_404(m.Enrollment.objects.filter(team_id__in=leads.values("team_id")).exclude(
        intern=intern_profile(request.user),
    ).select_related("intern__user"), pk=pk)
    form = f.MessageForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        s.send_team_message(request.user, pk, form.cleaned_data["body"])
        messages.success(request, "Message delivered to your teammate’s inbox.")
        return redirect("team")
    return render_form(request, form, "Message teammate", reverse("team"), "Send message",
                       description=member.intern.user.get_full_name())


@intern_required
def inbox(request):
    feed = m.TeamMessage.objects.filter(recipient=intern_profile(request.user)).select_related("sender__user", "team").order_by("-created_at")
    return render(request, "inbox.html", {"feed": feed})


@intern_required
@require_POST
def read_message(request, pk):
    item = get_object_or_404(m.TeamMessage, pk=pk, recipient=intern_profile(request.user))
    if not item.read_at:
        item.read_at = timezone.now()
        item.save(update_fields=["read_at"])
    return redirect("inbox")


def import_kinds(user):
    if is_coordinator(user):
        return [kind for kind in w.SCHEMAS if kind != "submissions"]
    if intern_profile(user):
        return ["submissions"]
    raise PermissionDenied


def allow_import(user, kind):
    if kind not in w.SCHEMAS:
        raise Http404
    if kind not in import_kinds(user):
        raise PermissionDenied


@login_required
def import_center(request):
    return render(request, "imports.html", {"kinds": [(kind, w.SCHEMAS[kind][0]) for kind in import_kinds(request.user)],
                  "batches": m.ImportBatch.objects.filter(actor=request.user).order_by("-created_at")[:12]})


@login_required
def download_template(request, kind):
    allow_import(request.user, kind)
    return w.template(kind, request.user)


@login_required
def new_batch(request, kind):
    allow_import(request.user, kind)
    if request.method == "POST":
        upload = request.FILES.get("workbook")
        staged = []
        try:
            if not upload:
                raise ValidationError("Choose the completed Excel workbook.")
            rows = w.parse(upload, kind)
            artifacts = request.FILES.getlist("artifacts") if kind == "submissions" else []
            if len(artifacts) > 50 or sum(file.size for file in artifacts) > 50 * 1024 * 1024:
                raise ValidationError("Attach up to 50 files and 50 MB per batch.")
            expected = {row["artifact"].casefold() for row in rows if kind == "submissions" and row["artifact"]}
            filenames = set()
            for file in artifacts:
                s.validate_artifact(file)
                key = file.name.casefold()
                if key in filenames or key not in expected:
                    raise ValidationError("Each attached file must have a unique filename listed in the workbook.")
                filenames.add(key)
            with transaction.atomic():
                batch = m.ImportBatch.objects.create(actor=request.user, kind=kind, rows=rows, filename=upload.name)
                for file in artifacts:
                    item = m.StagedArtifact(batch=batch, name=file.name)
                    item.file.save(file.name, file, save=False)
                    staged.append(item.file)
                    item.save()
            return redirect("batch", pk=batch.pk)
        except (ValidationError, OSError, IntegrityError) as exc:
            for file in staged:
                file.delete(save=False)
            messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else "The upload could not be saved.")
    return render(request, "upload.html", {"kind": kind, "label": w.SCHEMAS[kind][0]})


def get_batch(request, pk):
    batch = get_object_or_404(m.ImportBatch, actor=request.user, pk=pk)
    allow_import(request.user, batch.kind)
    return batch


@login_required
def batch_preview(request, pk):
    batch = get_batch(request, pk)
    result = [] if batch.committed_at else w.apply_batch(batch, dry_run=request.method != "POST")
    if request.method == "POST":
        batch.refresh_from_db()
        if batch.committed_at:
            messages.success(request, "Batch committed.")
            return redirect("batch", pk=batch.pk)
    return render(request, "batch.html", {
        "batch": batch, "result": result, "errors": sum(bool(item["error"]) for item in result),
        "label": w.SCHEMAS[batch.kind][0],
    })


@login_required
def batch_errors(request, pk):
    batch = get_batch(request, pk)
    return w.error_report(w.apply_batch(batch))


@coordinator_required
def reports(request):
    items = r.rows(query_scope(request))
    return render(request, "reports.html", {"items": [item for item in items if item["pending"]],
                  "team_totals": r.by_team(items), "counts": r.totals(items), **filters()})


@coordinator_required
@never_cache
def export(request, kind):
    if kind not in ("roster", "tasks", "follow-up", "results", "attendance"):
        raise Http404
    book = Workbook()
    sheet = book.active
    sheet.title = "Report"
    if kind == "roster":
        records = m.Enrollment.objects.select_related("intern__user", "team", "domain", "cohort")
        for key in ("team", "domain", "cohort"):
            value = request.GET.get(key, "")
            if value.isdecimal() and len(value) < 16:
                records = records.filter(**{f"{key}_id": int(value)})
        w.add_rows(sheet, [("DIT ID", "Name", "Email", "Cohort", "Domain", "Team")])
        w.add_rows(sheet, [(item.intern.dit_id, item.intern.user.get_full_name(), item.intern.user.email,
                            item.cohort.name, item.domain.name, item.team.name if item.team else "") for item in records])
    elif kind == "attendance":
        entries = m.Attendance.objects.select_related("session__team__domain", "enrollment__intern")
        for key, lookup in (("team", "session__team_id"), ("domain", "session__team__domain_id"), ("cohort", "session__team__cohort_id")):
            value = request.GET.get(key, "")
            if value.isdecimal() and len(value) < 16:
                entries = entries.filter(**{lookup: int(value)})
        w.add_rows(sheet, [("Session", "Date", "Team", "DIT ID", "Status")])
        w.add_rows(sheet, [(item.session.reference, item.session.date.isoformat(), item.session.team.name,
                            item.enrollment.intern.dit_id, item.get_status_display()) for item in entries])
    else:
        items = r.rows(query_scope(request))
        if kind == "results":
            w.add_rows(sheet, [("DIT ID", "Team", "Assignment", "Rubric", "Submission", "Score / 100", "Decision")])
            result_rows = [item for item in items if item["published"] and not item["assignment"].cancelled]
            w.add_rows(sheet, [(a.enrollment.intern.dit_id, a.team.name, a.reference, str(a.rubric),
                                item["submission"].version, item["published"].total, item["published"].get_decision_display())
                               for item in result_rows for a in [item["assignment"]]])
            summary = book.create_sheet("Team Results")
            group = {}
            for item in result_rows:
                team = item["assignment"].team
                group.setdefault(team.pk, (team, []))[1].append(item["published"].total)
            w.add_rows(summary, [("Cohort", "Team", "Released results", "Average / 100")])
            w.add_rows(summary, [(team.cohort.name, team.name, len(scores), round(sum(scores) / len(scores), 2))
                                 for team, scores in group.values()])
            w.style(summary)
        else:
            w.add_rows(sheet, [("Assignment", "Task", "DIT ID", "Team", "Domain", "Assigned", "Deadline", "Submitted",
                                "Status", "Overdue", "Late")])
            display = [item for item in items if item["pending"]] if kind == "follow-up" else items
            w.add_rows(sheet, [(a.reference, a.title, a.enrollment.intern.dit_id, a.team.name, a.domain.name,
                                a.assigned_date.isoformat(), item["deadline"].isoformat(),
                                timezone.localtime(item["submission"].submitted_at).isoformat() if item["submission"] else "",
                                item["status"], item["overdue"], item["late"])
                               for item in display for a in [item["assignment"]]])
            summary = book.create_sheet("Team Totals")
            w.add_rows(summary, [("Cohort", "Team", "Assignments", "Completed", "Pending", "Overdue", "Completion %")])
            w.add_rows(summary, [(team.cohort.name, team.name, counts["total"], counts["complete"], counts["pending"], counts["overdue"],
                                  counts["percent"]) for team, counts in r.by_team(items)])
            w.style(summary)
    metadata = book.create_sheet("Details")
    w.add_rows(metadata, [("Report", kind), ("Generated", timezone.localtime().isoformat())] +
               [(key, request.GET.get(key, "")[:100]) for key in ("cohort", "team", "domain", "q")])
    w.style(metadata)
    w.style(sheet)
    s.audit(request.user, "report.exported", kind)
    return w.workbook_response(book, f"{kind}-{timezone.localdate()}.xlsx")


@login_required
def guide(request):
    return render(request, "guide.html")
