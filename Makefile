# Команды передачи и эксплуатации (OPS-06).
.PHONY: help up down migrate check lint types test test-pg run-api run-worker run-scheduler evidence image trace

VENV := .venv/bin

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Поднять PostgreSQL 17 и хранилище
	docker compose up -d postgres
	@until docker exec fintracker_pg pg_isready -U fintracker_owner -d fintracker >/dev/null 2>&1; do sleep 1; done
	@docker exec fintracker_pg psql -U fintracker_owner -d fintracker -v ON_ERROR_STOP=1 -c \
	  "DO \$$\$$ BEGIN \
	     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fintracker_api') THEN \
	       CREATE ROLE fintracker_api LOGIN PASSWORD 'devpassword' NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; \
	     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fintracker_worker') THEN \
	       CREATE ROLE fintracker_worker LOGIN PASSWORD 'devpassword' NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; \
	   END \$$\$$; \
	   GRANT CONNECT ON DATABASE fintracker TO fintracker_api, fintracker_worker; \
	   GRANT USAGE ON SCHEMA public TO fintracker_api, fintracker_worker;" >/dev/null

down: ## Остановить среду
	docker compose down

migrate: ## Применить миграции
	$(VENV)/alembic upgrade head

lint: ## Формат и линтер
	$(VENV)/ruff format --check src tests
	$(VENV)/ruff check src tests

types: ## Типизация
	$(VENV)/mypy src/fintracker

test: ## Все проверки
	$(VENV)/pytest -q

test-pg: ## Только проверки на настоящей PostgreSQL
	$(VENV)/pytest -q -m pg

check: lint types test ## Полный набор проверок

evidence: ## Собрать доказательства проверок
	$(VENV)/python .planning/tools/collect_evidence.py

trace: ## Проверить прослеживаемость по фактическому прогону
	$(VENV)/python .planning/tools/check_traceability.py

image: ## Собрать единый артефакт api/worker/scheduler
	docker build -t fintracker:local .

run-api: ## Запустить API
	$(VENV)/fintracker api

run-worker: ## Запустить фоновый исполнитель
	$(VENV)/fintracker worker

run-scheduler: ## Запустить планировщик
	$(VENV)/fintracker scheduler
