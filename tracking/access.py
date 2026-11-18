from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F


def is_coordinator(user):
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name="Coordinators").exists())


def intern_profile(user):
    return getattr(user, "intern", None) if user.is_authenticated else None


def lead_memberships(user):
    profile = intern_profile(user)
    return profile.enrollments.filter(is_lead=True, team__isnull=False) if profile else None


def navigation(request):
    profile = intern_profile(request.user)
    leads = lead_memberships(request.user)
    return {
        "coordinator": is_coordinator(request.user),
        "intern_profile": profile,
        "team_lead": leads is not None and leads.exists(),
    }


def require_role(test):
    def decorator(view):
        @login_required
        @wraps(view)
        def guarded(request, *args, **kwargs):
            if not test(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return guarded
    return decorator


coordinator_required = require_role(is_coordinator)
intern_required = require_role(intern_profile)


def own_assignments(user, queryset):
    return queryset.filter(enrollment__intern__user=user)


def team_assignments(user, queryset):
    leads = lead_memberships(user)
    if leads is None:
        return queryset.none()
    return queryset.filter(
        team_id__in=leads.values("team_id"),
        team_id=F("enrollment__team_id"),
    )
