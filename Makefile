.PHONY: setup doctor dev build run test

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.txt
	.venv/bin/python scripts/frontend.py install

doctor:
	python3 scripts/frontend.py doctor

dev:
	.venv/bin/python scripts/dev.py

build:
	.venv/bin/python scripts/frontend.py build

run: build
	.venv/bin/python -m studio.app

test:
	.venv/bin/python -m pytest
	.venv/bin/python scripts/frontend.py test
