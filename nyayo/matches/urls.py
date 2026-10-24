from django.urls import path

from . import dashboard, gameslist, pages, views

urlpatterns = [
    path("dashboard", dashboard.dashboard, name="dashboard"),
    path("matches", pages.matches_list, name="matches"),
    path("games", gameslist.games_list, name="games"),
    path("matches/<str:match_id>", pages.match_detail, name="match"),
    path("matches/<str:match_id>/games/<str:game>", pages.game_page, name="game"),
    path("matches/<str:match_id>/games/<str:game>/live", pages.game_fragment, name="game_fragment"),
    path("api/games", views.api_games, name="api_games"),
    path("api/matches/<str:match_id>", views.api_match, name="api_match"),
    path("api/matches/<str:match_id>/games/<str:game>", views.api_game, name="api_game"),
    path("api/matches/<str:match_id>/pgns/<str:batch>", views.api_batch, name="api_batch"),
]
