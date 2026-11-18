from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking import models as m, reporting as r


class Command(BaseCommand):
    help = "Prepare the daily overdue follow-up list for coordinator use."

    def handle(self, *args, **options):
        day = timezone.localdate()
        items = [
            {"assignment": item["assignment"].reference, "team_id": item["assignment"].team_id,
             "deadline": item["deadline"].isoformat()}
            for item in r.rows(m.Assignment.objects.filter(cancelled=False), today=day)
            if item["overdue"]
        ]
        run, created = m.FollowUpRun.objects.update_or_create(date=day, defaults={"items": items})
        self.stdout.write(f"{run.date}: {len(items)} overdue assignments ({'created' if created else 'refreshed'}).")
