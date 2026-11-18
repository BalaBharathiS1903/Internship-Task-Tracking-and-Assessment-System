from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from . import models as m, reporting as r, services as s


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class Workflows(TestCase):
    @classmethod
    def setUpTestData(cls):
        with override_settings(DEBUG=True):
            call_command("seed_demo", stdout=StringIO())
        users = get_user_model().objects
        cls.coordinator = users.get(username="demo-coordinator")
        cls.lead = users.get(username="intern-demo-meera")
        cls.intern = users.get(username="intern-demo-arjun")
        cls.member = cls.intern.intern.enrollments.get()
        cls.outside = m.Enrollment.objects.get(intern__reference="DEMO-NAVEEN")
        cls.assignment = m.Assignment.objects.get(reference="DEMO-ARJUN-1")
        cls.rubric = cls.assignment.rubric
        cls.criteria = list(cls.rubric.criteria.order_by("-weight"))

    def download(self, route, *args, query=None):
        response = self.client.get(reverse(route, args=args), query or {})
        self.assertEqual(response.status_code, 200)
        return load_workbook(BytesIO(b"".join(response.streaming_content)))

    def upload_batch(self, kind, workbook, files=()):
        output = BytesIO()
        workbook.save(output)
        response = self.client.post(reverse("new_batch", args=[kind]), {
            "workbook": SimpleUploadedFile(f"{kind}.xlsx", output.getvalue()),
            "artifacts": files,
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
