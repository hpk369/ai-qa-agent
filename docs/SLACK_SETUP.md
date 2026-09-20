# Connecting a real Slack workspace

The Slack layer (`agent/slack_client.py`, `agent/slack_blocks.py`,
`agent/slack_verify.py`, the `/slack/action` endpoint, the approval gate,
MTTA/MTTR Slack sync) runs against `SLACK_MODE=stub` out of the box,
writing every payload to `reports/slack/` with no network call. This is
the checklist for switching it to `SLACK_MODE=live`.

**Topology note:** Slack posting and interactivity live in the Python
agent server, not in n8n (see [`docs/workflow-map.md`](workflow-map.md)),
so **the tunnel needs to reach the agent server on port 8001**.

---

## 1. A dedicated Slack workspace

Create a new workspace — don't reuse an existing work one, since this bot
will post into it constantly during testing.

**Where:** [slack.com/get-started#create](https://slack.com/get-started#create) (free tier is enough)

## 2. A Slack app, created from the manifest in this repo

**Where:** [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest** → select your new workspace → paste the contents of [`docs/slack-app-manifest.json`](slack-app-manifest.json) into the **JSON** tab → **Next** → **Create**.

The manifest already requests exactly the scopes `agent/slack_client.py`
needs (`chat:write`, `chat:write.public`, `reactions:read`,
`channels:history`) and enables interactivity. Its `request_url` is a
placeholder — you'll come back and fix it after step 5 (Slack lets you
edit this anytime under **Interactivity & Shortcuts** in the left sidebar
of your app's settings).

If you make the four channels below **private** rather than public, add
the `groups:history` scope too (Slack scopes it separately from public
channels) — under **OAuth & Permissions → Scopes → Bot Token Scopes**.

## 3. Install the app and collect its credentials

1. In your app's settings, go to **Install App** (left sidebar) → **Install to Workspace** → **Allow**.
2. Copy the **Bot User OAuth Token** (starts `xoxb-`) from that same page → this is `SLACK_BOT_TOKEN`.
3. Go to **Basic Information** (left sidebar) → **App Credentials** → copy **Signing Secret** → this is `SLACK_SIGNING_SECRET`. (Verified by `agent/slack_verify.py` on every inbound button click.)

## 4. Create the four channels and invite the bot

Create these in your new workspace (any naming works, but the code's
comments/docs assume these names):

| Channel | What it's for |
|---|---|
| `#etl-prod-alerts` | Every incident, all severities — the parent message lives here |
| `#etl-prod-p1` | P1 mirror, `@here` on post |
| `#etl-changes` | Approve/Reject/Escalate audit trail |
| `#etl-daily` | Reserved for a future digest — not posted to by any code yet |

Invite your app's bot user to each one (`/invite @Triage Agent` typed
into each channel, or **Integrations → Add apps** from the channel
details panel).

Then get each channel's **ID** (not its name) — right-click the channel
name → **View channel details** → scroll to the bottom → **Channel ID**
(starts with `C`). Set:

```
SLACK_CHANNEL_ALERTS=C...
SLACK_CHANNEL_P1=C...
SLACK_CHANNEL_CHANGES=C...
SLACK_CHANNEL_DAILY=C...
```

## 5. A public tunnel to the agent server (port 8001)

Slack needs to reach your machine to deliver button clicks to
`/slack/action`. Cloudflare Tunnel is recommended:

```bash
# quick/ephemeral — hostname changes every restart, fine for initial testing
cloudflared tunnel --url http://localhost:8001

# stable hostname — needs a domain you control added to Cloudflare
cloudflared tunnel create etl-triage-agent
cloudflared tunnel route dns etl-triage-agent triage.inkandinfra.com
cloudflared tunnel run --url http://localhost:8001 etl-triage-agent
```

Either way, note the resulting `https://...` hostname — that's your
`PUBLIC_WEBHOOK_BASE`.

**ngrok** works identically if you already use it: `ngrok http 8001`.

Now go back to your Slack app's **Interactivity & Shortcuts** page and
set the **Request URL** to `{PUBLIC_WEBHOOK_BASE}/slack/action` (e.g.
`https://triage.inkandinfra.com/slack/action`) — Slack will send a test
ping the moment you save this, so the agent server needs to already be
running (`python agent/agent.py`, or via `docker compose up`) before you
save it.

## 6. Populate `.env`

```bash
cp .env.example .env
```

Fill in everything from steps 3–5, plus:

```
SLACK_MODE=live
```

Leave `SLACK_MODE=stub` (or unset) for local development without
touching the real workspace — that's still the default and is fully
covered by the test suite.

## 7. Verify

```bash
# Start the agent server (needs ANTHROPIC_API_KEY set too)
python agent/agent.py
# or: docker compose up agent_server

# Trigger a run that will open an incident
INJECT_FAILURE=row_drop python mock_pipeline/producer.py
```

You should see a Block Kit message land in `#etl-prod-alerts` within a
few seconds, with Approve/Reject/Escalate buttons. Click one — it should
acknowledge instantly and a decision should appear in `#etl-changes` a
moment later. If nothing arrives, check the agent server's logs first
(`notify_slack`/`process_slack_action` log every failure loudly, per
this codebase's "Slack is a view, never the source of truth" design —
nothing here fails silently).
