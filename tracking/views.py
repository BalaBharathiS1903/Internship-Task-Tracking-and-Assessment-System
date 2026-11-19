from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Value
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Concat
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
from . import filters as fl
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
    queryset = fl.scoped_ids(queryset, request.GET, {"cohort": "team__cohort_id", "domain": "domain_id", "team": "team_id"})
    search = request.GET.get("q", "").strip()[:100]
    if search:
        queryset = queryset.annotate(intern_name=Concat("enrollment__intern__user__first_name", Value(" "),
                                                       "enrollment__intern__user__last_name"))
        queryset = queryset.filter(Q(reference__icontains=search) | Q(title__icontains=search) |
                                   Q(enrollment__intern__dit_id__icontains=search) |
                                   Q(intern_name__icontains=search))
    if college := request.GET.get("college", "")[:160]:
        queryset = queryset.filter(enrollment__intern__college=college)
    return queryset


def filters():
    return {"cohorts": m.Cohort.objects.order_by("name"), "domains": m.Domain.objects.order_by("name"),
            "teams": m.Team.objects.order_by("name"), "colleges": fl.college_options()}


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
    return render(request, "registry.html", filters())


SETUP = {
    "cohort": (m.Cohort, ("name", "code", "id_prefix"), "New internship batch", "registry"),
    "domain": (m.Domain, ("name", "code", "cohorts"), "New domain", "registry"),
    "team": (m.Team, ("name", "code", "cohort", "domain", "description"), "New team", "teams"),
    "session": (m.AttendanceSession, ("reference", "team", "date"), "New attendance session", "attendance"),
}


@coordinator_required
def setup_record(request, kind):
    if kind not in SETUP:
        raise Http404
    model, fields, title, back = SETUP[kind]
    factory = f.model_form(model, fields, {"date": f.DateInput()} if kind == "session" else None)
    form = factory(request.POST or None)
    if "cohort" in form.fields:
        form.fields["cohort"].label = "Internship batch"
    if "cohorts" in form.fields:
        form.fields["cohorts"].label = "Internship batches"
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
            return redirect(back)
        except IntegrityError as exc:
            form_error(form, exc)
    return render_form(request, form, title, reverse(back), "Create record", form_grid=True)


@coordinator_required
def roster(request):
    page = Paginator(fl.roster_records(request.GET), 20).get_page(request.GET.get("page"))
    return render(request, "roster.html", {"enrollments": page, "page": page, **filters()})


@coordinator_required
def new_intern(request):
    form = f.RosterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            enrollment = s.register_intern(request.user, **form.cleaned_data)
            followup = ("The login email was queued." if enrollment.intern.user.email else
                        "Share the activation link privately or add the intern's email address.")
            messages.success(request, f"Intern registered as {enrollment.intern.dit_id}. {followup}")
            return redirect("edit_intern", pk=enrollment.pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Register an intern", reverse("roster"), "Register intern",
                       description="Add the intern’s email address to send their username and temporary password.",
                       form_grid=True)


