from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("nyayo.matches.urls")),
    path("", include("nyayo.runs.urls")),
]
