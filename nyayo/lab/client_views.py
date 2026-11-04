"""The page that says how to get a worker going: the commands, with this server's address in them. Open to read."""

from django.shortcuts import render
from django.urls import reverse

from . import clientzip


def server_address(request):
    # The address a worker is to be given: where this page was asked for, without the path of the zip
    return request.build_absolute_uri(reverse('client_zip'))[:-len('/client.zip')]


def worker_page(request):
    return render(request, 'lab/worker_setup.html', {'here': 'machines', 'server': server_address(request),
                                                     'client_version': clientzip.version()})
