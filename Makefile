# The two lanes share one gate, so a red tree cannot become a PR.
# `check` runs lint -> types -> tests in the only order that works: a red
# lint/type tree is caught before the slow tests, and a follow-up edit that
# fixes a long line cannot silently corrupt a docstring the way it did three
# separate times on the control-plane lane.
.PHONY: test check lint types

test:
	PYTHONPATH=. pytest -q

lint:
	ruff check scorpion_mff tests

types:
	mypy scorpion_mff

check:
	scripts/gate.sh
