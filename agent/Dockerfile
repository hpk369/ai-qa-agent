FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY ../mock_pipeline /app/mock_pipeline
COPY ../agent_tools /app/agent_tools

EXPOSE 8001

CMD ["python", "agent/agent.py"]
