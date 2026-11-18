from django.shortcuts import redirect
from django.urls import reverse


class RequireInternPasswordChange:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.path_info not in (reverse("account_settings"), reverse("logout")):
            intern = getattr(request.user, "intern", None)
            if intern and intern.must_change_password:
                return redirect("account_settings")
        return self.get_response(request)
