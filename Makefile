VENV := .venv/bin
.PHONY: test lint types check run up down

test:
	$(VENV)/pytest -q tests/sheetbot
	node --test tests/sheetbot/bridge.test.cjs

lint:
	$(VENV)/ruff check src/fintracker tests/sheetbot
	$(VENV)/ruff format --check src/fintracker tests/sheetbot

types:
	$(VENV)/mypy src/fintracker

check: lint types test

run:
	$(VENV)/fintracker poll

up:
	docker compose up -d --build

down:
	docker compose down
