*** Settings ***
Library       RequestsLibrary
Library       Collections
Resource      keywords/pipeline_keywords.robot
Variables     resources/variables.robot

Suite Setup   Tool Server Should Be Healthy


*** Test Cases ***
Tool Server Is Healthy
    [Documentation]    Verify the QA tool server is reachable before running acceptance tests.
    Tool Server Should Be Healthy

Pipeline Run Completes Successfully
    [Documentation]    Verify full pipeline run passes all QA gates on clean data.
    [Tags]    smoke    acceptance
    ${result}=    Run SQL Validator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Status Should Be Pass    ${result}
    Row Drop Should Be Under Threshold    ${result}    threshold=5.0
    Null Rate Should Be Acceptable    ${result}    column=customer_id    threshold=0.05

Schema Should Match Source
    [Documentation]    Verify target schema is identical to source schema (no drift).
    [Tags]    smoke    acceptance
    ${result}=    Run Schema Comparator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Status Should Be Pass    ${result}
    No Columns Should Be Removed    ${result}
    No Columns Should Be Renamed    ${result}

Logs Should Contain No Errors
    [Documentation]    Verify Spark run log reports zero errors on a clean run.
    [Tags]    smoke    acceptance
    ${result}=    Run Log Analyser    ${LOG_PATH}
    Status Should Be Pass    ${result}
    Error Count Should Be Zero    ${result}

Row Count Within Tolerance
    [Documentation]    Source and target row counts must be within 5% of each other.
    [Tags]    acceptance
    ${result}=    Run SQL Validator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Row Drop Should Be Under Threshold    ${result}    threshold=${ROW_DROP_THRESHOLD}

Null Rate Within Tolerance For Amount
    [Documentation]    Null rate for the amount column must be below threshold.
    [Tags]    acceptance
    ${result}=    Run SQL Validator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    Null Rate Should Be Acceptable    ${result}    column=amount    threshold=${NULL_RATE_THRESHOLD}

No Type Changes In Schema
    [Documentation]    No column type changes should exist between source and target.
    [Tags]    acceptance
    ${result}=    Run Schema Comparator    ${SOURCE_TABLE}    ${TARGET_TABLE}
    ${type_changes}=    Get From Dictionary    ${result}    type_changes
    Length Should Be    ${type_changes}    0    Unexpected type changes: ${type_changes}
