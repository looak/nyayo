from django.urls import path

from . import views

urlpatterns = [
    path("", views.runs_list, name="runs"),
    path("runs/<uuid:run_id>", views.run_detail, name="run"),
    path("compare", views.compare_page, name="compare"),
    path("case/<str:check_id>/<path:case_id>", views.case_page, name="case"),
    path("api/runs/<uuid:run_id>", views.api_run, name="api_run"),
]
