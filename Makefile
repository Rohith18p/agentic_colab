VENV := .venv
PY := $(VENV)/bin/python
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: install run test demo lock clean

# Uses uv when available, otherwise python3.12 + pip.
$(VENV)/.installed: pyproject.toml requirements.lock
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --python 3.12 --allow-existing $(VENV) && \
		uv pip install --python $(PY) -r requirements.lock && \
		uv pip install --python $(PY) --no-deps -e '.[dev]'; \
	else \
		python3.12 -m venv $(VENV) && \
		$(PY) -m pip install -q -r requirements.lock && \
		$(PY) -m pip install -q --no-deps -e '.[dev]'; \
	fi
	@touch $@

install: $(VENV)/.installed

run: install
	$(PY) -m agentic_colab --host $(HOST) --port $(PORT)

test: install
	$(PY) -m pytest

demo: install
	PORT=$(PORT) $(PY) scripts/demo.py

# Regenerate the pinned lock file (all transitive deps) from pyproject.toml.
# e.g. make lock EXCLUDE_NEWER=2026-10-17 to skip releases newer than that date.
lock:
	uv pip compile pyproject.toml --extra dev --python-version 3.12 \
		$(if $(EXCLUDE_NEWER),--exclude-newer $(EXCLUDE_NEWER)) -o requirements.lock

clean:
	rm -rf $(VENV) data/*.db data/*.db-wal data/*.db-shm .pytest_cache
