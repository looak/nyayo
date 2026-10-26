from django.urls import path

from . import dashboard, exports, gameslist, pages, views

urlpatterns = [
    path("", dashboard.dashboard, name="dashboard"),
    path("matches", pages.matches_list, name="matches"),
    path("games", gameslist.games_list, name="games"),
    path("exports", exports.exports_page, name="exports"),
    path("exports/<int:dataset_id>", exports.export_page, name="export"),
    path("exports/<int:dataset_id>/file", exports.export_file, name="export_file"),
    path("api/exports", exports.api_exports, name="api_exports"),
    path("api/exports/<int:dataset_id>", exports.api_export, name="api_export"),
    path("matches/<str:match_id>", pages.match_detail, name="match"),
    path("matches/<str:match_id>/games/<str:game>", pages.game_page, name="game"),
    path("matches/<str:match_id>/games/<str:game>/live", pages.game_fragment, name="game_fragment"),
    path("api/games", views.api_games, name="api_games"),
    path("api/matches/<str:match_id>", views.api_match, name="api_match"),
    path("api/matches/<str:match_id>/games/<str:game>", views.api_game, name="api_game"),
    path("api/matches/<str:match_id>/pgns/<str:batch>", views.api_batch, name="api_batch"),
]
