from django.contrib import admin

from .models import (Book, Engine, EngineConfig, LogEvent, Machine, Network, Result, ServerState, SPSAParameter,
                     SPSARun, Step, Test)

for model in (Test, Result, Step, Machine, Engine, EngineConfig, Book, Network, SPSARun, SPSAParameter, LogEvent,
              ServerState):
    admin.site.register(model)
