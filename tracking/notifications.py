import json
import logging
from datetime import timedelta
from threading import Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import close_old_connections, transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import EmailNotification as Notice

logger = logging.getLogger(__name__)
_worker_lock = Lock()
_worker_running = False
_needs_run = False


class DeliveryError(Exception):
    pass


def delivery_enabled():
    return bool(settings.DIT_EMAIL_FROM and (
        settings.DIT_EMAIL_TRANSPORT == "django" or
        (settings.DIT_EMAIL_TRANSPORT == "resend" and settings.DIT_RESEND_API_KEY)
    ))


def temporary_password(notice):
    return "Dt!" + salted_hmac("intern-email-first-password", f"{notice.id}:{notice.user_id}",
                              algorithm="sha256").hexdigest()[:24]


def queue_welcome(intern):
    user = intern.user
    if not user.email:
        return
    notice = Notice.objects.create(kind=Notice.Kind.WELCOME, user=user, recipient=user.email)
    user.set_password(temporary_password(notice))
    user.save(update_fields=["password"])
    intern.must_change_password = True
    intern.save(update_fields=["must_change_password"])
    transaction.on_commit(wake_delivery)


def queue_assignment(assignment, user):
    if user.email and not assignment.cancelled:
        Notice.objects.create(kind=Notice.Kind.ASSIGNMENT, user=user, recipient=user.email, assignment=assignment)
        transaction.on_commit(wake_delivery)


def email_content(notice):
    user = notice.user
    if not user.is_active or user.email != notice.recipient:
        return None
    context = {"name": user.get_full_name() or user.username}
    if notice.kind == Notice.Kind.WELCOME:
        intern = getattr(user, "intern", None)
        password = temporary_password(notice)
        if not intern or not intern.must_change_password or not user.check_password(password):
            return None
        subject = "Your Internship Desk login | DIT Internship"
        context.update(username=user.username, temporary_password=password, dit_id=intern.dit_id,
                       login_url=settings.DIT_PUBLIC_URL.rstrip("/") + reverse("login"))
        template = "email/intern_welcome.txt"
    else:
        assignment = notice.assignment
        if not assignment or assignment.cancelled or assignment.enrollment.intern.user_id != user.pk:
            return None
        title_parts = [assignment.task_code, assignment.title, assignment.enrollment.intern.college]
        subject = " - ".join(part for part in title_parts if part) + " | DIT Internship"
        context.update(assignment=assignment, assigned=assignment.assigned_date.strftime("%d %b %Y"),
                       due=assignment.due_date.strftime("%d %b %Y"),
                       assignment_url=settings.DIT_PUBLIC_URL.rstrip("/") + reverse("task", args=[assignment.pk]))
        template = "email/task_assigned.txt"
    return " ".join(subject.split())[:240], render_to_string(template, context).strip()


def send(notice, subject, body):
    if settings.DIT_EMAIL_TRANSPORT == "django":
        if EmailMessage(subject, body, settings.DIT_EMAIL_FROM, [notice.recipient]).send() != 1:
            raise DeliveryError("The configured mail backend did not accept the message.")
        return
    payload = json.dumps({"from": settings.DIT_EMAIL_FROM, "to": [notice.recipient],
                          "subject": subject, "text": body}).encode()
    request = Request("https://api.resend.com/emails", data=payload, headers={
        "Authorization": f"Bearer {settings.DIT_RESEND_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "InternshipDesk/1.0",
        "Idempotency-Key": str(notice.pk),
    })
    try:
        with urlopen(request, timeout=12) as response:
            if not json.load(response).get("id"):
                raise DeliveryError("The email provider did not return a message ID.")
    except HTTPError as exc:
        raise DeliveryError(f"The email provider returned HTTP {exc.code}.") from None
    except (URLError, TimeoutError, ValueError):
        raise DeliveryError("The email provider could not be reached or returned an invalid response.") from None


def pending_notices():
    return Notice.objects.filter(sent_at__isnull=True, closed_at__isnull=True)


def deliver_pending(limit=500):
    if not delivery_enabled():
        return 0
    sent = 0
    for _ in range(limit):
        now = timezone.now()
        ready = pending_notices().filter(next_attempt_at__lte=now).order_by("created_at", "pk")
        notice_id = ready.values_list("pk", flat=True).first()
        if not notice_id:
            break
        if not ready.filter(pk=notice_id).update(next_attempt_at=now + timedelta(minutes=2), attempts=F("attempts") + 1):
            continue
        notice = Notice.objects.select_related(
            "user", "assignment__enrollment__intern", "assignment__team",
        ).get(pk=notice_id)
        try:
            content = email_content(notice)
            if not content:
                Notice.objects.filter(pk=notice_id).update(closed_at=timezone.now(), last_error="The account or assignment changed.")
                continue
            send(notice, *content)
        except (DeliveryError, OSError) as exc:
            error = str(exc) if isinstance(exc, DeliveryError) else "The configured mail backend could not send the message."
            delay = min(60, 2 ** min(notice.attempts, 6))
            Notice.objects.filter(pk=notice_id).update(
                last_error=error[:180], next_attempt_at=timezone.now() + timedelta(minutes=delay),
            )
        else:
            Notice.objects.filter(pk=notice_id).update(sent_at=timezone.now(), last_error="")
            sent += 1
    return sent


def _worker():
    global _worker_running, _needs_run
    while True:
        with _worker_lock:
            if not _needs_run:
                _worker_running = False
                return
            _needs_run = False
        close_old_connections()
        try:
            deliver_pending()
        except Exception:
            logger.error("The email queue processor stopped before its current pass finished.")
        finally:
            close_old_connections()


def wake_delivery():
    global _worker_running, _needs_run
    if not delivery_enabled() or not settings.DIT_MAIL_BACKGROUND:
        return
    with _worker_lock:
        _needs_run = True
        if not _worker_running:
            _worker_running = True
            Thread(target=_worker, name="dit-email-delivery", daemon=True).start()
