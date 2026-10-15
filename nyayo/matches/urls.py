from django.urls import path

from . import views

urlpatterns = [
    path("api/matches/<str:match_id>", views.api_match, name="api_match"),
    path("api/matches/<str:match_id>/games/<str:game>", views.api_game, name="api_game"),
]
