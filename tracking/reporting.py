from django.db.models import Count, Prefetch
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
    submissions = m.Submission.objects.select_related("evaluation").annotate(
        score_count=Count("evaluation__scores")
    ).order_by("-version")
    return queryset.select_related("enrollment__intern__user", "team__cohort", "domain", "rubric").annotate(
        criterion_count=Count("rubric__criteria")
    ).prefetch_related(Prefetch("submissions", queryset=submissions, to_attr="versions"))


def describe(assignment, today):
    current = assignment.versions[0] if assignment.versions else None
    evaluation = getattr(current, "evaluation", None) if current else None
    published = evaluation if evaluation and evaluation.published_at else None
    complete = bool(published and published.decision == m.Evaluation.Decision.PASSED)
    deadline = assignment.revision_due or assignment.due_date
    awaiting = bool(not current or assignment.revision_due)
    overdue = not assignment.cancelled and not complete and awaiting and deadline < today
    status = "Not started"
    if assignment.started:
        status = "In progress"
    if current:
        status = "Awaiting review" if not published else published.get_decision_display()
    if assignment.cancelled:
        status = "Cancelled"
    return {
        "assignment": assignment,
        "submission": current,
        "published": published,
        "deadline": deadline,
        "complete": complete,
        "overdue": overdue,
        "late": bool(current and timezone.localdate(current.submitted_at) > current.deadline),
        "can_submit": awaiting and not assignment.cancelled and assignment.assigned_date <= today,
        "status": status,
        "tone": "green" if complete else "orange" if overdue else "blue" if current else "muted",
        "pending": not assignment.cancelled and not complete,
        "missing_submission": not assignment.cancelled and not current,
        "incomplete_evaluation": bool(
            not assignment.cancelled and current and not published and current.score_count < assignment.criterion_count
        ),
    }


def rows(queryset, today=None):
    date = timezone.localdate() if today is None else today
    return [describe(assignment, date) for assignment in records(queryset).order_by("due_date", "reference")]


def totals(items):
    counts = dict.fromkeys(("total", "complete", "pending", "overdue", "in_review",
                            "missing_submission", "incomplete_evaluation"), 0)
    for item in items:
        if item["assignment"].cancelled:
            continue
        counts["total"] += 1
        counts["complete"] += item["complete"]
        counts["pending"] += item["pending"]
        counts["overdue"] += item["overdue"]
        counts["in_review"] += item["status"] == "Awaiting review"
        counts["missing_submission"] += item["missing_submission"]
        counts["incomplete_evaluation"] += item["incomplete_evaluation"]
    counts["percent"] = round(100 * counts["complete"] / counts["total"], 1) if counts["total"] else None
    return counts


def by_team(items):
    teams = {}
    for item in items:
        team = item["assignment"].team
        teams.setdefault(team.pk, (team, []))[1].append(item)
    return [(team, totals(team_rows)) for team, team_rows in teams.values()]


def attendance_summary(entries):
    counts = {value: 0 for value in m.Attendance.Status.values}
    for entry in entries:
        if entry.session.closed:
            counts[entry.status] += 1
    present = counts["present"] + counts["late"]
    denominator = present + counts["absent"]
    counts["rate"] = round(100 * present / denominator, 1) if denominator else None
    return counts
