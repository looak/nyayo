from django.urls import path

from . import client_views, worker_api as api

# The worker's endpoints, at the paths OpenBench's Client has always used
urlpatterns = [
    path("clientHello/", api.client_hello),
    path("client.zip", api.client_download, name="client_zip"),
    path("setup-windows-worker.ps1", api.setup_windows_worker, name="setup_windows_worker"),
    path("setup-linux-worker.sh", api.setup_linux_worker, name="setup_linux_worker"),
    path("worker", client_views.worker_page, name="worker_setup"),
    path("clientVersionRef/", api.client_version_ref),
    path("clientMatchRunnerVersionRef/", api.client_match_runner_version_ref),
    path("clientFastchess/<str:version>/<str:name>", api.client_fastchess),
    path("clientGetBuildInfo/", api.client_get_build_info),
    path("clientWorkerInfo/", api.client_worker_info),
    path("clientGetWorkload/", api.client_get_workload),
    path("clientGetNetwork/<str:engine>/<str:name>/", api.client_get_network),
    path("clientBenchError/", api.client_bench_error),
    path("clientReportStep/", api.client_report_step),
    path("clientSubmitNPS/", api.client_submit_nps),
    path("clientSubmitError/", api.client_submit_error),
    path("clientSubmitResults/", api.client_submit_results),
    path("clientSubmitNPSStats/", api.client_submit_nps_stats),
    path("clientHeartbeat/", api.client_heartbeat),
    path("clientSubmitPGN/", api.client_submit_pgn),
]
