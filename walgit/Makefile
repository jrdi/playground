REPO ?= demo
PORT ?= 8099
PY   := uv run

.PHONY: help
help:
	@echo "walgit -- git at any scale, as an exercise"
	@echo
	@echo "  make up          start MinIO (docker compose)"
	@echo "  make down        stop MinIO"
	@echo "  make install     create the venv and install deps"
	@echo "  make preflight   exercise 0: prove S3 gives you atomic conditional writes"
	@echo
	@echo "  make test        run everything"
	@echo "  make t1          exercise 1 -- content-addressed object store"
	@echo "  make t2          exercise 2 -- WAL + compare-and-swap index"
	@echo "  make t3          exercise 3 -- compaction and GC"
	@echo "  make t4          exercise 4 -- serving real git"
	@echo "  make todo        list every blank still unimplemented"
	@echo
	@echo "  make serve       run the git server on :$(PORT)"
	@echo "  make demo        full narrated end-to-end run"
	@echo "  make show        pretty-print the WAL index    (REPO=$(REPO))"
	@echo "  make compact     compact a repository          (REPO=$(REPO))"
	@echo "  make gc          sweep unreferenced objects    (REPO=$(REPO))"
	@echo
	@echo "  make peek EX=2   show the reference solution for one exercise"
	@echo "  make solve       overwrite your code with the reference (asks first)"
	@echo "  make reset       restore the blanks from the reference (asks first)"

# ----------------------------------------------------------------- environment

.PHONY: up down logs install
up:
	docker compose up -d
	@echo "waiting for MinIO..."
	@until docker compose exec -T minio mc ready local >/dev/null 2>&1; do sleep 0.5; done
	@echo "MinIO ready: http://127.0.0.1:9000  (console :9001, walgit/walgitwalgit)"

down:
	docker compose down

logs:
	docker compose logs -f minio

install:
	uv sync --all-groups

# ------------------------------------------------------------------- exercises

.PHONY: preflight test t1 t2 t3 t4 todo
preflight:
	$(PY) pytest tests/test_00_preflight.py -v

test:
	$(PY) pytest

t1:
	$(PY) pytest tests/test_01_objects.py -v

t2:
	$(PY) pytest tests/test_02_wal.py -v

t3:
	$(PY) pytest tests/test_03_compaction.py -v

t4:
	$(PY) pytest tests/test_04_serve.py -v

todo:
	@grep -rn "EXERCISE [0-9]" walgit/*.py \
	  | sed 's/^walgit\/\([a-z_]*\.py\):\([0-9]*\):.*EXERCISE \([0-9.]*\): \([^ ]*\).*/\3  \4  (walgit\/\1:\2)/' \
	  | sort

# ------------------------------------------------------------------ running it

.PHONY: serve demo show compact gc destroy
serve:
	WALGIT_CACHE_DIR=.walgit-cache $(PY) python -m walgit serve --port $(PORT)

demo:
	PORT=$(PORT) REPO=$(REPO) ./scripts/demo.sh

show:
	$(PY) python -m walgit show $(REPO)

compact:
	$(PY) python -m walgit compact $(REPO) --min-entries 2

gc:
	$(PY) python -m walgit gc $(REPO) --grace 0

destroy:
	$(PY) python -m walgit destroy-repo $(REPO)

# ------------------------------------------------------------------- solutions

.PHONY: peek solve reset clean
peek:
ifndef EX
	@echo "usage: make peek EX=1|2|3|4"
else
	@$(PY) python scripts/peek.py $(EX)
endif

solve:
	@read -p "Overwrite walgit/*.py with the reference solution? [y/N] " a; \
	 [ "$$a" = "y" ] || exit 0; \
	 cp reference/objects.py reference/wal.py reference/compact.py \
	    reference/repo.py reference/hooks.py walgit/; \
	 echo "reference implementation installed; 'make test' should be green"

reset:
	@read -p "Discard your work and restore the blanks? [y/N] " a; \
	 [ "$$a" = "y" ] || exit 0; \
	 cp reference/objects.py reference/wal.py reference/compact.py \
	    reference/repo.py reference/hooks.py walgit/; \
	 $(PY) python reference/.blank.py walgit

clean:
	rm -rf .walgit-cache .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
