from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from openpyxl import Workbook

from . import workbooks
from .access import coordinator_required, intern_profile, is_coordinator
from .models import AuditEvent, EmailNotification, Enrollment, Team
from .notifications import active_welcome_password, delivery_enabled, pending_notices, wake_delivery
from .services import audit, update_enrollment


class NewCoordinatorForm(forms.ModelForm):
    name = forms.CharField(label="Full name", max_length=150)
    password = forms.CharField(widget=forms.PasswordInput, strip=False)

    class Meta:
        model = get_user_model()
        fields = ("name", "username", "email", "password")

    def clean_password(self):
        from django.contrib.auth.password_validation import validate_password
        value = self.cleaned_data["password"]
        self.instance.username = self.cleaned_data.get("username", "")
        self.instance.email = self.cleaned_data.get("email", "")
        validate_password(value, self.instance)
        return value

    def save(self):
        user = super().save(commit=False)
        user.first_name, _, user.last_name = self.cleaned_data["name"].partition(" ")
        user.set_password(self.cleaned_data["password"])
        user.save()
        user.groups.add(Group.objects.get_or_create(name="Coordinators")[0])
        return user


class DesignateLeadForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.order_by("name"), label="Team")
    member = forms.ModelChoiceField(queryset=Enrollment.objects.filter(team__isnull=False).select_related(
        "intern__user", "cohort", "team",
    ), label="Registered team member")

    def clean(self):
        data = super().clean()
        team, member = data.get("team"), data.get("member")
        if team and member and team.pk != member.team_id:
            self.add_error("member", "Choose an intern who already belongs to the selected team.")
        return data


def account_role(user):
    if user.is_superuser:
        return "Administrator"
    if any(group.name == "Coordinators" for group in user.groups.all()):
        return "Coordinator"
    intern = intern_profile(user)
    if intern and any(member.is_lead for member in intern.enrollments.all()):
        return "Team Lead"
    return "Intern" if intern else "User"


def can_manage(actor, target):
    return target.pk != actor.pk and not target.is_superuser and (
        actor.is_superuser or (not target.is_staff and not is_coordinator(target))
    )


def set_password_changed(user):
    intern = intern_profile(user)
    if intern and intern.must_change_password:
        intern.must_change_password = False
        intern.save(update_fields=["must_change_password"])


@login_required
@never_cache
def account_settings(request):
    actor = request.user
    password_form = PasswordChangeForm(actor)
    create_form = NewCoordinatorForm() if actor.is_superuser else None
    lead_form = DesignateLeadForm() if is_coordinator(actor) else None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "password":
            password_form = PasswordChangeForm(actor, request.POST)
            if password_form.is_valid():
                with transaction.atomic():
                    account = password_form.save()
                    set_password_changed(account)
                    audit(actor, "account.password_changed", f"User {actor.pk}")
                update_session_auth_hash(request, account)
                messages.success(request, "Password updated.")
                return redirect("account_settings")
        elif action == "add_coordinator":
            if not actor.is_superuser:
                raise PermissionDenied
            create_form = NewCoordinatorForm(request.POST)
            if create_form.is_valid():
                try:
                    with transaction.atomic():
                        account = create_form.save()
                        audit(actor, "account.coordinator_created", f"User {account.pk}")
                except IntegrityError:
                    create_form.add_error("username", "That username is already in use.")
                else:
                    messages.success(request, "Coordinator account created.")
                    return redirect("account_settings")
        elif action == "designate_lead":
            if lead_form is None:
                raise PermissionDenied
            lead_form = DesignateLeadForm(request.POST)
            if lead_form.is_valid():
                member = lead_form.cleaned_data["member"]
                user = member.intern.user
                try:
                    update_enrollment(actor, member, name=user.get_full_name(), email=user.email,
                                      domain=member.domain, team=member.team, is_lead=True,
                                      reason="Team Lead designated in Settings.")
                except ValidationError as exc:
                    lead_form.add_error(None, " ".join(exc.messages))
                else:
                    messages.success(request, "Team Lead designation updated.")
                    return redirect("account_settings")
        else:
            raise PermissionDenied

    profile = intern_profile(actor)
    coordinator = is_coordinator(actor)
    context = {
        "password_form": password_form, "account_role": account_role(actor),
        "create_form": create_form, "lead_form": lead_form,
        "temporary_login": profile and profile.must_change_password,
    }
    if coordinator:
        accounts = get_user_model().objects.prefetch_related("groups", "intern__enrollments__team").order_by("username")
        page = Paginator(accounts, 20).get_page(request.GET.get("page"))
        context.update(
            page=page,
            accounts=[{"user": user, "role": account_role(user), "manageable": can_manage(actor, user),
                       "teams": [member.team for member in user.intern.enrollments.all() if member.team]
                       if intern_profile(user) else []} for user in page],
            recent=AuditEvent.objects.select_related("actor").order_by("-created_at")[:60],
            mail_enabled=delivery_enabled(), queued=pending_notices().count(),
            notices=EmailNotification.objects.order_by("-created_at")[:12],
        )
    return render(request, "account_settings.html", context)


