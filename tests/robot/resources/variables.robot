*** Variables ***
${TOOL_SERVER_HOST}    %{TOOL_SERVER_HOST=localhost}
${TOOL_SERVER_PORT}    %{TOOL_SERVER_PORT=8000}
${TOOL_SERVER_URL}     http://${TOOL_SERVER_HOST}:${TOOL_SERVER_PORT}

${SOURCE_TABLE}        src.transactions
${TARGET_TABLE}        tgt.transactions
${LOG_PATH}            /logs/spark_run_test.log

${ROW_DROP_THRESHOLD}     5.0
${NULL_RATE_THRESHOLD}    0.05
