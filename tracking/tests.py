from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from . import models as m, reporting as r, services as s
from .notifications import deliver_pending, temporary_password


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class Workflows(TestCase):
    @classmethod
    def setUpTestData(cls):
        with override_settings(DEBUG=True), patch.dict("os.environ", {"DJANGO_DEMO_PASSWORD": "Demo-test-only-2026"}):
            call_command("seed_demo", stdout=StringIO())
        users = get_user_model().objects
        cls.coordinator = users.get(username="DITcoordinator")
        cls.lead = users.get(username="DITteamlead")
        cls.intern = users.get(username="DITintern")
        cls.member = cls.intern.intern.enrollments.get()
        cls.outside = m.Enrollment.objects.get(intern__reference="DEMO-NAVEEN")
        cls.assignment = m.Assignment.objects.get(reference="DEMO-ARJUN-1")
        cls.rubric = cls.assignment.rubric
        cls.criteria = list(cls.rubric.criteria.order_by("-weight"))

    def test_demo_logins_share_password_and_keep_role_permissions(self):
        for user in (self.coordinator, self.lead, self.intern):
            self.assertTrue(self.client.login(username=user.username, password="Demo-test-only-2026"))
            self.client.logout()
        self.assertFalse(self.coordinator.is_superuser)

    def test_existing_local_demo_accounts_are_updated_without_resetting_records(self):
        legacy = {self.coordinator.pk: "demo-coordinator", self.lead.pk: "intern-demo-meera",
                  self.intern.pk: "intern-demo-arjun"}
        for pk, username in legacy.items():
            get_user_model().objects.filter(pk=pk).update(username=username)
        with override_settings(DEBUG=True), patch.dict("os.environ", {"DJANGO_DEMO_PASSWORD": "Updated-test-password"}):
            call_command("seed_demo", stdout=StringIO())
        for user in (self.coordinator, self.lead, self.intern):
            updated = get_user_model().objects.get(pk=user.pk)
            self.assertEqual(updated.username, user.username)
            self.assertTrue(updated.check_password("Updated-test-password"))
        self.assertTrue(m.Assignment.objects.filter(pk=self.assignment.pk).exists())

    def download(self, route, *args, query=None):
        response = self.client.get(reverse(route, args=args), query or {})
        self.assertEqual(response.status_code, 200)
        return load_workbook(BytesIO(b"".join(response.streaming_content)))

    def upload_batch(self, kind, workbook, files=(), *, include_setup=False):
        output = BytesIO()
        workbook.save(output)
        response = self.client.post(reverse("new_batch", args=[kind]), {
            "workbook": SimpleUploadedFile(f"{kind}.xlsx", output.getvalue()),
            "artifacts": files,
            "include_setup": "1" if include_setup else "",
        })
        self.assertEqual(response.status_code, 302, response.content[:600])
        batch_url = response["Location"]
        return batch_url, self.client.get(batch_url)

    def test_coordinator_pages_render(self):
        self.client.force_login(self.coordinator)
        sample = m.Submission.objects.filter(evaluation__isnull=True).first()
        session = m.AttendanceSession.objects.first()
        for route, args in {
            "dashboard": (), "registry": (), "roster": (), "new_intern": (), "edit_intern": (self.member.pk,),
            "tasks": (), "new_task": (), "task": (self.assignment.pk,), "edit_task": (self.assignment.pk,),
            "assessments": (), "evaluate": (sample.pk,), "rubric": (self.rubric.pk,),
            "attendance": (), "mark_session": (session.pk,), "imports": (), "reports": (), "guide": (),
        }.items():
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)

    def test_directory_filters_and_reports(self):
        self.client.force_login(self.coordinator)
        m.Intern.objects.filter(pk=self.member.intern_id).update(college="Campus One")
        roster = self.client.get(reverse("roster"), {
            "q": self.intern.get_full_name(), "college": "Campus One", "team": self.member.team_id,
        })
        self.assertEqual(len(roster.context["enrollments"]), 1)
        self.assertContains(roster, self.member.intern.dit_id)
        page = self.client.get(reverse("tasks"), {
            "college": "Campus One", "task_status": "Not started", "assessment_status": "Pending",
            "date_on": "assigned", "date_from": self.assignment.assigned_date.isoformat(), "sort": "oldest",
        })
        self.assertIn(self.assignment.pk, [item["assignment"].pk for item in page.context["items"]])
        reports = self.client.get(reverse("reports"))
        self.assertContains(reports, "Team Allocation")
        self.assertContains(reports, "Team Lead Report")
        for kind in ("roster", "allocation", "tasks", "completed", "follow-up", "overdue", "assessment", "team-lead"):
            workbook = self.download("export", kind)
            self.assertIn("Report", workbook)
        self.assertContains(self.client.get(reverse("team_report", args=[self.member.team_id])), "Print report")

    def test_team_management_permissions_and_protected_deletion(self):
        self.client.force_login(self.lead)
        self.assertEqual(self.client.get(reverse("teams")).status_code, 403)
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(reverse("teams")), "Team allocation (Excel)")
        self.assertRedirects(self.client.post(reverse("delete_team", args=[self.member.team_id])),
                             reverse("team_detail", args=[self.member.team_id]))
        self.assertTrue(m.Team.objects.filter(pk=self.member.team_id).exists())

    @override_settings(DIT_EMAIL_TRANSPORT="django", DIT_EMAIL_FROM="internships@example.invalid",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DIT_MAIL_BACKGROUND=False)
    def test_registration_sends_generated_credentials_and_requires_password_change(self):
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse("new_intern"), {
            "reference": "EMAIL-01", "name": "New Intern", "email": "new@example.invalid",
            "dit_id": "DIT-EMAIL-01", "cohort": self.member.cohort_id, "domain": self.member.domain_id,
            "team": self.member.team_id, "mode": "manual",
        })
        self.assertEqual(response.status_code, 302)
        notice = m.EmailNotification.objects.get(kind="welcome")
        password = temporary_password(notice)
        self.assertNotEqual(notice.user.password, password)
        self.assertEqual(deliver_pending(), 1)
        self.assertEqual(deliver_pending(), 0)
        self.assertEqual(mail.outbox[0].to, ["new@example.invalid"])
        self.assertIn(notice.user.username, mail.outbox[0].body)
        self.assertIn(password, mail.outbox[0].body)
        self.assertTrue(self.client.login(username=notice.user.username, password=password))
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("account_settings"))
        changed = self.client.post(reverse("account_settings"), {
            "action": "password", "old_password": password, "new_password1": "Stronger-local-test-pass!",
            "new_password2": "Stronger-local-test-pass!",
        })
        self.assertRedirects(changed, reverse("account_settings"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertFalse(m.Intern.objects.get(user=notice.user).must_change_password)

    @override_settings(DIT_EMAIL_TRANSPORT="django", DIT_EMAIL_FROM="internships@example.invalid",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DIT_MAIL_BACKGROUND=False)
    def test_import_confirmation_sends_welcome_and_task_email_to_the_assigned_intern(self):
        self.client.force_login(self.coordinator)
        roster = self.download("template", "roster")
        roster["Data"].append(("MAIL-02", "Import Intern", "import@example.invalid",
                               self.member.cohort.code, "DIT-EMAIL-02", "manual",
                               self.member.domain.code, "", self.member.team.code))
        batch_url, _ = self.upload_batch("roster", roster)
        self.assertFalse(m.EmailNotification.objects.exists())
        self.assertEqual(self.client.post(batch_url).status_code, 302)
        self.assertEqual(self.client.post(batch_url).status_code, 302)
        self.assertEqual(m.EmailNotification.objects.filter(kind="welcome").count(), 1)
        task_book = self.download("template", "tasks")
        today = timezone.localdate()
        task_book["Data"].append(("MAIL-TASK", "TASK-07", "Build the report", "DIT-EMAIL-02", self.member.cohort.code,
                                  self.member.team.code, str(self.rubric), today.isoformat(), today.isoformat(),
                                  "Submit the Python report and README."))
        task_batch, _ = self.upload_batch("tasks", task_book)
        self.assertFalse(m.EmailNotification.objects.filter(kind="assignment").exists())
        self.assertEqual(self.client.post(task_batch).status_code, 302)
        self.assertEqual(deliver_pending(), 2)
        message = next(item for item in mail.outbox if "TASK-07" in item.subject)
        self.assertEqual(message.to, ["import@example.invalid"])
        self.assertIn("Greetings from DefineInfoTech Pvt. Ltd.", message.body)
        self.assertIn("Submit the Python report and README.", message.body)
        self.assertIn("Due on:", message.body)

    def test_settings_protects_other_coordinators_and_logs_activity(self):
        another = get_user_model().objects.create_user("another-coordinator")
        another.groups.add(self.coordinator.groups.get(name="Coordinators"))
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(reverse("account_settings")), "Recent activity")
        self.assertEqual(self.client.post(reverse("manage_user", args=[another.pk, "status"])).status_code, 403)
        self.client.force_login(self.intern)
        self.assertNotContains(self.client.get(reverse("account_settings")), "User management")
        self.assertEqual(self.client.post(reverse("account_settings"), {"action": "add_coordinator"}).status_code, 403)

    def test_intern_pages_and_scoping(self):
        self.client.force_login(self.intern)
        self.assertContains(self.client.get(reverse("dashboard")), "My internship")
        for route in ("profile", "tasks", "attendance", "assessments", "imports", "inbox", "guide"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 200)
        self.assertEqual(self.client.get(reverse("team")).status_code, 403)
        self.assertEqual(self.client.get(reverse("task", args=[m.Assignment.objects.filter(enrollment=self.outside).first().pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("new_task"), {}).status_code, 403)

    def test_lead_is_intern_with_read_only_team_access_and_individual_messages(self):
        self.client.force_login(self.lead)
        page = self.client.get(reverse("team"))
        self.assertContains(page, "Arjun Kumar")
        self.assertNotContains(page, "Naveen")
        self.assertEqual(self.client.get(reverse("task", args=[self.assignment.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("edit_task", args=[self.assignment.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("new_batch", args=["tasks"])).status_code, 403)
        self.assertEqual(self.client.post(reverse("message_member", args=[self.outside.pk]), {"body": "Cross team"}).status_code, 404)
        sent = self.client.post(reverse("message_member", args=[self.member.pk]), {"body": "Please check today’s task."})
        self.assertEqual(sent.status_code, 302)
        record = m.TeamMessage.objects.get(body="Please check today’s task.")
        self.client.force_login(self.intern)
        self.assertContains(self.client.get(reverse("inbox")), record.body)
        self.assertEqual(self.client.post(reverse("read_message", args=[record.pk])).status_code, 302)
        record.refresh_from_db()
        self.assertIsNotNone(record.read_at)
        m.Enrollment.objects.filter(intern__user=self.lead).update(is_lead=False)
        self.client.force_login(self.lead)
        self.assertEqual(self.client.get(reverse("team")).status_code, 403)

    def test_roster_preview_requires_confirmation_and_preserves_text_ids(self):
        self.client.force_login(self.coordinator)
        book = self.download("template", "roster")
        self.assertIn("Desk_domain", book.defined_names)
        book["Data"].append(("NEW-01", "Diya S", "", "DEMO-26", "00072", "auto", "", "DEMO-WEB", "WEB"))
        batch, preview = self.upload_batch("roster", book)
        self.assertContains(preview, "00072")
        self.assertFalse(m.Intern.objects.filter(dit_key="00072").exists())
        self.assertEqual(self.client.post(batch).status_code, 302)
        self.assertEqual(self.client.post(batch).status_code, 302)
        record = m.Intern.objects.get(dit_key="00072")
        self.assertEqual(record.dit_id, "00072")
        self.assertEqual(record.enrollments.get().domain.code, "DEMO-WEB")

    def test_roster_errors_block_whole_batch(self):
        self.client.force_login(self.coordinator)
        book = self.download("template", "roster")
        book["Data"].append(("NEW-02", "Diya S", "", "DEMO-26", "DIT-72", "manual", "DEMO-WEB", "", "WEB"))
        book["Data"].append(("NEW-03", "Neha S", "", "DEMO-26", "DIT-73", "auto", "", "DEMO-DATA", "WEB"))
        batch, preview = self.upload_batch("roster", book)
        self.assertContains(preview, "one consistent preference")
        self.assertEqual(self.client.post(batch).status_code, 200)
        self.assertFalse(m.Intern.objects.filter(reference__in=["NEW-02", "NEW-03"]).exists())

    def test_intern_master_setup_requires_opt_in_and_commits_with_the_interns(self):
        self.client.force_login(self.coordinator)
        book = self.download("template", "roster")
        self.assertEqual(book["Data"]["D1"].value, "internship_batch")
        book["Setup"].append(("2026", "Internship 2026", "DIT-26-", "WEB", "Web", "WEB-A", "Web A"))
        book["Data"].append(("NEW-SETUP", "New Intern", "", "2026", "", "manual", "WEB", "", "WEB-A"))

        _, preview = self.upload_batch("roster", book)
        self.assertContains(preview, "Internship batch code 2026 is not configured.")
        batch, preview = self.upload_batch("roster", book, include_setup=True)
        self.assertContains(preview, "local DIT ID prefix DIT-26-")
        self.assertContains(preview, "DIT-26-0001")
        self.assertFalse(m.Cohort.objects.filter(code="2026").exists())
        self.assertFalse(m.Intern.objects.filter(reference="NEW-SETUP").exists())

        self.assertEqual(self.client.post(batch).status_code, 302)
        enrollment = m.Enrollment.objects.get(intern__reference="NEW-SETUP")
        self.assertEqual(enrollment.cohort.code, "2026")
        self.assertEqual(enrollment.team.code, "WEB-A")
        self.assertEqual(enrollment.intern.dit_id, "DIT-26-0001")
        self.assertEqual(self.client.post(batch).status_code, 302)

    def test_coordinator_can_assign_manually_and_through_task_register(self):
        self.client.force_login(self.coordinator)
        today = timezone.localdate()
        result = self.client.post(reverse("new_task"), {
            "reference": "NEW-TASK-1", "task_code": "WEB-02", "title": "New manual task",
            "enrollment": self.member.pk, "rubric": self.rubric.pk,
            "assigned_date": today, "due_date": today + timedelta(days=3),
        })
        self.assertEqual(result.status_code, 302)
        self.assertEqual(m.Assignment.objects.get(reference="NEW-TASK-1").domain, self.member.domain)
        book = self.download("template", "tasks")
        book["Data"].append((
            "NEW-TASK-2", "WEB-03", "New imported task", self.member.intern.dit_id, self.member.cohort.code,
            self.member.team.code, str(self.rubric), today.isoformat(), (today + timedelta(days=3)).isoformat(), "",
        ))
        batch, preview = self.upload_batch("tasks", book)
        self.assertContains(preview, "Confirm and commit")
        self.assertFalse(m.Assignment.objects.filter(reference="NEW-TASK-2").exists())
        self.assertEqual(self.client.post(batch).status_code, 302)
        self.assertEqual(m.Assignment.objects.get(reference="NEW-TASK-2").enrollment, self.member)

    def test_offline_submission_keeps_verified_date_and_coordinator_audit(self):
        self.client.force_login(self.coordinator)
        day = timezone.localdate() - timedelta(days=1)
        response = self.client.post(reverse("record_offline", args=[self.assignment.pk]), {
            "submitted_date": day, "reason": "Verified paper handoff",
            "link": "https://example.invalid/verified",
        })
        self.assertEqual(response.status_code, 302)
        submission = self.assignment.submissions.get()
        self.assertEqual(timezone.localdate(submission.submitted_at), day)
        self.assertEqual(submission.recorded_by, self.coordinator)
        self.assertTrue(m.AuditEvent.objects.filter(verb="submission.offline", detail="Verified paper handoff").exists())

    def test_submission_manifest_binds_account_and_authorizes_file_download(self):
        self.client.force_login(self.intern)
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            book = self.download("template", "submissions")
            row = next(cells for cells in book["Data"].iter_rows(min_row=2)
                       if cells[0].value == self.assignment.reference)
            row[1].value = "evidence.txt"
            blob = SimpleUploadedFile("evidence.txt", b"synthetic project evidence", content_type="text/plain")
            batch, preview = self.upload_batch("submissions", book, [blob])
            self.assertContains(preview, "1 rows passed validation")
            self.assertEqual(self.client.post(batch).status_code, 302)
            self.assertEqual(self.client.post(batch).status_code, 302)
            submission = self.assignment.submissions.get()
            self.assertEqual(submission.recorded_by, self.intern)
            url = reverse("artifact", args=[submission.pk])
            downloaded = self.client.get(url)
            self.assertEqual(b"".join(downloaded.streaming_content), b"synthetic project evidence")
            downloaded.close()
            self.client.force_login(self.lead)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_cross_account_manifest_rejected(self):
        self.client.force_login(self.lead)
        book = self.download("template", "submissions")
        book["Data"].append((self.assignment.reference, "", "https://example.invalid/project", ""))
        batch, preview = self.upload_batch("submissions", book)
        self.assertContains(preview, "does not match a record")
        self.assertEqual(self.client.post(batch).status_code, 200)
        self.assertFalse(self.assignment.submissions.exists())

    def test_weighted_scores_remain_draft_until_published(self):
        submission = s.submit(self.intern, self.assignment.pk, link="https://example.invalid/sample")
        first, second = self.criteria
        with self.assertRaises(ValidationError):
            s.assess(self.coordinator, submission, {first.pk: (Decimal(12), "", "")})
        draft = s.assess(self.coordinator, submission, {first.pk: (Decimal(8), "", "")})
        self.assertIsNone(draft.total)
        draft = s.assess(self.coordinator, submission, {second.pk: (Decimal(3), "", "")})
        self.assertEqual(draft.total, Decimal("78.00"))
        self.client.force_login(self.intern)
        self.assertNotContains(self.client.get(reverse("task", args=[self.assignment.pk])), "78.00")
        s.assess(self.coordinator, submission, {}, decision="passed")
        self.assertContains(self.client.get(reverse("task", args=[self.assignment.pk])), "78.00")

    def test_score_import_creates_draft_and_revision_preserves_history(self):
        submission = s.submit(self.intern, self.assignment.pk, link="https://example.invalid/sample")
        self.client.force_login(self.coordinator)
        book = self.download("template", "scores")
        for criterion, score in zip(self.criteria, (8, 3)):
            book["Data"].append((self.assignment.reference, 1, criterion.code, score, "Evidence reviewed", ""))
        batch, preview = self.upload_batch("scores", book)
        self.assertContains(preview, "2 rows passed validation")
        self.assertEqual(self.client.post(batch).status_code, 302)
        evaluation = m.Evaluation.objects.get(submission=submission)
        self.assertEqual(evaluation.total, Decimal("78.00"))
        self.assertIsNone(evaluation.published_at)
        deadline = timezone.localdate() + timedelta(days=2)
        s.assess(self.coordinator, submission, {}, decision="revision", revision_due=deadline)
        resubmission = s.submit(self.intern, self.assignment.pk, link="https://example.invalid/revision")
        self.assertEqual(resubmission.version, 2)
        self.assertEqual(resubmission.deadline, deadline)
        self.assertEqual(self.assignment.submissions.count(), 2)
        self.client.force_login(self.intern)
        self.assertContains(self.client.get(reverse("task", args=[self.assignment.pk])), "78.00")

    def test_dashboard_and_excel_team_totals_reconcile(self):
        self.client.force_login(self.coordinator)
        params = {"team": self.member.team_id}
        page = self.client.get(reverse("dashboard"), params)
        counts = page.context["counts"]
        sheet = self.download("export", "tasks", query=params)["Team Totals"]
        self.assertEqual([sheet.cell(2, index).value for index in range(3, 7)],
                         [counts[key] for key in ("total", "complete", "pending", "overdue")])
        self.assertEqual(counts["total"], counts["complete"] + counts["pending"])

    def test_due_date_and_cancelled_states(self):
        today = timezone.localdate()
        self.assignment.due_date = today
        self.assignment.save(update_fields=["due_date"])
        items = m.Assignment.objects.filter(pk=self.assignment.pk)
        self.assertFalse(r.rows(items, today=today)[0]["overdue"])
        self.assertTrue(r.rows(items, today=today + timedelta(days=1))[0]["overdue"])
        self.assignment.cancelled = True
        self.assignment.save(update_fields=["cancelled"])
        self.assertEqual(r.totals(r.rows(items, today=today))["total"], 0)

    def test_session_snapshot_and_coordinator_only_marking(self):
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse("setup", args=["session"]), {
            "reference": "NEW-SESSION", "team": self.member.team_id, "date": timezone.localdate(),
        })
        self.assertEqual(response.status_code, 302)
        session = m.AttendanceSession.objects.get(reference="NEW-SESSION")
        self.assertEqual(m.Attendance.objects.filter(session=session, status="unmarked").count(), 3)
        response = self.client.post(reverse("mark_session", args=[session.pk]), {
            "enrollment": self.member.pk, "status": "present",
        })
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.lead)
        self.assertEqual(self.client.post(reverse("mark_session", args=[session.pk])).status_code, 403)
