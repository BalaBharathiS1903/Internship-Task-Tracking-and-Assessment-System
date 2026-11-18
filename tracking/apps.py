from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in, user_logged_out


class TrackingConfig(AppConfig):
    name = "tracking"

    def ready(self):
        from .services import audit

        def log_sign_in(sender, user, **kwargs):
            audit(user, "account.login", f"User {user.pk}", "Signed in")

        def log_sign_out(sender, user, **kwargs):
            if user and user.is_authenticated:
                audit(user, "account.logout", f"User {user.pk}", "Signed out")

        user_logged_in.connect(log_sign_in, weak=False, dispatch_uid="tracking-sign-in")
        user_logged_out.connect(log_sign_out, weak=False, dispatch_uid="tracking-sign-out")
