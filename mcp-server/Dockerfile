FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Puerto expuesto -- debe coincidir con var.mcp_server_port (terraform/ecs.tf)
EXPOSE 8080

# app.py expone el FastAPI real (endpoint HTTP /incident que llama el
# dispatcher de Alertmanager) -- server.py es la interfaz MCP/stdio para
# clientes tipo Claude Desktop, no lo que corre en ECS.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
