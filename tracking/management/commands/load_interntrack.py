import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from tracking import models as m

COHORT_CODE = "INTERNTRACK-QA"
STATUSES = {
    "Not Started": (None, False),
    "In Progress": (None, True),
    "Submitted": ("Pending", True),
    "Under Review": ("Under Review", True),
    "Completed": ("Approved", True),
    "Rejected": ("Rejected", True),
}
DECISIONS = {"Approved": m.Evaluation.Decision.PASSED, "Rejected": m.Evaluation.Decision.REVISION}


def read_package(path):
    if not path.is_file():
        raise CommandError(f"Source SQLite file not found: {path}")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema = OFF")
            return tuple(
                db.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()
                for table, columns in (
                    ("teams", "id, name, description, leader_id"),
                    ("interns", "id, name, dit_id, email, phone, college, role, joining_date"),
                    ("team_members", "team_id, intern_id"),
                    ("tasks", "id, task_code, title, description, intern_id, assigned_date, due_date, submission_date, status, assessment_status, assessment_score, remarks"),
                    ("assessments", "task_id, status, score, remarks, assessed_at"),
                )
            )
    except sqlite3.Error as exc:
        raise CommandError("Could not read the supplied InternTrack SQLite schema.") from exc


class Command(BaseCommand):
    help = "Load the InternTrack synthetic SQLite package into a fresh demo database."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=Path, default=settings.BASE_DIR / "fixtures" / "InternTrack_Synthetic_500.sqlite")

    @transaction.atomic
    def handle(self, *args, **options):
        if connection.vendor != "sqlite" or not (settings.DEBUG or os.environ.get("DJANGO_SQLITE_DEMO") == "1"):
            raise CommandError("The synthetic package is restricted to SQLite demo environments.")
        if m.Cohort.objects.filter(code=COHORT_CODE).exists():
            self.stdout.write("InternTrack package is already loaded.")
            return
        users = get_user_model().objects
        if m.Cohort.objects.exists() or users.exists():
            raise CommandError("Use an empty database to import the InternTrack package.")
        password = os.environ.get("DJANGO_DEMO_PASSWORD", "")
        if len(password) < 8:
            raise CommandError("Set DJANGO_DEMO_PASSWORD before loading the demo accounts.")
        teams, interns, members, tasks, assessments = read_package(options["source"])
        if not teams or not interns or not tasks:
            raise CommandError("Source must contain teams, interns, and assignments.")
        try:
            self.load(users, password, teams, interns, members, tasks, assessments)
        except (ValueError, KeyError, StopIteration, ArithmeticError) as exc:
            raise CommandError(f"Source records are inconsistent: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(interns)} interns, {len(teams)} teams, {len(tasks)} tasks and {len(assessments)} assessments."
        ))

    def load(self, users, password, teams, interns, members, tasks, assessments):
        coordinator = users.create_user("DITcoordinator", password=password, first_name="Internship", last_name="Coordinator")
        coordinator.groups.add(Group.objects.get_or_create(name="Coordinators")[0])
        cohort = m.Cohort.objects.create(code=COHORT_CODE, name="InternTrack QA Cohort")
        lead_ids = {team["id"]: team["leader_id"] for team in teams}
        first_team = teams[0]["id"]
        demo_lead = lead_ids[first_team]
        demo_intern = next(member["intern_id"] for member in members
                           if member["team_id"] == first_team and member["intern_id"] != demo_lead)
        login_names = {demo_lead: "DITteamlead", demo_intern: "DITintern"}
        team_records = {}
        for team in teams:
            code = f"QA-{team['id']}"
            domain = m.Domain.objects.create(name=team["name"], code=code)
            domain.cohorts.add(cohort)
            team_records[team["id"]] = m.Team.objects.create(
                cohort=cohort, domain=domain, name=team["name"], code=code, description=team["description"] or "",
            )
        intern_records = {}
        for intern in interns:
            dit_id = intern["dit_id"]
            username = login_names.get(intern["id"], f"intern-{dit_id.lower()}")
            first, _, last = intern["name"].partition(" ")
            user = users.create_user(username, email=intern["email"], first_name=first, last_name=last,
                                     password=password if intern["id"] in login_names else None)
            intern_records[intern["id"]] = m.Intern.objects.create(
                user=user, reference=dit_id, dit_id=dit_id, phone=intern["phone"] or "",
                college=intern["college"] or "", placement_role=intern["role"] or "",
                joined_on=date.fromisoformat(intern["joining_date"]),
            )
        enrollments = {}
        for member in members:
            intern_id, team_id = member["intern_id"], member["team_id"]
            if intern_id in enrollments:
                raise ValueError("An intern belongs to more than one source team.")
            team = team_records[team_id]
            enrollments[intern_id] = m.Enrollment.objects.create(
                cohort=cohort, intern=intern_records[intern_id], domain=team.domain, team=team,
                is_lead=lead_ids[team_id] == intern_id,
            )
        if enrollments.keys() != intern_records.keys() or any(
            enrollments[lead].team_id != team_records[team].pk for team, lead in lead_ids.items()
        ):
            raise ValueError("Source team membership or lead references are incomplete.")

        rubric = m.Rubric.objects.create(name="InternTrack imported scoring", version=1, published=True)
        criterion = rubric.criteria.create(code="TOTAL", name="Imported overall score",
                                           weight=100, minimum=0, maximum=100, step=Decimal("0.01"))
        by_task = {assessment["task_id"]: assessment for assessment in assessments}
        if len(by_task) != len(assessments):
            raise ValueError("Multiple assessments exist for one source task.")
        for task in tasks:
            self.load_assignment(task, by_task.pop(task["id"], None), enrollments[task["intern_id"]],
                                 rubric, criterion, coordinator)
        if by_task:
            raise ValueError("An assessment refers to a missing assignment.")

    def load_assignment(self, task, assessment, enrollment, rubric, criterion, coordinator):
        expected, started = STATUSES[task["status"]]
        required_review = expected not in (None, "Pending")
        if (
            bool(task["submission_date"]) != bool(expected)
            or task["assessment_status"] != (expected or "Pending")
            or bool(assessment) != required_review
            or (assessment and (assessment["status"] != expected or assessment["score"] != task["assessment_score"]))
        ):
            raise ValueError(f"Task {task['task_code']} has inconsistent status or assessment fields.")
        task_code = task["task_code"]
        deadline = date.fromisoformat(task["due_date"])
        assignment = m.Assignment.objects.create(
            reference=task_code, task_code=task_code, title=task["title"], instructions=task["description"] or "",
            enrollment=enrollment, team=enrollment.team, domain=enrollment.domain, rubric=rubric,
            assigned_date=date.fromisoformat(task["assigned_date"]), due_date=deadline, started=started,
        )
        if not expected:
            return
        submitted_at = timezone.make_aware(datetime.fromisoformat(task["submission_date"]))
        submission = m.Submission.objects.create(
            assignment=assignment, version=1, recorded_by=coordinator, submitted_at=submitted_at,
            deadline=deadline, note=task["remarks"] or "",
        )
        if not assessment:
            return
        decision = DECISIONS.get(expected, "")
        score = Decimal(str(assessment["score"])) if assessment["score"] is not None else None
        if bool(decision) != (score is not None) or (score is not None and not 0 <= score <= 100):
            raise ValueError(f"Task {task_code} has an invalid final score.")
        reviewed_at = timezone.make_aware(datetime.fromisoformat(assessment["assessed_at"]))
        remarks = assessment["remarks"] or ""
        evaluation = m.Evaluation.objects.create(
            submission=submission, coordinator=coordinator, decision=decision, total=score,
            reviewed_at=reviewed_at, published_at=reviewed_at if decision else None,
            imported_notes=remarks,
        )
        if score is not None:
            m.Score.objects.create(evaluation=evaluation, criterion=criterion, value=score, comment=remarks, feedback=remarks)
