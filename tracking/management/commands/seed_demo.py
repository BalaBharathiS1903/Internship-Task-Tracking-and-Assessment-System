import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from tracking import models as m, services as s

COORDINATOR = "DITcoordinator"
TEAM_LEAD = "DITteamlead"
INTERN = "DITintern"


def set_demo_login(user, username, password):
    if user.username != username:
        if get_user_model().objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
            raise CommandError(f"The demo username {username} is already in use.")
        user.username = username
    user.set_password(password)
    user.save(update_fields=["username", "password"])


class Command(BaseCommand):
    help = "Create synthetic sample records and configure demo account logins."

    @transaction.atomic
    def handle(self, *args, **options):
        hosted_demo = os.environ.get("DJANGO_SQLITE_DEMO") == "1"
        if connection.vendor != "sqlite" or not (settings.DEBUG or hosted_demo):
            raise CommandError("Demo seed is restricted to SQLite demo environments.")
        password = os.environ.get("DJANGO_DEMO_PASSWORD", "")
        if len(password) < 8:
            raise CommandError("Set DJANGO_DEMO_PASSWORD before seeding the demo.")
        User = get_user_model()
        if m.Cohort.objects.filter(code="DEMO-26").exists():
            coordinator = User.objects.filter(username=COORDINATOR, groups__name="Coordinators").first()
            if not coordinator:
                coordinator = User.objects.filter(username="demo-coordinator", groups__name="Coordinators").first()
            lead = User.objects.filter(intern__reference="DEMO-MEERA", intern__enrollments__cohort__code="DEMO-26").first()
            intern = User.objects.filter(intern__reference="DEMO-ARJUN", intern__enrollments__cohort__code="DEMO-26").first()
            if not all((coordinator, lead, intern)):
                raise CommandError("Could not identify all three existing demo accounts.")
            for user, username in ((coordinator, COORDINATOR), (lead, TEAM_LEAD), (intern, INTERN)):
                if user.username != username or not user.check_password(password):
                    set_demo_login(user, username, password)
            self.stdout.write("Existing demo records retained and demo logins configured.")
            return
        if User.objects.filter(username__in=[COORDINATOR, TEAM_LEAD, INTERN]).exists():
            raise CommandError("A demo username already exists.")
        coordinator = User.objects.create_user(COORDINATOR, password=password, first_name="Ananya", last_name="Rao")
        group, _ = Group.objects.get_or_create(name="Coordinators")
        coordinator.groups.add(group)
        cohort = s.save_checked(m.Cohort(code="DEMO-26", name="Demo Internship · 2026", id_prefix="DIT-DEMO-"))
        teams = []
        for code, name, team_name in (
            ("WEB", "Web Development", "Web Studio"),
            ("DATA", "Data Analytics", "Data Lab"),
            ("UX", "UI/UX Design", "Design Lab"),
        ):
            domain = s.save_checked(m.Domain(code=f"DEMO-{code}", name=name))
            domain.cohorts.add(cohort)
            teams.append(s.save_checked(m.Team(code=code, cohort=cohort, domain=domain, name=team_name)))
        people = [
            ("MEERA", "Meera Iyer", 0), ("ARJUN", "Arjun Kumar", 0), ("RIYA", "Riya Thomas", 0),
            ("NAVEEN", "Naveen S", 1), ("PRIYA", "Priya Nair", 1),
            ("ISHA", "Isha Menon", 2), ("VIKRAM", "Vikram Das", 2),
        ]
        today = timezone.localdate()
        members = []
        for index, (code, name, team_index) in enumerate(people):
            team = teams[team_index]
            enrollment = s.register_intern(
                coordinator, reference=f"DEMO-{code}", name=name, cohort=cohort, team=team,
                domain=team.domain, email=f"{code.lower()}@example.invalid", notify=False,
            )
            user = enrollment.intern.user
            if index == 0:
                set_demo_login(user, TEAM_LEAD, password)
            elif index == 1:
                set_demo_login(user, INTERN, password)
            if index == 0:
                s.update_enrollment(coordinator, enrollment, name=name, email=user.email,
                                    team=team, domain=team.domain, is_lead=True, reason="Synthetic roster")
            members.append(enrollment)
            titles = (
                ("Responsive landing page", "Accessibility checklist"),
                ("Clean source data", "Publish summary dashboard"),
                ("Design a wireframe", "Prepare handoff notes"),
            )[team_index]
            for offset, title in enumerate(titles):
                assignment = s.assign_task(
                    coordinator, enrollment=enrollment, reference=f"DEMO-{code}-{offset + 1}",
                    task_code=f"{team.code}-{offset + 1}", title=title,
                    assigned_date=today - timedelta(days=8),
                    due_date=today + timedelta(days=(-2, 4, 7, -1, 2, 8, 3)[index] + offset),
                    instructions="Describe the work you completed and attach a document for review.", notify=False,
                )
                if offset == 0 and index in (0, 2, 3, 5, 6):
                    artifact = SimpleUploadedFile("demo-work.txt", b"Synthetic project evidence for the demo.")
                    submission = s.submit(user, assignment.pk, artifact=artifact, note="Synthetic project evidence")
                    if index in (0, 3, 5):
                        s.review_submission(coordinator, submission, decision=m.SubmissionReview.Decision.COMPLETED,
                                            feedback="Verified the work details and document.", notify=False)
        for team in teams:
            session = s.save_checked(m.AttendanceSession(team=team, reference=f"DEMO-{team.code}-01", date=today))
            for member in members:
                if member.team_id == team.pk:
                    s.mark_attendance(coordinator, session, member, m.Attendance.Status.PRESENT)
            session.closed = True
            session.save(update_fields=["closed"])
        s.send_team_message(members[0].intern.user, members[1].pk, "Please check the deadline for the responsive landing page.")
        self.stdout.write(self.style.SUCCESS("Synthetic cohort and demo accounts created."))