@coordinator_required
@never_cache
def edit_intern(request, pk):
    enrollment = get_object_or_404(m.Enrollment.objects.select_related("intern__user", "cohort", "domain", "team"), pk=pk)
    user = enrollment.intern.user
    form = f.EnrollmentForm(request.POST or None, enrollment=enrollment, initial={
        "name": user.get_full_name(), "email": user.email, "domain": enrollment.domain, "team": enrollment.team,
        "is_lead": enrollment.is_lead, "college": enrollment.intern.college, "phone": enrollment.intern.phone,
        "placement_role": enrollment.intern.placement_role, "joined_on": enrollment.intern.joined_on,
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
                       description=f"{enrollment.intern.dit_id} · {enrollment.cohort}", form_grid=True)


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
    coordinator = is_coordinator(request.user)
    form = fl.TaskFilters(request.GET) if coordinator else None
    if form:
        form.is_valid()
    items = fl.task_rows(items, form.cleaned_data if form else {}, request.GET.get("state"), sort_newest=coordinator)
    page = Paginator(items, 20).get_page(request.GET.get("page"))
    return render(request, "tasks.html", {"items": page, "page": page, "task_filters": form})


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
        del form.fields["team"]
        form.fields["reference"].disabled = True
        form.fields["enrollment"].queryset = m.Enrollment.objects.filter(pk=assignment.enrollment_id)
        form.fields["enrollment"].disabled = True
        form.fields["enrollment"].required = True
        form.fields["reason"].required = True
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        reason = values.pop("reason")
        team = values.pop("team", None)
        values["reference"] = values["reference"].upper()
        try:
            if team:
                values.pop("enrollment")
                with transaction.atomic():
                    items = s.assign_team_task(request.user, team=team, **values)
                    if reason:
                        for item in items:
                            s.audit(request.user, "assignment.reason", item.reference, reason)
                messages.success(request, f"Task assigned to {len(items)} members of {team.name}.")
                return redirect(f"{reverse('tasks')}?team={team.pk}")
            with transaction.atomic():
                locked = m.Assignment.objects.select_for_update().get(pk=pk) if pk else None
                item = s.assign_task(request.user, existing=locked, **values)
                if reason:
                    s.audit(request.user, "assignment.reason", item.reference, reason)
            messages.success(request, "Assignment saved." + (
                " The task email was queued." if locked is None and item.enrollment.intern.user.email else ""
            ))
            return redirect("task", pk=item.pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Edit assignment" if pk else "Assign a task", reverse("tasks"), form_grid=True)


@login_required
def task_detail(request, pk):
    assignment = task_for(request.user, pk)
    item = r.describe(assignment, timezone.localdate())
    return render(request, "task.html", {"item": item, "assignment": assignment, "timeline": assignment.versions})


@intern_required
@require_POST
def start_task(request, pk):
    assignments = own_assignments(request.user, m.Assignment.objects)
    get_object_or_404(assignments, pk=pk)
    assignments.filter(pk=pk, cancelled=False, assigned_date__lte=timezone.localdate(), submissions__isnull=True).update(started=True)
    return redirect("task", pk=pk)


@intern_required
def submit_task(request, pk):
    assignment = get_object_or_404(own_assignments(request.user, m.Assignment.objects), pk=pk)
    form = f.SubmissionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            s.submit(request.user, pk, **form.cleaned_data)
            messages.success(request, "Work submitted for coordinator review.")
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
def reviews(request):
    coordinator = is_coordinator(request.user)
    items = [item for item in r.rows(query_scope(request)) if item["submission"]]
    if coordinator and request.GET.get("queue") == "review":
        items = [item for item in items if item["status"] == "Awaiting review"]
    page = Paginator(items, 20).get_page(request.GET.get("page"))
    return render(request, "reviews.html", {"items": page, "page": page, **(filters() if coordinator else {})})


@coordinator_required
def review_submission(request, pk):
    submission = get_object_or_404(m.Submission.objects.select_related("assignment__enrollment__intern__user", "review"), pk=pk)
    assignment = submission.assignment
    if assignment.cancelled or hasattr(submission, "review") or assignment.submissions.first() != submission:
        return redirect("task", pk=assignment.pk)
    form = f.SubmissionReviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            review = s.review_submission(request.user, submission, **form.cleaned_data)
            messages.success(request, "Task marked complete." if review.decision == m.SubmissionReview.Decision.COMPLETED
                             else "Revision requested.")
            return redirect("task", pk=assignment.pk)
        except (ValidationError, IntegrityError) as exc:
            form_error(form, exc)
    return render_form(request, form, "Verify submission", reverse("task", args=[assignment.pk]), "Send status to intern",
                       description=f"{assignment} · Submission {submission.version} · {assignment.enrollment.intern}",
                       evidence=submission)


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
                  "batches": m.ImportBatch.objects.filter(actor=request.user, kind__in=w.SCHEMAS).order_by("-created_at")[:12]})


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
            rows, setup_rows = w.parse(upload, kind, include_setup=kind == "roster" and request.POST.get("include_setup") == "1")
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
                batch = m.ImportBatch.objects.create(actor=request.user, kind=kind, rows=rows,
                                                     setup_rows=setup_rows, filename=upload.name)
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
def teams(request):
    directory = m.Team.objects.select_related("cohort", "domain").order_by("name", "pk")
    return render(request, "teams.html", {"summaries": r.team_summaries(directory, r.rows(m.Assignment.objects.all()))})


@coordinator_required
def team_detail(request, pk):
    team_record = get_object_or_404(m.Team.objects.select_related("cohort", "domain"), pk=pk)
    items = r.rows(query_scope(request).filter(team=team_record))
    summary = r.team_summaries([team_record], items)[0]
    members = m.Enrollment.objects.filter(team=team_record).select_related("intern__user").order_by("intern__dit_id")
    if college := request.GET.get("college", "")[:160]:
        members = members.filter(intern__college=college)
        summary["members"] = members.count()
    return render(request, "team_detail.html", {
        "record": team_record, "summary": summary, "counts": summary["counts"], "members": members, "items": items,
        "printable": request.resolver_match.url_name == "team_report",
    })


