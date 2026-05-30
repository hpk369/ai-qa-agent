"""
FastAPI tool server — exposes sql_validator, log_analyser, schema_comparator as HTTP endpoints.
n8n calls these via HTTP Request nodes; the AI agent also calls them directly.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_tools.log_analyser import LogAnalyser
from agent_tools.schema_comparator import SchemaComparator
from agent_tools.sql_validator import SQLValidator

app = FastAPI(title="QA Agent Tool Server", version="1.0.0")


# ---------- Request models ----------

class SQLValidatorRequest(BaseModel):
    source_table: str
    target_table: str
    run_id: str = ""


class LogAnalyserRequest(BaseModel):
    log_path: str
    run_id: str = ""


class SchemaComparatorRequest(BaseModel):
    source_table: str
    target_table: str


# ---------- Endpoints ----------

@app.post("/tools/sql_validator")
def sql_validator(req: SQLValidatorRequest):
    return SQLValidator().validate(req.source_table, req.target_table, req.run_id)


@app.post("/tools/log_analyser")
def log_analyser(req: LogAnalyserRequest):
    return LogAnalyser().analyse(req.log_path, req.run_id)


@app.post("/tools/schema_comparator")
def schema_comparator(req: SchemaComparatorRequest):
    return SchemaComparator().compare(req.source_table, req.target_table)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("TOOL_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
