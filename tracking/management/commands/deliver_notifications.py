from django.core.management.base import BaseCommand

from tracking.notifications import deliver_pending, delivery_enabled


class Command(BaseCommand):
    help = "Deliver queued internship emails that are ready to send."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not delivery_enabled():
            self.stdout.write("Email delivery is not configured.")
            return
        sent = deliver_pending(max(0, options["limit"]))
        self.stdout.write(f"Sent {sent} email notification(s).")
