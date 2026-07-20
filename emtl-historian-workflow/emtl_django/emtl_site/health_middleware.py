"""Production-only middleware.

``HealthCheckMiddleware`` answers ``GET /healthz`` with a plain 200 before
Django's Host-header validation runs. The ALB health checker connects to the
target's private IP and sends that IP as the Host header, which is not in
``ALLOWED_HOSTS``; without this short-circuit Django would return 400
(DisallowedHost) and the target would be marked unhealthy. Real traffic still
goes through normal Host validation.
"""

from __future__ import annotations

from django.http import HttpResponse


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
