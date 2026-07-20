from __future__ import annotations

from django.urls import path

from . import live_views, views


app_name = "tagger"

urlpatterns = [
    path("", views.home, name="home"),
    path("tagger/", views.workbench, name="workbench"),
    path("tagger/live/", live_views.live_workbench, name="live_workbench"),
    path("tagger/live/export/", live_views.live_export, name="live_export"),
    path("data/", views.data_page, name="data"),
    path("corpus/", views.corpus_page, name="corpus"),
    path(
        "tagger/new-id/<int:proposal_id>/<slug:action>/",
        views.update_new_id_proposal,
        name="update_new_id_proposal",
    ),
    path(
        "tagger/entity/<int:stage_output_id>/propose/",
        views.create_entity_proposal,
        name="create_entity_proposal",
    ),
    path(
        "tagger/stage/<int:stage_output_id>/<slug:action>/",
        views.update_stage_output,
        name="update_stage_output",
    ),
    path("tagger/download/<slug:artifact>/", views.download_fixture, name="download_fixture"),
]
