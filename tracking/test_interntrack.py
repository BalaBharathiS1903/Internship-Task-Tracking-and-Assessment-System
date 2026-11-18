from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from . import models as m, reporting as r


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InternTrackImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Sample-import-test-only"
        with override_settings(DEBUG=True), patch.dict("os.environ", {"DJANGO_DEMO_PASSWORD": cls.password}):
            call_command("load_interntrack", stdout=StringIO())

    def test_package_records_and_statuses_reconcile(self):
        self.assertEqual(m.Intern.objects.count(), 500)
        self.assertEqual(m.Intern.objects.exclude(college="").count(), 500)
        self.assertEqual(m.Team.objects.count(), 10)
        self.assertEqual(m.Enrollment.objects.filter(is_lead=True).count(), 10)
        self.assertEqual(m.Assignment.objects.count(), 500)
        self.assertEqual(m.Submission.objects.count(), 300)
        self.assertEqual(m.Evaluation.objects.count(), 200)
        self.assertEqual(m.Evaluation.objects.filter(published_at__isnull=True).exclude(imported_notes="").count(), 50)
        items = r.rows(m.Assignment.objects.all(), today=date(2026, 11, 17))
        counts = r.totals(items)
        self.assertEqual((counts["complete"], counts["overdue"], counts["in_review"]), (100, 100, 150))
        with override_settings(DEBUG=True):
            call_command("load_interntrack", stdout=StringIO())
        self.assertEqual(m.Assignment.objects.count(), 500)

    def test_demo_access_and_roster_pagination(self):
        self.assertTrue(self.client.login(username="DITteamlead", password=self.password))
        self.assertEqual(self.client.get(reverse("team")).status_code, 200)
        self.assertEqual(self.client.get(reverse("roster")).status_code, 403)
        self.client.force_login(get_user_model().objects.get(username="DITcoordinator"))
        page = self.client.get(reverse("roster"), {"page": 25})
        self.assertContains(page, "Showing 481–500 of 500")
