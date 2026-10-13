from django.http import HttpResponse
from django.shortcuts import render


def index(request):
    return render(request, "nyayo/index.html")


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
