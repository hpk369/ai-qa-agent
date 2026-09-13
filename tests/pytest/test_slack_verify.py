"""
Tests for agent.slack_verify.verify_slack_request. Covers a valid
signature, a tampered body, a wrong secret, a stale timestamp, a future
timestamp beyond skew, and missing headers (rejected, not raised).
"""

import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from agent.slack_verify import ALLOWED_SKEW_SECONDS, verify_slack_request

SECRET = "test-signing-secret"
FIXED_NOW = 1_700_000_000.0


def _sign(timestamp: int, body: bytes, secret: str = SECRET) -> str:
    base_string = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _headers(timestamp: int, signature: str) -> dict:
    return {"X-Slack-Request-Timestamp": str(timestamp), "X-Slack-Signature": signature}


class TestValidSignature:
    def test_valid_signature_is_accepted(self):
        body = b'{"type":"block_actions"}'
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, body)

        assert verify_slack_request(_headers(timestamp, signature), body, SECRET, now=FIXED_NOW) is True

    def test_header_lookup_is_case_insensitive(self):
        body = b"payload=abc"
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, body)
        headers = {"x-slack-request-timestamp": str(timestamp), "x-slack-signature": signature}

        assert verify_slack_request(headers, body, SECRET, now=FIXED_NOW) is True


class TestTamperedBody:
    def test_body_changed_after_signing_is_rejected(self):
        original_body = b'{"amount": 100}'
        tampered_body = b'{"amount": 999999}'
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, original_body)

        assert verify_slack_request(_headers(timestamp, signature), tampered_body, SECRET, now=FIXED_NOW) is False

    def test_reserialized_body_with_same_data_is_rejected(self):
        # Guards against comparing a re-parsed/re-serialized body instead
        # of the raw bytes — reordering keys must break the signature.
        original_body = b'{"a": 1, "b": 2}'
        reordered_body = b'{"b": 2, "a": 1}'
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, original_body)

        assert verify_slack_request(_headers(timestamp, signature), reordered_body, SECRET, now=FIXED_NOW) is False


class TestWrongSecret:
    def test_signature_from_different_secret_is_rejected(self):
        body = b"payload=abc"
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, body, secret="a-different-secret")

        assert verify_slack_request(_headers(timestamp, signature), body, SECRET, now=FIXED_NOW) is False


class TestTimestampSkew:
    def test_stale_timestamp_is_rejected(self):
        body = b"payload=abc"
        stale_timestamp = int(FIXED_NOW) - ALLOWED_SKEW_SECONDS - 1
        signature = _sign(stale_timestamp, body)

        assert verify_slack_request(_headers(stale_timestamp, signature), body, SECRET, now=FIXED_NOW) is False

    def test_timestamp_just_inside_skew_is_accepted(self):
        body = b"payload=abc"
        timestamp = int(FIXED_NOW) - ALLOWED_SKEW_SECONDS
        signature = _sign(timestamp, body)

        assert verify_slack_request(_headers(timestamp, signature), body, SECRET, now=FIXED_NOW) is True

    def test_future_timestamp_beyond_skew_is_rejected(self):
        body = b"payload=abc"
        future_timestamp = int(FIXED_NOW) + ALLOWED_SKEW_SECONDS + 1
        signature = _sign(future_timestamp, body)

        assert verify_slack_request(_headers(future_timestamp, signature), body, SECRET, now=FIXED_NOW) is False


class TestMissingHeaders:
    def test_missing_signature_header_is_rejected_not_raised(self):
        body = b"payload=abc"
        headers = {"X-Slack-Request-Timestamp": str(int(FIXED_NOW))}
        assert verify_slack_request(headers, body, SECRET, now=FIXED_NOW) is False

    def test_missing_timestamp_header_is_rejected_not_raised(self):
        body = b"payload=abc"
        headers = {"X-Slack-Signature": "v0=whatever"}
        assert verify_slack_request(headers, body, SECRET, now=FIXED_NOW) is False

    def test_empty_headers_is_rejected_not_raised(self):
        assert verify_slack_request({}, b"payload=abc", SECRET, now=FIXED_NOW) is False

    def test_non_numeric_timestamp_is_rejected_not_raised(self):
        headers = {"X-Slack-Request-Timestamp": "not-a-number", "X-Slack-Signature": "v0=whatever"}
        assert verify_slack_request(headers, b"payload=abc", SECRET, now=FIXED_NOW) is False

    def test_empty_signing_secret_is_rejected_not_raised(self):
        body = b"payload=abc"
        timestamp = int(FIXED_NOW)
        signature = _sign(timestamp, body)
        assert verify_slack_request(_headers(timestamp, signature), body, "", now=FIXED_NOW) is False
