.PHONY: test check lint typecheck hygiene replay-golden bench

test:
	PYTHONPATH=src:. pytest -q

lint:
	ruff check src tests

typecheck:
	mypy src/scorpion

hygiene:
	bash scripts/check_repo_hygiene.sh

check: lint typecheck hygiene
	python -m compileall -q src research tests
	PYTHONPATH=src:. pytest -q

# Research box only (needs pandas + the frozen fwd logger); regenerates tests/fixtures goldens.
replay-golden:
	bash scripts/make_replay_golden.sh

bench:
	PYTHONPATH=src python scripts/bench_exec_latency.py --samples 2000 --out var/latency.json
