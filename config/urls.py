from django.contrib import admin
from django.contrib.auth import views as auth
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.urls import include, path
from django.views.decorators.http import require_safe


@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("Unavailable", status=503, content_type="text/plain")
    return HttpResponse("OK", content_type="text/plain")


urlpatterns = [
    path("healthz/", health, name="health"),
    path("admin/", admin.site.urls),
    path("login/", auth.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth.LogoutView.as_view(), name="logout"),
    path("", include("tracking.urls")),
]
