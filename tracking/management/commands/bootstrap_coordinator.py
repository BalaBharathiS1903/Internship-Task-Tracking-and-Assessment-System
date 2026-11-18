import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Initialize the hosted demo or create the first production coordinator."

    @transaction.atomic
    def handle(self, *args, **options):
        if os.environ.get("DJANGO_SQLITE_DEMO") == "1" and os.environ.get("DJANGO_DEMO_PASSWORD"):
            call_command("load_interntrack", stdout=self.stdout)
            return
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        if not username:
            return
        users = get_user_model().objects
        existing = users.filter(username=username).first()
        if existing:
            if not existing.is_superuser:
                raise CommandError("The configured coordinator username belongs to a non-superuser.")
            return
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if len(password) < 20:
            raise CommandError("Configure a generated coordinator password of at least 20 characters.")
        users.create_superuser(username=username, password=password)
        self.stdout.write("Initial coordinator account created.")
