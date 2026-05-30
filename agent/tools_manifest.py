"""
Tool definitions for the Claude API tool-use loop.
Each entry matches the corresponding tool server endpoint request/response schema.
"""

TOOLS = [
    {
        "name": "sql_validator",
        "description": (
            "Validates data quality between source and target tables. "
            "Checks row counts (detects drops), null rates per column, and duplicate counts. "
            "Returns status PASS or FAIL with a list of issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_table": {
                    "type": "string",
                    "description": "Fully qualified source table name, e.g. src.transactions",
                },
                "target_table": {
                    "type": "string",
                    "description": "Fully qualified target table name, e.g. tgt.transactions",
                },
                "run_id": {
                    "type": "string",
                    "description": "Pipeline run UUID for traceability",
                },
            },
            "required": ["source_table", "target_table"],
        },
    },
    {
        "name": "log_analyser",
        "description": (
            "Parses Spark and Kafka logs for errors, warnings, and consumer lag. "
            "Returns error/warning counts, error messages, Kafka lag, and status PASS or FAIL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "log_path": {
                    "type": "string",
                    "description": "Absolute path to the Spark run log file",
                },
                "run_id": {
                    "type": "string",
                    "description": "Pipeline run UUID for correlation",
                },
            },
            "required": ["log_path"],
        },
    },
    {
        "name": "schema_comparator",
        "description": (
            "Compares source and target table schemas to detect drift. "
            "Reports added, removed, renamed columns and type changes. "
            "Returns status PASS or FAIL with a list of issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_table": {
                    "type": "string",
                    "description": "Fully qualified source table name",
                },
                "target_table": {
                    "type": "string",
                    "description": "Fully qualified target table name",
                },
            },
            "required": ["source_table", "target_table"],
        },
    },
]
