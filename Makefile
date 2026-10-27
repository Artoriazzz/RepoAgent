PYTHON ?= .venv/bin/python
MVN ?= mvn
NPM ?= npm
COMPOSE ?= docker compose

.PHONY: help install frontend-install infra infra-down backend frontend worker test test-python test-backend frontend-build

help:
	@printf '%s\n' 'make install           Install Python development/Worker dependencies' \
	  'make frontend-install  Install frontend dependencies from lockfile' \
	  'make infra             Start MySQL, Redis, RabbitMQ and Qdrant' \
	  'make backend           Run Spring Boot on port 8080' \
	  'make frontend          Run the frontend on port 5173' \
	  'make worker            Run Python stage workers' \
	  'make test              Run Python/Java tests and build the frontend' \
	  'make infra-down        Stop dependencies, preserving data volumes'

install:
	$(PYTHON) -m pip install -e '.[dev,worker,benchmark]'

frontend-install:
	$(NPM) --prefix backend/frontend ci

infra:
	$(COMPOSE) --env-file .env -f backend/compose.yaml up -d --wait

infra-down:
	$(COMPOSE) --env-file .env -f backend/compose.yaml down

backend:
	$(MVN) -f backend/pom.xml spring-boot:run

frontend:
	$(NPM) --prefix backend/frontend run dev

worker:
	$(PYTHON) -m repo_agent.worker.main

test: test-python test-backend frontend-build

test-python:
	$(PYTHON) -m pytest -q

test-backend:
	$(MVN) --batch-mode -f backend/pom.xml test

frontend-build:
	$(NPM) --prefix backend/frontend run build