@coordinator_required
@require_POST
@never_cache
def download_users(request):
    notices = EmailNotification.objects.filter(
        kind=EmailNotification.Kind.WELCOME, closed_at__isnull=True, recipient=F("user__email"),
        user__is_active=True, user__is_staff=False, user__is_superuser=False, user__intern__must_change_password=True,
    ).exclude(user__groups__name="Coordinators").select_related("user__intern").order_by("user_id", "-created_at", "-pk")
    latest = {}
    for notice in notices:
        latest.setdefault(notice.user_id, notice)

    rows = [("Name", "Username", "Temporary password", "Password status")]
    password_count = 0
    for user in get_user_model().objects.select_related("intern").order_by("username"):
        notice = latest.get(user.pk)
        password = active_welcome_password(notice) if notice else ""
        if not user.is_active:
            status = "Inactive"
        elif password:
            status = "Change at first sign-in"
            password_count += 1
        elif not user.has_usable_password():
            status = "Activation required"
        elif intern_profile(user) and user.intern.must_change_password:
            status = "Temporary password unavailable"
        else:
            status = "Password set"
        rows.append((user.get_full_name() or user.username, user.username, password, status))

    book = Workbook()
    sheet = book.active
    sheet.title = "User management"
    workbooks.add_rows(sheet, rows)
    workbooks.style(sheet)
    audit(request.user, "account.credentials_exported", "User management",
          f"{len(rows) - 1} accounts; {password_count} temporary passwords")
    return workbooks.workbook_response(book, f"user-management-{timezone.localdate()}.xlsx")


@coordinator_required
@never_cache
def manage_user(request, pk, action):
    target = get_object_or_404(get_user_model(), pk=pk)
    if not can_manage(request.user, target):
        raise PermissionDenied
    if action == "password":
        form = SetPasswordForm(target, request.POST or None)
        if request.method == "POST" and form.is_valid():
            with transaction.atomic():
                account = form.save()
                set_password_changed(account)
                audit(request.user, "account.password_reset", f"User {target.pk}")
            messages.success(request, "Account password reset.")
            return redirect("account_settings")
        return render(request, "form.html", {"form": form, "title": "Reset account password",
                      "description": target.username, "back": reverse("account_settings"), "button": "Reset password"})
    if action == "status":
        if request.method != "POST":
            raise PermissionDenied
        with transaction.atomic():
            target.is_active = not target.is_active
            target.save(update_fields=["is_active"])
            audit(request.user, "account.activated" if target.is_active else "account.deactivated", f"User {target.pk}")
        messages.success(request, "Account status updated.")
    elif action == "delete":
        if request.method != "POST":
            return render(request, "account_delete.html", {"target": target})
        try:
            with transaction.atomic():
                target.delete()
                audit(request.user, "account.deleted", f"User {pk}")
        except ProtectedError:
            messages.error(request, "This account has linked records. Deactivate it to keep its history.")
        else:
            messages.success(request, "Account deleted.")
    else:
        raise PermissionDenied
    return redirect("account_settings")


@coordinator_required
@require_POST
def retry_notifications(request):
    if not delivery_enabled():
        messages.error(request, "Configure an email provider in the service environment first.")
    else:
        wake_delivery()
        messages.success(request, "Queued emails that are ready will be sent.")
    return redirect("account_settings")
