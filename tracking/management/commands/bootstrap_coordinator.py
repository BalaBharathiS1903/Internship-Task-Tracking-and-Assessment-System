import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create the initial coordinator and any explicitly configured superuser."

    @transaction.atomic
    def handle(self, *args, **options):
        hosted_demo = os.environ.get("DJANGO_SQLITE_DEMO") == "1"
        if hosted_demo:
            password = os.environ.get("DJANGO_COORDINATOR_PASSWORD") or os.environ.get("DJANGO_DEMO_PASSWORD")
            if not password:
                raise CommandError("Set DJANGO_COORDINATOR_PASSWORD to provision the hosted coordinator.")
            users = get_user_model().objects
            if not users.filter(username="DITcoordinator").exists():
                coordinator = users.create_user("DITcoordinator", password=password,
                                                first_name="Internship", last_name="Coordinator")
                coordinator.groups.add(Group.objects.get_or_create(name="Coordinators")[0])
                self.stdout.write("Initial coordinator account created.")
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
        if not hosted_demo and len(password) < 20:
            raise CommandError("Configure a generated coordinator password of at least 20 characters.")
        try:
            validate_password(password, get_user_model()(username=username))
        except ValidationError as exc:
            raise CommandError("The configured superuser password does not meet the password requirements.") from exc
        users.create_superuser(username=username, password=password)
        self.stdout.write("Configured superuser created.")
