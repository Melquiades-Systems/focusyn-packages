SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test check deadcode cli-check cli-install plugin-check

help:  ## Lista los targets disponibles
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Crea/actualiza el .venv del repo (raíz) con todos los miembros del workspace
	uv sync --all-packages

lint:  ## ruff
	uv run ruff check cli

fmt:  ## ruff --fix + format
	uv run ruff check --fix cli && uv run ruff format cli

typecheck:  ## mypy strict
	uv run mypy cli/src cli/tests

test:  ## pytest del CLI
	uv run pytest cli/tests -q

check: lint typecheck test cli-check plugin-check  ## todo lo anterior + los dos guards

# Frontera de confianza como TEST: el wheel del cliente NO debe arrastrar ninguna dep del gateway.
# Este guard vino del Makefile de focusyn cuando el CLI era miembro de aquel workspace; ahora que el
# artefacto vive acá, el guard vive acá — es lo que hace ejecutable la separación entre los dos repos
# ("el cliente es delgado") en vez de dejarla en prosa.
CLI_FORBIDDEN := fastapi starlette sqlalchemy asyncpg alembic pgvector pygit2 grpcio protobuf \
                 redis cryptography argon2-cffi mcp tree-sitter uvicorn slowapi
CLI_CHECK_DIR := /tmp/focusyn-cli-check

cli-check:  ## Verifica que el wheel del cliente sea delgado (sin deps del gateway)
	@rm -rf $(CLI_CHECK_DIR) && mkdir -p $(CLI_CHECK_DIR)
	@uv build --package focusyn-cli -o $(CLI_CHECK_DIR)/dist >/dev/null
	@uv venv $(CLI_CHECK_DIR)/venv >/dev/null 2>&1
	@VIRTUAL_ENV=$(CLI_CHECK_DIR)/venv uv pip install $(CLI_CHECK_DIR)/dist/*.whl >/dev/null 2>&1
	@installed=$$(VIRTUAL_ENV=$(CLI_CHECK_DIR)/venv uv pip list --format=freeze | cut -d= -f1 | tr 'A-Z' 'a-z'); \
	 bad=""; for dep in $(CLI_FORBIDDEN); do echo "$$installed" | grep -qx "$$dep" && bad="$$bad $$dep"; done; \
	 if [ -n "$$bad" ]; then echo "✗ el cliente arrastra deps del gateway:$$bad"; exit 1; fi; \
	 echo "✓ cliente delgado: sin deps del gateway ($$(echo "$$installed" | wc -l) paquetes)"

# El plugin es JSON + un script: no hay compilador que lo valide. Sin este guard, un JSON roto o un
# script sin permiso de ejecución sólo se descubre en la máquina del usuario, al instalar.
plugin-check:  ## Valida los JSON del plugin/marketplace y que el hook sea ejecutable
	@python3 -c "import json,sys; [json.load(open(f)) for f in sys.argv[1:]]; print('✓ JSON válido:', len(sys.argv)-1, 'archivos')" \
	  mcp/.claude-plugin/plugin.json mcp/hooks/hooks.json .claude-plugin/marketplace.json
	@test -x mcp/scripts/memory-sync.sh \
	  && echo "✓ hook ejecutable: mcp/scripts/memory-sync.sh" \
	  || { echo "✗ mcp/scripts/memory-sync.sh no tiene bit de ejecución (chmod +x)"; exit 1; }
	@bash -n mcp/scripts/memory-sync.sh && echo "✓ sintaxis del hook OK"

deadcode:  ## Candidatos a dead code del propio CLI (reporte, no gate)
	uv run focusyn deadcode

cli-install:  ## Instala el CLI editable en el PATH (uv tool) para usarlo en otros repos
	uv tool install --editable ./cli
