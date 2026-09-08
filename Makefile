.PHONY: test check

test:
	PYTHONPATH=src:. pytest -q

check:
	python -m compileall -q src research tests
	PYTHONPATH=src:. pytest -q
