.PHONY: install dev test lint run-mcp collector-local clean

install:
	pip install -r mcp-server/requirements.txt

dev:
	pip install -r mcp-server/requirements.txt
	pip install -r lambda-collector/requirements.txt

test:
	pytest mcp-server/tests/ lambda-collector/tests/ -v

lint:
	ruff check mcp-server/ lambda-collector/

run-mcp:
	python mcp-server/server.py

# Ejecutar el collector localmente contra AWS (requiere credenciales)
collector-local:
	python -c "from lambda_collector.handler import handler; handler({'source': 'local'}, {})"

# Escribir script para que el usuario ejecute (runner universal)
run_script:
	@echo "Ejecutando _script.sh..."
	bash _script.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
