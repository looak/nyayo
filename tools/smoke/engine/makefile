# Builds the smoke test engine the way the worker builds every engine: `make EXE=<name>`.
# The output is engine.py behind a shebang for PYTHON, which must have python-chess.

EXE    ?= smoke
PYTHON ?= python3

# `make TUNE=1` is the build that exposes the tunable option Skill, as Elephant's does for its search constants
TUNE   ?= 0

all:
	printf '#!%s\n' "$$(command -v $(PYTHON))" > $(EXE)
	printf 'TUNE_BUILD = %s\n' "$(if $(filter 1,$(TUNE)),True,False)" >> $(EXE)
	cat engine.py >> $(EXE)
	chmod +x $(EXE)
