"""
Log-set triage: the scope this project actually delivers.

A *log set* is a bundle of log files assembled for one session — mixed
from a corpus of real, public production logs (see logsets/corpus.py)
with a handful of ETL error signatures injected into it. The agent reads
only the log files, derives severity signals from the text, classifies
them with the existing deterministic classifier (agent/severity.py),
opens an incident, and alerts Slack. Every session's set is different,
and every set can be downloaded as a zip so the alert can be checked
against the logs that produced it.

Modules:
  catalog  — log sources and the error signatures the agent recognises
  corpus   — background log lines (real corpus if fetched, synthetic otherwise)
  session  — build / load / zip one session's log set
  triage   — analyse a log set, classify it, open an incident, alert Slack
"""
