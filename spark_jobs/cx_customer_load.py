"""
cx_customer_load.py — the real Hadoop-stack transform (T2.4), replacing
mock_pipeline's Postgres-mock movement for the customer_transactions
pipeline. Reads customer transaction data from the source Hive schema,
applies realistic CX transformations, and writes partitioned Parquet to
the target schema: a deduplicated fact table and an SCD Type 2 customer
dimension.

Split deliberately into pure DataFrame transforms (dedup, date/currency
normalisation, PII masking, SCD2) and a thin I/O layer (read_source/
read_existing_dim/main). The transforms are genuinely unit-tested against
a local SparkSession in tests/pytest/test_cx_customer_load.py — no Hive
or HDFS needed for that. The I/O layer needs a real Hive metastore and
HDFS to run and has NOT been executed anywhere; see docs/hadoop-stack.md.

Run with: spark-submit --master yarn spark_jobs/cx_customer_load.py --load-date 2026-09-13
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

SOURCE_TABLE = "source.customer_transactions"
DIM_TABLE = "target.customer_dim"
FACT_TABLE = "target.account_balance_fact"

DIM_TRACKED_COLS = ["customer_name", "region_code"]
DIM_SCHEMA = (
    "customer_id STRING, customer_name STRING, email STRING, region_code STRING, "
    "last_updated_source STRING, effective_date STRING, end_date STRING, is_current BOOLEAN"
)

# Static FX-to-USD table for demo currency normalisation -- a real
# deployment would read this from a rates table/service, not hardcode it.
FX_RATES_TO_USD = {"USD": 1.0, "GBP": 1.27, "EUR": 1.09, "INR": 0.012}


def dedup_on_natural_key(df: DataFrame, key_cols: list[str], order_col: str) -> DataFrame:
    """Keep the latest record per natural key (by order_col desc) --
    drops exact re-delivered duplicates and picks the newest version of a
    genuinely re-sent, changed row. See runbook RB-... duplicate_on_rerun
    in expansion-plan.md's failure-mode list for why this matters."""
    window = Window.partitionBy(*key_cols).orderBy(F.col(order_col).desc())
    return (
        df.withColumn("_rn", F.row_number().over(window))
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )


def mask_email(df: DataFrame, column: str = "email") -> DataFrame:
    """PII masking: keep the first character and the domain, mask the
    rest of the local part -- e.g. j***@example.com. A null or malformed
    (no '@') value masks to null rather than leaking the raw string."""
    return df.withColumn(
        column,
        F.when(
            F.col(column).isNotNull() & F.col(column).contains("@"),
            F.concat(F.substring(F.col(column), 1, 1), F.lit("***@"), F.substring_index(F.col(column), "@", -1)),
        ).otherwise(F.lit(None).cast("string")),
    )


def normalize_dates(df: DataFrame, column: str = "transaction_date") -> DataFrame:
    """Source dates arrive in more than one format (a common real-world
    CX data-quality issue) -- coalesce across the formats actually seen
    rather than failing or nulling out the whole row on a format
    mismatch with a single fixed pattern."""
    parsed = F.coalesce(
        F.to_date(F.col(column), "yyyy-MM-dd"),
        F.to_date(F.col(column), "MM/dd/yyyy"),
        F.to_date(F.col(column), "dd-MM-yyyy"),
    )
    return df.withColumn(column, parsed)


def normalize_currency(
    df: DataFrame,
    amount_col: str = "account_balance",
    currency_col: str = "currency_code",
    rates: dict[str, float] | None = None,
) -> DataFrame:
    """Convert amount_col from currency_col into a single `{amount_col}_usd`
    column; drops the pre-conversion amount and the currency column since
    the target schema is USD-only. An unrecognised currency code produces
    a null converted amount rather than a silently wrong number."""
    rates = rates or FX_RATES_TO_USD
    rate_map = F.create_map([F.lit(x) for pair in rates.items() for x in pair])
    return (
        df.withColumn("_rate", rate_map[F.col(currency_col)])
        .withColumn(
            f"{amount_col}_usd",
            F.when(F.col("_rate").isNotNull(), F.col(amount_col) * F.col("_rate")).otherwise(F.lit(None)),
        )
        .drop(amount_col, currency_col, "_rate")
    )


