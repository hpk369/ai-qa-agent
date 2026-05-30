FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY ../mock_pipeline /app/mock_pipeline

EXPOSE 8000

CMD ["python", "tool_server.py"]
