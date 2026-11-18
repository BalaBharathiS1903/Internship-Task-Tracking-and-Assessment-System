from django.db.models import Prefetch
from django.utils import timezone

from . import models as m

METRICS = [
    ("total", "Assignments"),
    ("complete", "Completed"),
    ("pending", "Pending"),
    ("overdue", "Overdue"),
]


def records(queryset=None):
    queryset = m.Assignment.objects.all() if queryset is None else queryset
    submissions = m.Submission.objects.select_related("review").order_by("-version")
    return queryset.select_related("enrollment__intern__user", "team__cohort", "domain").prefetch_related(
        Prefetch("submissions", queryset=submissions, to_attr="versions"),
    )


def describe(assignment, today):
    current = assignment.versions[0] if assignment.versions else None
    review = getattr(current, "review", None) if current else None
    complete = bool(review and review.decision == m.SubmissionReview.Decision.COMPLETED and not assignment.cancelled)
    deadline = assignment.revision_due or assignment.due_date
    awaiting = bool(not current or assignment.revision_due)
    overdue = not assignment.cancelled and not complete and awaiting and deadline < today
    status = "Not started"
    if assignment.started:
        status = "In progress"
    if current:
        status = review.get_decision_display() if review else "Awaiting review"
    if assignment.cancelled:
        status = "Cancelled"
    return {
        "assignment": assignment,
        "submission": current,
        "review": review,
        "deadline": deadline,
        "complete": complete,
        "overdue": overdue,
        "late": bool(current and timezone.localdate(current.submitted_at) > current.deadline),
        "can_submit": awaiting and not assignment.cancelled and assignment.assigned_date <= today,
        "status": status,
        "review_status": review.get_decision_display() if review else "Awaiting review" if current else "Not submitted",
        "tone": "green" if complete else "orange" if overdue else "blue" if current else "muted",
        "pending": not assignment.cancelled and not complete,
        "missing_submission": not assignment.cancelled and not current,
    }


def rows(queryset, today=None):
    date = timezone.localdate() if today is None else today
    return [describe(assignment, date) for assignment in records(queryset).order_by("due_date", "reference")]


def totals(items):
    counts = dict.fromkeys(("total", "complete", "pending", "overdue", "in_review",
                            "missing_submission"), 0)
    for item in items:
        if item["assignment"].cancelled:
            continue
        counts["total"] += 1
        counts["complete"] += item["complete"]
        counts["pending"] += item["pending"]
        counts["overdue"] += item["overdue"]
        counts["in_review"] += item["status"] == "Awaiting review"
        counts["missing_submission"] += item["missing_submission"]
    counts["percent"] = round(100 * counts["complete"] / counts["total"], 1) if counts["total"] else None
    return counts


def by_team(items):
    teams = {}
    for item in items:
        team = item["assignment"].team
        teams.setdefault(team.pk, (team, []))[1].append(item)
    return [(team, totals(team_rows)) for team, team_rows in teams.values()]


def team_summaries(teams, items, *, college=""):
    totals_by_id = {team.pk: counts for team, counts in by_team(items)}
    summaries = {team.pk: {"team": team, "counts": totals_by_id.get(team.pk, totals([])),
                           "members": 0, "leader": None} for team in teams}
    for enrollment in m.Enrollment.objects.filter(team_id__in=summaries).select_related("intern__user"):
        summary = summaries[enrollment.team_id]
        if not college or enrollment.intern.college == college:
            summary["members"] += 1
        if enrollment.is_lead:
            summary["leader"] = enrollment.intern
    return [summary for summary in summaries.values() if not college or summary["members"] or summary["counts"]["total"]]


def attendance_summary(entries):
    counts = {value: 0 for value in m.Attendance.Status.values}
    for entry in entries:
        if entry.session.closed:
            counts[entry.status] += 1
    present = counts["present"] + counts["late"]
    denominator = present + counts["absent"]
    counts["rate"] = round(100 * present / denominator, 1) if denominator else None
    return counts
