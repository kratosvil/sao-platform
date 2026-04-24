ECR_URL = 805778285334.dkr.ecr.us-east-1.amazonaws.com/sao-mcp-server
AWS_REGION = us-east-1

.PHONY: install dev test lint run-mcp build-collector build-hitl docker-build docker-push docker-deploy collector-local clean

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

# El ZIP de HITL lo genera Terraform via data archive_file — no requiere build manual.
# Este target es solo para verificar la sintaxis del handler localmente.
build-hitl:
	python -c "import ast; ast.parse(open('lambda-hitl/handler.py').read()); print('handler.py OK')"

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

# Listar todas las propuestas HITL guardadas en S3
list-proposals:
	aws s3 ls s3://kratosvil-sao-graph-805778285334/proposals/ --region $(AWS_REGION)

# Ver detalle de una propuesta: make show-proposal TOKEN=<uuid>
show-proposal:
	aws s3 cp s3://kratosvil-sao-graph-805778285334/proposals/$(TOKEN).json - --region $(AWS_REGION)

# Logs recientes del Lambda dispatcher
logs-dispatcher:
	aws logs tail /aws/lambda/sao-alarm-dispatcher --since 10m --region $(AWS_REGION)

# Logs recientes del MCP Server (ECS)
logs-mcp:
	aws logs tail /ecs/sao-platform --since 10m --region $(AWS_REGION)

# Apuntar el servicio ECS a la ultima task definition (con todos los env vars)
fix-taskdef:
	$(eval LATEST := $(shell aws ecs list-task-definitions --family-prefix sao-platform-task --sort DESC --query "taskDefinitionArns[0]" --output text --region $(AWS_REGION)))
	@echo "Actualizando servicio a: $(LATEST)"
	aws ecs update-service --cluster sao-platform-cluster --service sao-platform-service --task-definition $(LATEST) --force-new-deployment --region $(AWS_REGION) --no-cli-pager

# Escribir script para que el usuario ejecute (runner universal)
run_script:
	@echo "Ejecutando _script.sh..."
	bash _script.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
