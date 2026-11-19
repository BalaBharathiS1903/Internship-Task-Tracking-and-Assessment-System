from django import forms
from django.db.models import Q

from . import models as m


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self):
        super().__init__(format="%Y-%m-%d")


class InternDetailsForm(forms.Form):
    name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(required=False)
    college = forms.CharField(max_length=160)
    phone = forms.CharField(max_length=32, required=False, widget=forms.TextInput(attrs={"type": "tel"}))
    placement_role = forms.CharField(max_length=100, required=False)
    joined_on = forms.DateField(label="Joining date", widget=DateInput(), required=False)


class RosterForm(InternDetailsForm):
    field_order = ["reference", "name", "email", "college", "phone", "placement_role", "joined_on"]
    reference = forms.CharField(max_length=40, label="Intern reference")
    dit_id = forms.CharField(max_length=40, required=False, label="Official DIT ID",
                             help_text="Leave blank only if this batch authorizes local issuance.")
    cohort = forms.ModelChoiceField(label="Internship batch", queryset=m.Cohort.objects.order_by("name"))
    mode = forms.ChoiceField(label="Domain allocation", choices=[("manual", "Manual selection"), ("auto", "Automatic matching")],
                             help_text="Automatic matching assigns a team in the selected domain when no team is chosen.")
    domain = forms.ModelChoiceField(queryset=m.Domain.objects.order_by("name"), required=False)
    preferred_domain = forms.ModelChoiceField(queryset=m.Domain.objects.order_by("name"), required=False)
    team = forms.ModelChoiceField(queryset=m.Team.objects.order_by("name"), required=False)


class EnrollmentForm(InternDetailsForm):
    domain = forms.ModelChoiceField(queryset=m.Domain.objects.none())
    team = forms.ModelChoiceField(queryset=m.Team.objects.none(), required=False)
    is_lead = forms.BooleanField(label="Designate as Team Lead", required=False)
    reason = forms.CharField(max_length=180, label="Reason for change")

    def __init__(self, *args, enrollment, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].queryset = enrollment.cohort.domains.order_by("name")
        self.fields["team"].queryset = m.Team.objects.filter(cohort=enrollment.cohort)


class AssignmentTeamChoice(forms.ModelChoiceField):
    def label_from_instance(self, team):
        return f"{team.name} ({team.code}) · {team.cohort}"


class AssignmentForm(forms.Form):
    reference = forms.CharField(max_length=40, label="Assignment reference")
    task_code = forms.CharField(max_length=40)
    title = forms.CharField(max_length=150)
    enrollment = forms.ModelChoiceField(
        label="Intern and internship batch", required=False,
        queryset=m.Enrollment.objects.filter(team__isnull=False).select_related("intern__user", "cohort"),
    )
    team = AssignmentTeamChoice(
        queryset=m.Team.objects.select_related("cohort").order_by("name", "cohort__name", "code"),
        required=False, help_text="Assign to every current member of this team.",
    )
    assigned_date = forms.DateField(widget=DateInput(), label="Start date")
    due_date = forms.DateField(widget=DateInput())
    instructions = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    cancelled = forms.BooleanField(required=False, label="Cancel this assignment")
    reason = forms.CharField(max_length=180, required=False, label="Reason for change")

    def clean(self):
        cleaned = super().clean()
        if self.fields.get("team") and not self.errors and bool(cleaned.get("team")) == bool(cleaned.get("enrollment")):
            raise forms.ValidationError("Choose either a team or an individual intern.")
        return cleaned


class SubmissionForm(forms.Form):
    link = forms.URLField(required=False, max_length=500, label="HTTPS project link")
    artifact = forms.FileField(label="Upload document", help_text="Upload a supported work file up to 15 MB, such as a PDF or Word document.")
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=4000, label="Work details",
                           help_text="Describe what you completed and what the coordinator should check.")


class OfflineSubmissionForm(SubmissionForm):
    submitted_date = forms.DateField(widget=DateInput(), label="Verified submission date")
    reason = forms.CharField(max_length=200, label="Reason for recording offline")


class SubmissionReviewForm(forms.Form):
    decision = forms.ChoiceField(label="Review decision", choices=[("", "Choose a decision"), *m.SubmissionReview.Decision.choices])
    feedback = forms.CharField(label="Review comments", required=False, max_length=4000,
                               widget=forms.Textarea(attrs={"rows": 4}),
                               help_text="The intern can see these comments. Explain what to change if you request a revision.")
    revision_due = forms.DateField(label="Revision deadline", required=False, widget=DateInput(),
                                   help_text="Required for a revision request.")


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
