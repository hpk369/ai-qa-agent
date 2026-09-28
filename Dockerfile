# The approval console / agent server. Host-agnostic on purpose: it is a
# plain uvicorn process on $PORT, which is what Fly, Render, Railway,
# Cloud Run and a bare VM all expect.
#
# One requirement that is not visible in this file: reports/ must be a
# PERSISTENT volume. The console's whole reason for existing is to hold
# incident records that outlive the CI runner that opened them, so a
# container with ephemeral storage forgets every incident on redeploy and
# every button lands on "unknown incident".
FROM python:3.11-slim

WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 10001 agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
COPY logsets/ logsets/
COPY config/ config/
COPY schemas/ schemas/
COPY docs/runbooks/ docs/runbooks/

RUN mkdir -p reports/incidents reports/evidence reports/logsets reports/slack \
 && chown -R agent:agent /app
USER agent

ENV AGENT_SERVER_HOST=0.0.0.0 \
    AGENT_SERVER_PORT=8001 \
    PYTHONUNBUFFERED=1
EXPOSE 8001

# $PORT wins where the platform sets one; the default keeps parity with
# the rest of the repo and with docs/SLACK_SETUP.md.
CMD ["sh", "-c", "exec uvicorn agent.agent:app --host 0.0.0.0 --port ${PORT:-8001}"]
