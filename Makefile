ECR_URL = 805778285334.dkr.ecr.us-east-1.amazonaws.com/sao-mcp-server
AWS_REGION = us-east-1

.PHONY: install dev test lint run-mcp build-collector docker-build docker-push docker-deploy collector-local clean

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
	rm -rf /tmp/sao-collector-build lambda-collector/collector.zip
	mkdir -p /tmp/sao-collector-build
	pip install -r lambda-collector/requirements-lambda.txt \
		--target /tmp/sao-collector-build \
		--quiet
	cp lambda-collector/handler.py /tmp/sao-collector-build/
	cp -r lambda-collector/collectors /tmp/sao-collector-build/
	cd /tmp/sao-collector-build && zip -r9 $(CURDIR)/lambda-collector/collector.zip . -x "*.pyc" -x "*/__pycache__/*"
	@echo "ZIP generado: lambda-collector/collector.zip"
	@du -sh lambda-collector/collector.zip

# Login ECR + build imagen MCP Server
docker-build:
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR_URL)
	docker build -t sao-mcp-server:latest .
	docker tag sao-mcp-server:latest $(ECR_URL):latest

# Push imagen al ECR
docker-push: docker-build
	docker push $(ECR_URL):latest

# Force new ECS deployment (despues de push)
docker-deploy: docker-push
	aws ecs update-service \
		--cluster sao-platform-cluster \
		--service sao-platform-service \
		--force-new-deployment \
		--region $(AWS_REGION) \
		--no-cli-pager

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
