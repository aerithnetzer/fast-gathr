"""Site-wide login gate (production only).

Redirects any unauthenticated request to ``LOGIN_URL`` except for a small
allowlist: the ALB health probe, the auth views themselves, password-reset
URLs, and static assets (so the login page can load its CSS). Placed after
Django's AuthenticationMiddleware so ``request.user`` is populated.
"""

from __future__ import annotations

from django.contrib.auth.views import redirect_to_login


class LoginRequiredMiddleware:
    ALLOWLIST = (
        "/healthz",
        "/accounts/login/",
        "/accounts/logout/",
        "/accounts/password_reset/",
        "/accounts/reset/",
        "/static/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated or request.path.startswith(self.ALLOWLIST):
            return self.get_response(request)
        return redirect_to_login(request.get_full_path())
