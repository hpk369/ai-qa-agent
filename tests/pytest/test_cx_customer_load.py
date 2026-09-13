"""
Tests for spark_jobs/cx_customer_load.py's pure DataFrame transforms
(dedup, PII masking, date/currency normalisation, SCD2). Run against a
real local SparkSession (`local[2]`) — no Hive or HDFS needed, unlike
the module's I/O layer (read_source/read_existing_dim/main), which does
need both and is not exercised here; see docs/hadoop-stack.md.

Requires pyspark (requirements-spark.txt, not part of the main lite
profile's dependencies) — skipped entirely, not failed, when it isn't
installed, so `pytest tests/pytest/` stays dependency-free for anyone
not working on the Hadoop stack.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

pyspark = pytest.importorskip("pyspark")

from pyspark.sql import SparkSession  # noqa: E402

from spark_jobs.cx_customer_load import (  # noqa: E402
    DIM_SCHEMA,
    apply_scd2,
    build_dim,
    build_fact,
    dedup_on_natural_key,
    mask_email,
    normalize_currency,
    normalize_dates,
)


@pytest.fixture(scope="module")
def spark():
    session = (
        SparkSession.builder.master("local[2]")
        .appName("test_cx_customer_load")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def _rows(df):
    return [row.asDict() for row in df.collect()]


class TestDedupOnNaturalKey:
    def test_keeps_newest_of_a_redelivered_duplicate(self, spark):
        df = spark.createDataFrame(
            [("T1", "2026-01-01T00:00:00"), ("T1", "2026-01-02T00:00:00"), ("T2", "2026-01-01T00:00:00")],
            ["transaction_id", "last_updated_source"],
        )
        result = dedup_on_natural_key(df, ["transaction_id"], "last_updated_source")
        assert result.count() == 2
        t1 = [r for r in _rows(result) if r["transaction_id"] == "T1"][0]
        assert t1["last_updated_source"] == "2026-01-02T00:00:00"

    def test_no_duplicates_is_a_no_op(self, spark):
        df = spark.createDataFrame([("T1", "2026-01-01T00:00:00")], ["transaction_id", "last_updated_source"])
        result = dedup_on_natural_key(df, ["transaction_id"], "last_updated_source")
        assert result.count() == 1


class TestMaskEmail:
    def test_masks_valid_email_keeping_first_char_and_domain(self, spark):
        df = spark.createDataFrame([("john@example.com",)], ["email"])
        result = mask_email(df)
        assert _rows(result)[0]["email"] == "j***@example.com"

    def test_null_email_stays_null(self, spark):
        df = spark.createDataFrame([(None,)], "email STRING")
        result = mask_email(df)
        assert _rows(result)[0]["email"] is None

    def test_malformed_email_without_at_sign_masks_to_null_not_leaked(self, spark):
        df = spark.createDataFrame([("not-an-email",)], ["email"])
        result = mask_email(df)
        assert _rows(result)[0]["email"] is None


class TestNormalizeDates:
    def test_handles_multiple_source_formats(self, spark):
        df = spark.createDataFrame(
            [("2026-01-15",), ("01/20/2026",), ("25-12-2025",)], ["transaction_date"]
        )
        result = normalize_dates(df)
        dates = sorted(str(r["transaction_date"]) for r in _rows(result))
        assert dates == ["2025-12-25", "2026-01-15", "2026-01-20"]

    def test_unrecognised_format_becomes_null_not_a_crash(self, spark):
        df = spark.createDataFrame([("not-a-date",)], ["transaction_date"])
        result = normalize_dates(df)
        assert _rows(result)[0]["transaction_date"] is None


class TestNormalizeCurrency:
    def test_converts_known_currencies_to_usd(self, spark):
        df = spark.createDataFrame([(100.0, "USD"), (100.0, "GBP")], ["account_balance", "currency_code"])
        result = normalize_currency(df)
        values = sorted(r["account_balance_usd"] for r in _rows(result))
        assert values == [100.0, 127.0]

    def test_unrecognised_currency_becomes_null_not_a_wrong_number(self, spark):
        df = spark.createDataFrame([(100.0, "XYZ")], ["account_balance", "currency_code"])
        result = normalize_currency(df)
        assert _rows(result)[0]["account_balance_usd"] is None

    def test_drops_original_amount_and_currency_columns(self, spark):
        df = spark.createDataFrame([(100.0, "USD")], ["account_balance", "currency_code"])
        result = normalize_currency(df)
        assert set(result.columns) == {"account_balance_usd"}


class TestApplyScd2:
    def test_all_new_customers_get_current_records(self, spark):
        empty_dim = spark.createDataFrame([], schema=DIM_SCHEMA)
        source = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "US", "2026-01-01T00:00:00")],
            ["customer_id", "customer_name", "email", "region_code", "last_updated_source"],
        )
        result = apply_scd2(source, empty_dim, "2026-01-01")
        rows = _rows(result)
        assert len(rows) == 1
        assert rows[0]["is_current"] is True
        assert rows[0]["effective_date"] == "2026-01-01"
        assert rows[0]["end_date"] is None

    def test_changed_attribute_closes_old_and_opens_new_record(self, spark):
        day1_dim = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "US", "2026-01-01T00:00:00", "2026-01-01", None, True)],
            DIM_SCHEMA,
        )
        day2_source = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "UK", "2026-01-02T00:00:00")],
            ["customer_id", "customer_name", "email", "region_code", "last_updated_source"],
        )
        result = apply_scd2(day2_source, day1_dim, "2026-01-02")
        rows = _rows(result)
        assert len(rows) == 2

        current = [r for r in rows if r["is_current"]]
        closed = [r for r in rows if not r["is_current"]]
        assert len(current) == 1 and current[0]["region_code"] == "UK"
        assert current[0]["effective_date"] == "2026-01-02"
        assert len(closed) == 1 and closed[0]["region_code"] == "US"
        assert closed[0]["end_date"] == "2026-01-02"

    def test_unchanged_attributes_do_not_create_a_new_version(self, spark):
        day1_dim = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "US", "2026-01-01T00:00:00", "2026-01-01", None, True)],
            DIM_SCHEMA,
        )
        day2_source = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "US", "2026-01-02T00:00:00")],
            ["customer_id", "customer_name", "email", "region_code", "last_updated_source"],
        )
        result = apply_scd2(day2_source, day1_dim, "2026-01-02")
        rows = _rows(result)
        assert len(rows) == 1
        assert rows[0]["is_current"] is True
        assert rows[0]["effective_date"] == "2026-01-01"  # unchanged -- original record stands as-is

    def test_customer_with_no_activity_is_carried_forward_untouched(self, spark):
        day1_dim = spark.createDataFrame(
            [("C1", "Alice", "alice@example.com", "US", "2026-01-01T00:00:00", "2026-01-01", None, True)],
            DIM_SCHEMA,
        )
        no_activity = spark.createDataFrame(
            [], schema="customer_id STRING, customer_name STRING, email STRING, region_code STRING, last_updated_source STRING"
        )
        result = apply_scd2(no_activity, day1_dim, "2026-01-02")
        assert result.count() == 1
        assert _rows(result)[0]["customer_id"] == "C1"

    def test_history_rows_are_preserved_across_loads(self, spark):
        # Two loads' worth of history already in the dimension: a closed
        # record plus the current one.
        history_and_current = spark.createDataFrame(
            [
                ("C1", "Alice", "alice@example.com", "US", "2026-01-01T00:00:00", "2026-01-01", "2026-01-02", False),
                ("C1", "Alice", "alice@example.com", "UK", "2026-01-02T00:00:00", "2026-01-02", None, True),
            ],
            DIM_SCHEMA,
        )
        no_activity = spark.createDataFrame(
            [], schema="customer_id STRING, customer_name STRING, email STRING, region_code STRING, last_updated_source STRING"
        )
        result = apply_scd2(no_activity, history_and_current, "2026-01-03")
        assert result.count() == 2  # both rows carried forward, none dropped


class TestBuildFactAndDim:
    def test_end_to_end_produces_expected_shapes(self, spark):
        source = spark.createDataFrame(
            [
                ("TXN1", "C1", "Alice", "alice@example.com", "US", 100.0, "USD", "2026-01-15", "SETTLED", "2026-01-15T00:00:00"),
                ("TXN2", "C2", "Bob", "bob@example.com", "UK", 50.0, "GBP", "01/16/2026", "SETTLED", "2026-01-16T00:00:00"),
                ("TXN2", "C2", "Bob", "bob@example.com", "UK", 50.0, "GBP", "01/16/2026", "SETTLED", "2026-01-16T01:00:00"),
            ],
            [
                "transaction_id", "customer_id", "customer_name", "email", "region_code",
                "account_balance", "currency_code", "transaction_date", "status", "last_updated_source",
            ],
        )
        empty_dim = spark.createDataFrame([], schema=DIM_SCHEMA)

        fact = build_fact(source, "2026-01-16")
        assert fact.count() == 2  # TXN2's re-delivered duplicate collapsed to one
        assert set(fact.columns) == {
            "transaction_id", "customer_id", "account_balance_usd", "transaction_date", "status", "load_date"
        }

        dim = build_dim(source, empty_dim, "2026-01-16")
        assert dim.count() == 2
        emails = {r["email"] for r in _rows(dim)}
        assert emails == {"a***@example.com", "b***@example.com"}

    def test_fact_currency_conversion_is_correct(self, spark):
        source = spark.createDataFrame(
            [("TXN1", "C1", "Alice", "a@example.com", "US", 50.0, "GBP", "2026-01-16", "SETTLED", "2026-01-16T00:00:00")],
            [
                "transaction_id", "customer_id", "customer_name", "email", "region_code",
                "account_balance", "currency_code", "transaction_date", "status", "last_updated_source",
            ],
        )
        fact = build_fact(source, "2026-01-16")
        assert _rows(fact)[0]["account_balance_usd"] == pytest.approx(63.5)  # 50 * 1.27