def apply_scd2(
    source_df: DataFrame,
    existing_dim_df: DataFrame,
    load_date: str,
    key_col: str = "customer_id",
    tracked_cols: list[str] | None = None,
) -> DataFrame:
    """
    Slowly Changing Dimension Type 2 on the customer dimension.

    For each customer_id present in source_df:
      - not in the current dimension at all -> insert a new current record.
      - present with identical tracked_cols -> no-op, the existing current
        record stands.
      - present with a changed tracked_cols value -> close out the old
        current record (end_date=load_date, is_current=False) and insert
        a new one (effective_date=load_date, end_date=None, is_current=True).
    A current record whose customer_id has no activity in this load is
    carried forward untouched.

    Returns the full new dimension state (history + carried-forward +
    closed-out + unchanged + newly-inserted). Column ambiguity in the
    join is resolved by aliasing both sides and reading columns off the
    join's own return value (`joined["src_customer_name"]`-style), not by
    string-keyed join column merging, which would make `cur`'s copy of
    the key unreadable for exactly the null-check this function needs.
    """
    tracked_cols = tracked_cols or DIM_TRACKED_COLS
    key_present_col = f"_cur_{key_col}"

    current = existing_dim_df.filter(F.col("is_current"))
    history = existing_dim_df.filter(~F.col("is_current"))

    src = source_df.select(key_col, *tracked_cols, "email", "last_updated_source")
    cur_renamed = current.select(
        F.col(key_col).alias(key_present_col),
        *[F.col(c).alias(f"_cur_{c}") for c in tracked_cols],
    )

    joined = src.join(cur_renamed, src[key_col] == cur_renamed[key_present_col], how="left")

    changed = F.lit(False)
    for col in tracked_cols:
        changed = changed | (F.col(col) != F.col(f"_cur_{col}"))
    is_new = F.col(key_present_col).isNull()
    is_changed = (~is_new) & changed
    is_unchanged = (~is_new) & (~changed)

    new_or_changed_keys = joined.filter(is_new | is_changed).select(key_col).distinct()
    new_records = (
        src.join(new_or_changed_keys, on=key_col, how="inner")
        .withColumn("effective_date", F.lit(load_date))
        .withColumn("end_date", F.lit(None).cast("string"))
        .withColumn("is_current", F.lit(True))
        .select(key_col, *tracked_cols, "email", "last_updated_source", "effective_date", "end_date", "is_current")
    )

    changed_keys = joined.filter(is_changed).select(F.col(key_col).alias(key_col)).distinct()
    closed_out = (
        current.join(changed_keys, on=key_col, how="inner")
        .withColumn("end_date", F.lit(load_date))
        .withColumn("is_current", F.lit(False))
    )

    unchanged_keys = joined.filter(is_unchanged).select(key_col).distinct()
    unchanged_current = current.join(unchanged_keys, on=key_col, how="inner")

    active_keys = source_df.select(key_col).distinct()
    untouched_current = current.join(active_keys, on=key_col, how="left_anti")

    return (
        history.unionByName(closed_out)
        .unionByName(unchanged_current)
        .unionByName(untouched_current)
        .unionByName(new_records)
    )


def build_fact(source_df: DataFrame, load_date: str) -> DataFrame:
    df = dedup_on_natural_key(source_df, ["transaction_id"], "last_updated_source")
    df = normalize_dates(df, "transaction_date")
    df = normalize_currency(df, "account_balance", "currency_code")
    return df.select(
        "transaction_id", "customer_id", "account_balance_usd", "transaction_date", "status"
    ).withColumn("load_date", F.lit(load_date))


def build_dim(source_df: DataFrame, existing_dim_df: DataFrame, load_date: str) -> DataFrame:
    staged = dedup_on_natural_key(source_df, ["customer_id"], "last_updated_source")
    staged = mask_email(staged, "email")
    dim = apply_scd2(staged, existing_dim_df, load_date)
    return dim.withColumn("load_date", F.lit(load_date))


def read_source(spark: SparkSession) -> DataFrame:
    return spark.table(SOURCE_TABLE)


def read_existing_dim(spark: SparkSession) -> DataFrame:
    try:
        return spark.table(DIM_TABLE)
    except Exception:
        return spark.createDataFrame([], schema=DIM_SCHEMA)


def main(load_date: str) -> None:
    spark = SparkSession.builder.appName("cx_customer_load").enableHiveSupport().getOrCreate()
    try:
        source_df = read_source(spark)
        existing_dim_df = read_existing_dim(spark)

        fact_df = build_fact(source_df, load_date)
        dim_df = build_dim(source_df, existing_dim_df, load_date)

        fact_df.write.mode("append").partitionBy("load_date").format("parquet").saveAsTable(FACT_TABLE)
        dim_df.write.mode("overwrite").partitionBy("load_date").format("parquet").saveAsTable(DIM_TABLE)
    finally:
        spark.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load-date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    args = parser.parse_args()
    main(args.load_date)
