from django import forms
from django.db.models import Q

from . import models as m


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self):
        super().__init__(format="%Y-%m-%d")


class RosterForm(forms.Form):
    reference = forms.CharField(max_length=40, label="Roster reference")
    name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(required=False)
    dit_id = forms.CharField(max_length=40, required=False, label="Official DIT ID",
                             help_text="Leave blank only if this batch authorizes local issuance.")
    cohort = forms.ModelChoiceField(label="Internship batch", queryset=m.Cohort.objects.order_by("name"))
    mode = forms.ChoiceField(label="Domain allocation", choices=[("manual", "Manual selection"), ("auto", "Automatic matching")])
    domain = forms.ModelChoiceField(queryset=m.Domain.objects.order_by("name"), required=False)
    preferred_domain = forms.ModelChoiceField(queryset=m.Domain.objects.order_by("name"), required=False)
    team = forms.ModelChoiceField(queryset=m.Team.objects.order_by("name"), required=False)


class EnrollmentForm(forms.Form):
    name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(required=False)
    domain = forms.ModelChoiceField(queryset=m.Domain.objects.none())
    team = forms.ModelChoiceField(queryset=m.Team.objects.none(), required=False)
    is_lead = forms.BooleanField(label="Designate as Team Lead", required=False)
    reason = forms.CharField(max_length=180, label="Reason for change")

    def __init__(self, *args, enrollment, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].queryset = enrollment.cohort.domains.order_by("name")
        self.fields["team"].queryset = m.Team.objects.filter(cohort=enrollment.cohort)


class AssignmentForm(forms.Form):
    reference = forms.CharField(max_length=40, label="Assignment reference")
    task_code = forms.CharField(max_length=40)
    title = forms.CharField(max_length=150)
    enrollment = forms.ModelChoiceField(
        label="Intern and internship batch", queryset=m.Enrollment.objects.filter(team__isnull=False).select_related("intern__user", "cohort"),
    )
    rubric = forms.ModelChoiceField(label="Scoring guide", queryset=m.Rubric.objects.filter(published=True))
    assigned_date = forms.DateField(widget=DateInput())
    due_date = forms.DateField(widget=DateInput())
    instructions = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    cancelled = forms.BooleanField(required=False, label="Cancel this assignment")
    reason = forms.CharField(max_length=180, required=False, label="Reason for change")


class SubmissionForm(forms.Form):
    link = forms.URLField(required=False, max_length=500, label="HTTPS project link")
    artifact = forms.FileField(required=False, label="Project file")
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False, max_length=4000)


class OfflineSubmissionForm(SubmissionForm):
    submitted_date = forms.DateField(widget=DateInput(), label="Verified submission date")
    reason = forms.CharField(max_length=200, label="Reason for recording offline")


class EvaluationForm(forms.Form):
    decision = forms.ChoiceField(
        required=False, choices=[("", "Save draft"), *m.Evaluation.Decision.choices],
    )
    revision_due = forms.DateField(label="Revision deadline", required=False, widget=DateInput())

    def __init__(self, *args, criteria, scores, **kwargs):
        super().__init__(*args, **kwargs)
        for item in criteria:
            saved = scores.get(item.pk)
            self.fields[f"score_{item.pk}"] = forms.DecimalField(
                required=False, max_digits=8, decimal_places=2, label=f"{item.name} · {item.weight}%",
                initial=saved.value if saved else None,
                min_value=item.minimum, max_value=item.maximum,
                help_text=f"{item.minimum}–{item.maximum}, increments of {item.step}. {item.description}",
            )
            self.fields[f"comment_{item.pk}"] = forms.CharField(
                required=False, max_length=2000, label=f"{item.name} · Coordinator notes",
                initial=saved.comment if saved else "",
            )
            self.fields[f"feedback_{item.pk}"] = forms.CharField(
                required=False, max_length=4000, label=f"{item.name} · Feedback",
                initial=saved.feedback if saved else "", widget=forms.Textarea(attrs={"rows": 2}),
            )
        self.order_fields([name for name in self.fields if name not in ("decision", "revision_due")]
                          + ["decision", "revision_due"])


class AttendanceForm(forms.Form):
    enrollment = forms.ModelChoiceField(queryset=m.Enrollment.objects.none(), label="Intern")
    status = forms.ChoiceField(choices=m.Attendance.Status.choices)
    reason = forms.CharField(max_length=250, required=False, label="Correction reason")

    def __init__(self, *args, session, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["enrollment"].queryset = m.Enrollment.objects.filter(
            Q(team=session.team) | Q(attendance__session=session)
        ).select_related("intern__user", "cohort").distinct()


class MessageForm(forms.Form):
    body = forms.CharField(label="Message", max_length=1500, widget=forms.Textarea(attrs={"rows": 5}))


def model_form(model, fields, widgets=None):
    return forms.modelform_factory(model, fields=fields, widgets=widgets)
