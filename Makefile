.PHONY: install dev test lint run-mcp build-collector collector-local clean

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

# Empaquetar Lambda Collector — genera lambda-collector/collector.zip
build-collector:
	rm -rf /tmp/sao-collector-build
	mkdir -p /tmp/sao-collector-build
	pip install -r lambda-collector/requirements.txt \
		--target /tmp/sao-collector-build \
		--quiet \
		--no-deps boto3 botocore  # ya vienen en el runtime python3.12
	cp lambda-collector/handler.py /tmp/sao-collector-build/
	cp -r lambda-collector/collectors /tmp/sao-collector-build/
	cd /tmp/sao-collector-build && zip -r9 $(CURDIR)/lambda-collector/collector.zip . -x "*.pyc" -x "*/__pycache__/*"
	@echo "ZIP generado: lambda-collector/collector.zip"
	@du -sh lambda-collector/collector.zip

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
