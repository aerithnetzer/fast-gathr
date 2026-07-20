from __future__ import annotations

from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path


def healthz(_request):
    """Liveness probe for the ALB target group. Deliberately does not touch
    the database so a healthy container is not marked unhealthy during a
    transient DB hiccup."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("tagger.urls")),
]