@coordinator_required
def edit_team(request, pk):
    team_record = get_object_or_404(m.Team, pk=pk)
    factory = f.model_form(m.Team, SETUP["team"][1])
    form = factory(request.POST or None, instance=team_record)
    form.fields["cohort"].label = "Internship batch"
    if team_record.enrollment_set.exists() or team_record.assignment_set.exists():
        for field in ("cohort", "domain"):
            form.fields[field].disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                updated = form.save()
                s.audit(request.user, "team.updated", updated.pk)
            messages.success(request, "Team updated.")
            return redirect("team_detail", pk=pk)
        except IntegrityError as exc:
            form_error(form, exc)
    return render_form(request, form, "Edit team", reverse("team_detail", args=[pk]), form_grid=True)


@coordinator_required
def delete_team(request, pk):
    team_record = get_object_or_404(m.Team, pk=pk)
    if request.method == "POST":
        try:
            with transaction.atomic():
                team_record.delete()
                s.audit(request.user, "team.deleted", pk)
        except (ProtectedError, IntegrityError):
            messages.error(request, "This team is still referenced by members, tasks, attendance, or messages.")
            return redirect("team_detail", pk=pk)
        messages.success(request, "Team deleted.")
        return redirect("teams")
    return render(request, "team_delete.html", {"record": team_record})


REPORT_CARDS = [
    ("roster", "Intern Master", "Intern details, team placement, and task counts."),
    ("allocation", "Team Allocation", "Team-wise intern allocation and designated leads."),
    ("tasks", "Complete Task Tracker", "Assignments, deadlines, submissions, and review status."),
    ("completed", "Completed Tasks", "Assignments verified and marked complete by the coordinator."),
    ("follow-up", "Pending Tasks", "Open assignments that still need action."),
    ("overdue", "Overdue Tasks", "Unfinished assignments with missed submission deadlines."),
    ("verification", "Submission Review Report", "Verification status, review dates, and coordinator comments."),
    ("team-lead", "Team Lead Report", "Team membership, completion, overdue work, and submissions awaiting review."),
]


def report_teams(params, items):
    teams = fl.scoped_ids(m.Team.objects.select_related("cohort", "domain").order_by("name"), params,
                          {"cohort": "cohort_id", "domain": "domain_id", "team": "pk"})
    return r.team_summaries(teams, items, college=params.get("college", "")[:160])


@coordinator_required
def reports(request):
    items = r.rows(query_scope(request))
    return render(request, "reports.html", {"cards": REPORT_CARDS, "summaries": report_teams(request.GET, items), **filters()})


