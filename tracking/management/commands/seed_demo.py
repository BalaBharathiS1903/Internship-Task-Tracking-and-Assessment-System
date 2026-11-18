from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from tracking import models as m, services as s

DEMO_PASSWORD = "DeskDemo-2026!"


class Command(BaseCommand):
    help = "Create synthetic sample records and local demo accounts."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or connection.vendor != "sqlite":
            raise CommandError("Demo seed is restricted to local SQLite development.")
        if m.Cohort.objects.filter(code="DEMO-26").exists():
            self.stdout.write("Demo cohort already exists.")
            return
        User = get_user_model()
        if User.objects.filter(username__in=["demo-coordinator", "intern-demo-meera", "intern-demo-arjun"]).exists():
            raise CommandError("A demo username already exists.")
        coordinator = User.objects.create_user("demo-coordinator", password=DEMO_PASSWORD, first_name="Ananya", last_name="Rao")
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
        rubric = s.save_checked(m.Rubric(name="Demo project", version=1))
        criteria = [
            s.add_criterion(coordinator, rubric, code=code, name=name, description=description,
                            weight=Decimal(weight), minimum=Decimal(0), maximum=Decimal(scale), step=Decimal(1))
            for code, name, description, weight, scale in (
                ("QUALITY", "Implementation quality", "Check that the key requirements work.", 60, 10),
                ("DOCS", "Evidence and documentation", "Explain how to reproduce the result.", 40, 4),
            )
        ]
        s.publish_rubric(coordinator, rubric)
        rubric.refresh_from_db()
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
                domain=team.domain, email=f"{code.lower()}@example.invalid",
            )
            user = enrollment.intern.user
            if index < 2:
                user.set_password(DEMO_PASSWORD)
                user.save(update_fields=["password"])
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
                    task_code=f"{team.code}-{offset + 1}", title=title, rubric=rubric,
                    assigned_date=today - timedelta(days=8),
                    due_date=today + timedelta(days=(-2, 4, 7, -1, 2, 8, 3)[index] + offset),
                    instructions="Attach a file or share an HTTPS link. Include a short note explaining how to review your work.",
                )
                if offset == 0 and index in (0, 2, 3, 5, 6):
                    submission = s.submit(user, assignment.pk, link="https://example.invalid/demo-work", note="Synthetic project evidence")
                    if index in (0, 3, 5):
                        s.assess(coordinator, submission, {
                            criteria[0].pk: (Decimal(8), "Key requirements are covered.", ""),
                            criteria[1].pk: (Decimal(3), "Include a few more verification notes.", ""),
                        }, decision=m.Evaluation.Decision.PASSED)
        for team in teams:
            session = s.save_checked(m.AttendanceSession(team=team, reference=f"DEMO-{team.code}-01", date=today))
            for member in members:
                if member.team_id == team.pk:
                    s.mark_attendance(coordinator, session, member, m.Attendance.Status.PRESENT)
            session.closed = True
            session.save(update_fields=["closed"])
        s.send_team_message(members[0].intern.user, members[1].pk, "Please check the deadline for the responsive landing page.")
        self.stdout.write(self.style.SUCCESS("Synthetic cohort and demo accounts created."))
