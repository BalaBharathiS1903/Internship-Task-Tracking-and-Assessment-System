from django import forms
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.utils import timezone

from . import models as m
from .forms import DateInput


def scoped_ids(queryset, params, fields):
    for key, lookup in fields.items():
        value = params.get(key, "")
        if value.isdecimal() and len(value) < 16:
            queryset = queryset.filter(**{lookup: int(value)})
    return queryset


def roster_records(params):
    queryset = scoped_ids(
        m.Enrollment.objects.select_related("intern__user", "team", "domain", "cohort"),
        params, {"team": "team_id", "domain": "domain_id", "cohort": "cohort_id"},
    )
    term = params.get("q", "").strip()[:100]
    if term:
        queryset = queryset.annotate(intern_name=Concat("intern__user__first_name", Value(" "), "intern__user__last_name")).filter(
            Q(intern__dit_id__icontains=term) | Q(intern_name__icontains=term) | Q(intern__user__email__icontains=term) |
            Q(intern__college__icontains=term)
        )
    if college := params.get("college", "")[:160]:
        queryset = queryset.filter(intern__college=college)
    return queryset.order_by("intern__dit_id", "pk")


def college_options():
    return m.Intern.objects.exclude(college="").order_by("college").values_list("college", flat=True).distinct()


TASK_STATUSES = (
    ("Not started", "Not started"), ("In progress", "In progress"), ("Awaiting review", "Submitted"),
    ("Under review", "Under review"), ("Passed", "Completed"), ("Needs revision", "Needs revision"),
    ("Overdue", "Overdue"), ("Cancelled", "Cancelled"),
)


class TaskFilters(forms.Form):
    q = forms.CharField(label="Search DIT ID, intern, or task", required=False, max_length=100,
                        widget=forms.TextInput(attrs={"placeholder": "DIT2026004, Priya, API…"}))
    team = forms.ModelChoiceField(label="Team", queryset=m.Team.objects.order_by("name"), required=False,
                                  empty_label="All teams")
    college = forms.ChoiceField(required=False, label="College")
    task_status = forms.ChoiceField(required=False, label="Task status", choices=[("", "All statuses"), *TASK_STATUSES])
    assessment_status = forms.ChoiceField(required=False, label="Assessment status", choices=[
        ("", "All"), ("Pending", "Pending"), ("Under review", "Under review"),
        ("Passed", "Passed"), ("Needs revision", "Needs revision"),
    ])
    date_on = forms.ChoiceField(required=False, label="Date range on", choices=[
        ("due", "Due date"), ("assigned", "Assigned date"), ("submitted", "Submission date"),
    ])
    date_from = forms.DateField(required=False, label="From", widget=DateInput())
    date_to = forms.DateField(required=False, label="To", widget=DateInput())
    sort = forms.ChoiceField(required=False, label="Sort by", choices=[
        ("newest", "Newest first"), ("oldest", "Oldest first"), ("due", "Earliest deadline"),
    ])
    cohort = forms.ModelChoiceField(label="Internship batch", queryset=m.Cohort.objects.order_by("name"),
                                    required=False, empty_label="All batches")
    domain = forms.ModelChoiceField(queryset=m.Domain.objects.order_by("name"), required=False,
                                    empty_label="All domains")

    def __init__(self, data):
        super().__init__(data)
        self.fields["college"].choices = [("", "All colleges"), *((value, value) for value in college_options())]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end date must be on or after the start date.")
        return cleaned


def task_rows(items, cleaned, state=None, *, sort_newest=False):
    if state in ("pending", "overdue", "complete", "missing_submission"):
        items = [item for item in items if item[state]]
    if status := cleaned.get("task_status"):
        items = [item for item in items if ("Overdue" if item["overdue"] else item["status"]) == status]
    if assessment := cleaned.get("assessment_status"):
        items = [item for item in items if item["assessment_status"] == assessment]
    start, end = cleaned.get("date_from"), cleaned.get("date_to")
    if start or end:
        date_on = cleaned.get("date_on") or "due"
        filtered = []
        for item in items:
            value = item["deadline"]
            if date_on == "assigned":
                value = item["assignment"].assigned_date
            if date_on == "submitted":
                value = timezone.localdate(item["submission"].submitted_at) if item["submission"] else None
            if value and (not start or value >= start) and (not end or value <= end):
                filtered.append(item)
        items = filtered
    order = cleaned.get("sort") or ("newest" if sort_newest else "due")
    if order in ("newest", "oldest"):
        items = sorted(items, key=lambda item: (item["assignment"].assigned_date, item["assignment"].pk),
                       reverse=order == "newest")
    return items