@coordinator_required
@never_cache
def export(request, kind):
    if kind not in {kind for kind, _, _ in REPORT_CARDS} | {"attendance"}:
        raise Http404
    book = Workbook()
    sheet = book.active
    sheet.title = "Report"
    if kind in ("roster", "allocation"):
        records = fl.roster_records(request.GET)
        if kind == "roster":
            by_intern = {}
            for item in r.rows(m.Assignment.objects.filter(enrollment__in=records)):
                if not item["assignment"].cancelled:
                    counts = by_intern.setdefault(item["assignment"].enrollment_id, [0, 0])
                    counts[0] += 1
                    counts[1] += item["complete"]
            w.add_rows(sheet, [("DIT ID", "Name", "Email", "Phone", "College", "Placement role", "Joined",
                                "Internship batch", "Domain", "Team", "Tasks", "Completed")])
            w.add_rows(sheet, [(item.intern.dit_id, item.intern.user.get_full_name(), item.intern.user.email, item.intern.phone,
                                item.intern.college, item.intern.placement_role, item.intern.joined_on,
                                item.cohort.name, item.domain.name, item.team.name if item.team else "",
                                *by_intern.get(item.pk, (0, 0))) for item in records])
        else:
            w.add_rows(sheet, [("Team", "Internship batch", "Domain", "DIT ID", "Intern", "College", "Role")])
            w.add_rows(sheet, [(item.team.name, item.cohort.name, item.domain.name, item.intern.dit_id,
                                item.intern.user.get_full_name(), item.intern.college, "Team Lead" if item.is_lead else "Member")
                               for item in records.filter(team__isnull=False).order_by("team__name", "-is_lead", "intern__dit_id")])
    elif kind == "attendance":
        entries = m.Attendance.objects.select_related("session__team__domain", "enrollment__intern__user")
        for key, lookup in (("team", "session__team_id"), ("domain", "session__team__domain_id"), ("cohort", "session__team__cohort_id")):
            value = request.GET.get(key, "")
            if value.isdecimal() and len(value) < 16:
                entries = entries.filter(**{lookup: int(value)})
        if college := request.GET.get("college", "")[:160]:
            entries = entries.filter(enrollment__intern__college=college)
        w.add_rows(sheet, [("Session", "Date", "Team", "DIT ID", "Intern", "College", "Status")])
        w.add_rows(sheet, [(item.session.reference, item.session.date.isoformat(), item.session.team.name,
                            item.enrollment.intern.dit_id, item.enrollment.intern.user.get_full_name(),
                            item.enrollment.intern.college, item.get_status_display()) for item in entries])
    else:
        items = r.rows(query_scope(request))
        form = fl.TaskFilters(request.GET)
        form.is_valid()
        items = fl.task_rows(items, form.cleaned_data, request.GET.get("state"))
        if kind == "team-lead":
            summaries = report_teams(request.GET, items)
            w.add_rows(sheet, [("Internship batch", "Team", "Leader", "Members", "Total", "Completed", "Pending",
                                "Overdue", "Awaiting review", "Completion %")])
            w.add_rows(sheet, [(row["team"].cohort.name, row["team"].name,
                               row["leader"].user.get_full_name() if row["leader"] else "", row["members"],
                               *[row["counts"][key] for key in ("total", "complete", "pending", "overdue", "in_review", "percent")])
                               for row in summaries])
            member_sheet = book.create_sheet("Members")
            members = m.Enrollment.objects.filter(team_id__in=[row["team"].pk for row in summaries]).select_related(
                "team", "intern__user").order_by("team__name", "-is_lead", "intern__dit_id")
            if college := request.GET.get("college", "")[:160]:
                members = members.filter(intern__college=college)
            w.add_rows(member_sheet, [("Team", "DIT ID", "Intern", "College", "Role")])
            w.add_rows(member_sheet, [(member.team.name, member.intern.dit_id, member.intern.user.get_full_name(),
                                       member.intern.college, "Team Lead" if member.is_lead else "Member") for member in members])
            w.style(member_sheet)
        elif kind == "verification":
            w.add_rows(sheet, [("DIT ID", "Intern", "College", "Team", "Assignment", "Submitted", "Review status",
                                "Reviewed on", "Review comments")])
            for item in items:
                submission, assignment, review = item["submission"], item["assignment"], item["review"]
                if submission and not assignment.cancelled:
                    intern = assignment.enrollment.intern
                    w.add_rows(sheet, [(intern.dit_id, intern.user.get_full_name(), intern.college, assignment.team.name,
                                        assignment.reference, timezone.localdate(submission.submitted_at), item["review_status"],
                                        timezone.localdate(review.published_at) if review else None,
                                        review.feedback if review else "")])
        else:
            w.add_rows(sheet, [("Assignment", "Task", "DIT ID", "College", "Team", "Domain", "Start date", "Due date", "Submitted",
                                "Status", "Review status", "Overdue", "Late")])
            selected = {"follow-up": "pending", "completed": "complete", "overdue": "overdue"}.get(kind)
            display = [item for item in items if item[selected] and not item["assignment"].cancelled] if selected else items
            w.add_rows(sheet, [(a.reference, a.title, a.enrollment.intern.dit_id, a.enrollment.intern.college, a.team.name, a.domain.name,
                                a.assigned_date.isoformat(), item["deadline"].isoformat(),
                                timezone.localtime(item["submission"].submitted_at).isoformat() if item["submission"] else "",
                                item["status"], item["review_status"], item["overdue"], item["late"])
                               for item in display for a in [item["assignment"]]])
            summary = book.create_sheet("Team Totals")
            w.add_rows(summary, [("Internship batch", "Team", "Assignments", "Completed", "Pending", "Overdue", "Completion %")])
            w.add_rows(summary, [(team.cohort.name, team.name, counts["total"], counts["complete"], counts["pending"], counts["overdue"],
                                  counts["percent"]) for team, counts in r.by_team(items)])
            w.style(summary)
    metadata = book.create_sheet("Details")
    report_name = "Intern Master" if kind == "roster" else kind
    w.add_rows(metadata, [("Report", report_name), ("Generated", timezone.localtime().isoformat())] +
               [("Internship batch" if key == "cohort" else key, request.GET.get(key, "")[:160]) for key in
                ("cohort", "team", "domain", "q", "college", "state", "task_status", "review_status",
                 "date_on", "date_from", "date_to", "sort")])
    w.style(metadata)
    w.style(sheet)
    s.audit(request.user, "report.exported", kind)
    filename = "intern-master" if kind == "roster" else kind
    return w.workbook_response(book, f"{filename}-{timezone.localdate()}.xlsx")


@login_required
def guide(request):
    return render(request, "guide.html")
