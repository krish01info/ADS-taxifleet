"""Shared bounded Spark session for the Phase 1 integration risk tests."""

import os
import sys
import time

import pytest
from pyspark.sql import SparkSession


@pytest.fixture(scope="session")
def phase1_spark():
    """Use a UTC session and two local workers; downloaded trip files are never required."""
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):  # not available on Windows; Spark gets its time zone from the config below
        time.tzset()
    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    session = (SparkSession.builder.master("local[2]").appName("phase1-integration-tests")
               .config("spark.driver.bindAddress", "127.0.0.1").config("spark.ui.enabled", "false")
               .config("spark.sql.session.timeZone", "UTC").config("spark.sql.shuffle.partitions", "2")
               .config("spark.sql.parquet.outputTimestampType", "TIMESTAMP_MICROS")
               .config("spark.sql.ansi.enabled", "true").getOrCreate())
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()
