# Workflow Map — n8n + the Triage Agent's own Slack integration

This documents the full incident-notification path, split across two
processes: the n8n workflow (`n8n_workflows/qa_agent_workflow.json`)
still owns triggering, running the right validation framework, and
notifying Jenkins/the original caller — but it no longer touches Slack at
all. The Python agent server (`agent/agent.py`) now owns every Slack
interaction: posting the incident, mirroring P1s, and handling button
clicks.

## Why Slack moved out of n8n

The Slack Web API client, Block Kit builders and HMAC signature
verification are Python, each fully unit-tested with the HTTP layer
mocked. `agent/slack_client.py::post_incident` mutates the
`Incident` object (writing back `slack_ts`/`slack_channel`) and
re-persists it in the same call, which only makes sense from the process
that owns `agent/incident.py::persist`. Re-implementing Block Kit
rendering and HMAC verification a second time in n8n's JS Code nodes
would mean maintaining two implementations of the same logic — one of
them untested in this environment, since there is no running n8n instance
here to verify JS against. So: **Slack's Interactivity & Shortcuts Request
URL should point at the agent server's `/slack/action` endpoint directly,
not at an n8n webhook.**

## n8n workflow (`n8n_workflows/qa_agent_workflow.json`)

```
① Pipeline Trigger      (Webhook — POST /pipeline-trigger)
         │
② Call Triage Agent     (HTTP Request → agent_server:8001/agent/run, 60s timeout)
         │              Claude tool-use loop + severity classification run here;
         │              agent/agent.py::notify_slack posts to Slack from inside
         │              this same call, before the HTTP response returns.
         │
③ Check Incident Status (IF node — clean == true)
         │
    TRUE ┤                              FALSE
   clean │                            incident
         ▼                                 ▼
④ Run Robot Framework           ⑤ Run pytest
         │                                 │
⑥ Read RF Report                ⑦ Read pytest Report
         │                                 │
         └──────────────┬──────────────────┘
                        ▼
               ⑧ Merge Reports
                        │
          ⑨ Build Incident Record   (Code node — JS)
             Shapes the summary object for Jenkins/the caller.
             No longer builds a Slack message — see above.
                        │
              ⑩ Jenkins Webhook
              POST summary JSON to CI
                        │
            ⑪ Respond to Webhook
            Returns summary JSON to caller
```

### Node reference

| # | Node | n8n Type | Key Config | Purpose |
|---|------|----------|------------|---------|
| 1 | **Pipeline Trigger** | Webhook | `POST /pipeline-trigger`, `responseMode: responseNode` | Entry point. Holds the connection open until node ⑪ fires. |
| 2 | **Call Triage Agent** | HTTP Request | `POST agent_server:8001/agent/run`, timeout 60 s | Runs the full agent loop *and* the Slack post for any incident opened — both complete before this node returns. |
| 3 | **Check Incident Status** | IF | `$json.clean == true` | True (clean) → validation/restoration check. False (incident) → diagnostic deep-dive. |
| 4 | **Run Robot Framework** | Execute Command | `robot --outputdir reports/robot tests/robot/acceptance.robot` | |
| 5 | **Run pytest** | Execute Command | `pytest tests/pytest/ --junitxml=reports/pytest/results.xml -v` | |
| 6 | **Read RF Report** | Read Binary File | `/qa/reports/robot/output.xml` | |
| 7 | **Read pytest Report** | Read Binary File | `/qa/reports/pytest/results.xml` | |
| 8 | **Merge Reports** | Merge | `mergeByPosition` | Pass-through convergence — only one branch ever ran. |
| 9 | **Build Incident Record** | Code (JS) | See `n8n_workflows/qa_agent_workflow.json` | Builds the `summary` object (including `slack_channel`/`slack_ts`, already set by the agent by this point) for Jenkins and the caller. |
| 10 | **Jenkins Webhook** | HTTP Request | `POST $env.JENKINS_WEBHOOK_URL` | Fires a downstream Jenkins job with `summary` as payload. |
| 11 | **Respond to Webhook** | Respond to Webhook | `respondWith: json` | Returns `summary` to whoever called node ①. |

Removed in this task: the **Slack Alert** node (previously an incoming
webhook, `$env.SLACK_WEBHOOK_URL`). `SLACK_WEBHOOK_URL` in `.env.example`
is now legacy/unused by this workflow — kept only as a reminder of what it
used to do until someone removes the variable entirely.

## The agent server's Slack surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/agent/run` | POST | Existing endpoint (n8n calls this). Now also posts a newly opened incident to Slack (`agent.agent.notify_slack`) before returning, using `SLACK_MODE` (`stub` by default — see `.env.example`). |
| `/slack/action` | POST | **New.** Slack's Interactivity Request URL points here. Verifies `X-Slack-Signature`/`X-Slack-Request-Timestamp` via `agent.slack_verify.verify_slack_request`; an invalid signature is rejected with `401` before the payload is parsed. A valid request is acknowledged with `200` immediately (Slack's 3-second deadline is non-negotiable), and the actual decision processing runs in a FastAPI `BackgroundTask` (`agent.agent.process_slack_action`) after the response is sent. |

### `/slack/action` sequence

```
Slack button click
       │
POST /slack/action  (raw body + X-Slack-Signature/X-Slack-Request-Timestamp)
       │
verify_slack_request()  ──fail──▶ 401, stop (payload never parsed)
       │ pass
200 {"ok": true}   ◄── returned immediately, satisfies the 3s deadline
       │
       ▼ (background task, after the response)
process_slack_action()
   → agent.incident.load(incident_id)
   → agent.incident.record_approval_decision(...)   [records to the
     timeline and persists, applies the approval-gate rules: rejecting a
     second decision, posting to #etl-changes, escalate re-opening +
     mirroring to #etl-prod-p1]
```

## Human setup this implies

Point the Slack app's **Interactivity & Shortcuts** Request URL at
`{PUBLIC_WEBHOOK_BASE}/slack/action` on the **agent server** (port 8001),
not at n8n (port 5678).

For the full step-by-step (creating the workspace, the app manifest to
use, where every `.env` value comes from), see
[`docs/SLACK_SETUP.md`](SLACK_SETUP.md).
