*** Settings ***
Library    RequestsLibrary
Library    Collections
Library    OperatingSystem
Variables  ../resources/variables.robot


*** Keywords ***
Run SQL Validator
    [Documentation]    POST to /tools/sql_validator and return the response body as a dict.
    [Arguments]    ${source_table}    ${target_table}    ${run_id}=test-run-001
    ${payload}=    Create Dictionary
    ...    source_table=${source_table}
    ...    target_table=${target_table}
    ...    run_id=${run_id}
    ${response}=    POST    ${TOOL_SERVER_URL}/tools/sql_validator    json=${payload}    expected_status=200
    RETURN    ${response.json()}

Run Log Analyser
    [Documentation]    POST to /tools/log_analyser and return the response body as a dict.
    [Arguments]    ${log_path}    ${run_id}=test-run-001
    ${payload}=    Create Dictionary
    ...    log_path=${log_path}
    ...    run_id=${run_id}
    ${response}=    POST    ${TOOL_SERVER_URL}/tools/log_analyser    json=${payload}    expected_status=200
    RETURN    ${response.json()}

Run Schema Comparator
    [Documentation]    POST to /tools/schema_comparator and return the response body as a dict.
    [Arguments]    ${source_table}    ${target_table}
    ${payload}=    Create Dictionary
    ...    source_table=${source_table}
    ...    target_table=${target_table}
    ${response}=    POST    ${TOOL_SERVER_URL}/tools/schema_comparator    json=${payload}    expected_status=200
    RETURN    ${response.json()}

Status Should Be Pass
    [Arguments]    ${result}
    Should Be Equal As Strings    ${result}[status]    PASS

Status Should Be Fail
    [Arguments]    ${result}
    Should Be Equal As Strings    ${result}[status]    FAIL

Row Drop Should Be Under Threshold
    [Arguments]    ${result}    ${threshold}=5.0
    ${drop}=    Convert To Number    ${result}[row_drop_pct]
    Should Be True    ${drop} < ${threshold}
    ...    Row drop ${drop}% exceeds threshold ${threshold}%

Null Rate Should Be Acceptable
    [Arguments]    ${result}    ${column}    ${threshold}=0.05
    ${rates}=    Get From Dictionary    ${result}    null_rates
    ${rate}=    Get From Dictionary    ${rates}    ${column}
    ${rate_num}=    Convert To Number    ${rate}
    Should Be True    ${rate_num} < ${threshold}
    ...    Null rate for ${column} is ${rate_num} which exceeds threshold ${threshold}

No Columns Should Be Removed
    [Arguments]    ${result}
    ${removed}=    Get From Dictionary    ${result}    columns_removed
    Length Should Be    ${removed}    0    Unexpected removed columns: ${removed}

No Columns Should Be Renamed
    [Arguments]    ${result}
    ${renamed}=    Get From Dictionary    ${result}    columns_renamed
    Length Should Be    ${renamed}    0    Unexpected renamed columns: ${renamed}

Error Count Should Be Zero
    [Arguments]    ${result}
    ${count}=    Convert To Integer    ${result}[error_count]
    Should Be Equal As Integers    ${count}    0

Tool Server Should Be Healthy
    ${response}=    GET    ${TOOL_SERVER_URL}/health    expected_status=200
    Should Be Equal As Strings    ${response.json()}[status]    ok
