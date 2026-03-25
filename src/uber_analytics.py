"""Uber rides analytics helpers.

This module keeps notebook code small and repeatable:
- load/clean data
- feature engineering
- peak & trend analysis
- anomaly detection
- plotting helpers

Designed for use from `uber_enhanced.ipynb`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import numpy as np
import pandas as pd


DAY_ORDER = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


@dataclass(frozen=True)
class PeakResult:
    peak_hours: pd.Series
    peak_days: pd.Series
    hour_threshold: float
    day_threshold: float


@dataclass(frozen=True)
class AnomalyResult:
    long_trips: pd.DataFrame
    short_trips: pd.DataFrame
    very_fast: pd.DataFrame
    very_slow: pd.DataFrame
    duration_bounds: Tuple[float, float]
    mpm_bounds: Tuple[float, float]


def load_uber_data(
    *,
    cleaned_csv_path: str = "UberDatasetCleaned.csv",
    raw_csv_path: str = "UberDataset.csv",
    prefer_cleaned: bool = True,
) -> pd.DataFrame:
    """Load Uber rides data.

    If `prefer_cleaned` is True and the cleaned CSV exists/readable, it will be used.
    Otherwise the raw CSV is loaded.
    """

    if prefer_cleaned:
        try:
            return pd.read_csv(cleaned_csv_path)
        except Exception:
            pass

    return pd.read_csv(raw_csv_path)


def normalize_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names and types for downstream analysis.

    Supports both the project's raw schema and the project's cleaned schema.
    """

    df = df.copy()

    # If this is the raw dataset, create the cleaned-like columns.
    if "START_DATE" in df.columns and "END_DATE" in df.columns:
        df["PURPOSE"] = df.get("PURPOSE").ffill()

        df["START_DATE"] = pd.to_datetime(df["START_DATE"], errors="coerce")
        df["END_DATE"] = pd.to_datetime(df["END_DATE"], errors="coerce")

        df["start_date"] = df["START_DATE"].dt.date.astype("string")
        df["end_date"] = df["END_DATE"].dt.date.astype("string")

        df["start_hour"] = df["START_DATE"].dt.hour.astype("float")
        df["start_minute"] = df["START_DATE"].dt.minute.astype("float")
        df["end_hour"] = df["END_DATE"].dt.hour.astype("float")
        df["end_minute"] = df["END_DATE"].dt.minute.astype("float")

        df["month"] = df["START_DATE"].dt.strftime("%b")
        df["year"] = df["START_DATE"].dt.year

        df["duration"] = (
            (df["END_DATE"] - df["START_DATE"]).dt.total_seconds() / 60.0
        )

        df["day"] = df["START_DATE"].dt.day_name()

        df.drop(columns=["START_DATE", "END_DATE"], inplace=True)

    # Coerce known numeric columns.
    for col in ["MILES", "duration", "speed", "start_hour", "start_minute"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Ensure category/purpose strings are clean.
    for col in ["CATEGORY", "PURPOSE", "START", "STOP", "day", "day_time", "month"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()

    # Standardize day ordering.
    if "day" in df.columns:
        df["day"] = pd.Categorical(df["day"], categories=DAY_ORDER, ordered=True)

    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add analysis features used throughout the notebook."""

    df = df.copy()

    # Build a proper timestamp to enable trend analysis.
    if "start_date" in df.columns and "start_hour" in df.columns and "start_minute" in df.columns:
        start_dt = pd.to_datetime(df["start_date"], errors="coerce")
        hour = df["start_hour"].fillna(0).astype(int)
        minute = df["start_minute"].fillna(0).astype(int)
        df["start_dt"] = start_dt + pd.to_timedelta(hour, unit="h") + pd.to_timedelta(minute, unit="m")

    # Miles per minute (efficiency). Guard against zero/negative duration.
    if "MILES" in df.columns and "duration" in df.columns:
        duration = df["duration"].where(df["duration"] > 0)
        df["miles_per_min"] = df["MILES"] / duration

    # Recompute speed in mph if missing/inconsistent.
    if "speed" not in df.columns and "MILES" in df.columns and "duration" in df.columns:
        df["speed"] = (df["MILES"] / (df["duration"] / 60.0)).replace([np.inf, -np.inf], np.nan)

    # Peak-friendly hour (int) column.
    if "start_hour" in df.columns:
        df["start_hour_int"] = pd.to_numeric(df["start_hour"], errors="coerce").round().astype("Int64")

    # Convenience: weekend flag.
    if "day" in df.columns:
        df["is_weekend"] = df["day"].isin(["Saturday", "Sunday"]).astype("boolean")

    return df


def detect_peaks(
    df: pd.DataFrame,
    *,
    peak_quantile: float = 0.9,
    min_count: int = 5,
) -> PeakResult:
    """Detect demand peaks by hour and day using a quantile threshold."""

    if "start_hour_int" not in df.columns or "day" not in df.columns:
        raise ValueError("DataFrame must include `start_hour_int` and `day`.")

    rides_by_hour = (
        df.dropna(subset=["start_hour_int"]).groupby("start_hour_int").size().sort_index()
    )
    rides_by_day = df.groupby("day").size().reindex(DAY_ORDER)

    hour_threshold = float(rides_by_hour.quantile(peak_quantile)) if len(rides_by_hour) else 0.0
    day_threshold = float(rides_by_day.quantile(peak_quantile)) if len(rides_by_day) else 0.0

    peak_hours = rides_by_hour[(rides_by_hour >= hour_threshold) & (rides_by_hour >= min_count)]
    peak_days = rides_by_day[(rides_by_day >= day_threshold) & (rides_by_day >= min_count)]

    return PeakResult(
        peak_hours=peak_hours,
        peak_days=peak_days,
        hour_threshold=hour_threshold,
        day_threshold=day_threshold,
    )


def compute_category_kpis(df: pd.DataFrame) -> pd.DataFrame:
    """Compute category-level KPIs for Business vs Personal comparison."""

    required = {"CATEGORY", "MILES", "duration", "miles_per_min"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Missing columns for KPI computation: {missing}")

    kpis = (
        df.groupby("CATEGORY")
        .agg(
            rides=("CATEGORY", "size"),
            miles_total=("MILES", "sum"),
            miles_median=("MILES", "median"),
            duration_median_min=("duration", "median"),
            miles_per_min_median=("miles_per_min", "median"),
        )
        .sort_values("rides", ascending=False)
    )

    # Share-of-rides is useful for interpretation.
    kpis["ride_share"] = kpis["rides"] / kpis["rides"].sum()

    return kpis


def correlation_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a numeric-only frame suitable for correlation analysis."""

    # Note: `speed` is a deterministic transform of `miles_per_min` (mph = miles/min * 60),
    # so including both creates an uninformative perfect correlation.
    cols = [c for c in ["MILES", "duration", "miles_per_min", "start_hour"] if c in df.columns]

    return df[cols].replace([np.inf, -np.inf], np.nan)


def detect_anomalies(
    df: pd.DataFrame,
    *,
    duration_iqr_multiplier: float = 1.5,
    mpm_iqr_multiplier: float = 1.5,
    max_rows: int = 15,
) -> AnomalyResult:
    """Detect unusual trips using IQR bounds on duration and miles/min.

    Returns the top suspicious records for quick inspection.
    """

    if "duration" not in df.columns or "miles_per_min" not in df.columns:
        raise ValueError("DataFrame must include `duration` and `miles_per_min`.")

    work = df.copy()

    duration = work["duration"].dropna()
    mpm = work["miles_per_min"].dropna().replace([np.inf, -np.inf], np.nan).dropna()

    def iqr_bounds(series: pd.Series, mult: float) -> Tuple[float, float]:
        q1, q3 = series.quantile([0.25, 0.75])
        iqr = q3 - q1
        low = float(q1 - mult * iqr)
        high = float(q3 + mult * iqr)
        return low, high

    d_low, d_high = iqr_bounds(duration, duration_iqr_multiplier)
    m_low, m_high = iqr_bounds(mpm, mpm_iqr_multiplier)

    long_trips = work[(work["duration"] > d_high)].sort_values("duration", ascending=False).head(max_rows)
    short_trips = work[(work["duration"] < max(d_low, 0))].sort_values("duration", ascending=True).head(max_rows)

    very_fast = work[(work["miles_per_min"] > m_high)].sort_values("miles_per_min", ascending=False).head(max_rows)
    very_slow = work[(work["miles_per_min"] < max(m_low, 0))].sort_values("miles_per_min", ascending=True).head(max_rows)

    return AnomalyResult(
        long_trips=long_trips,
        short_trips=short_trips,
        very_fast=very_fast,
        very_slow=very_slow,
        duration_bounds=(d_low, d_high),
        mpm_bounds=(m_low, m_high),
    )


def monthly_weekly_trends(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute monthly and weekly trend tables for rides and miles."""

    if "start_dt" not in df.columns:
        raise ValueError("DataFrame must include `start_dt`.")

    work = df.dropna(subset=["start_dt"]).copy()

    monthly = (
        work.set_index("start_dt")
        .resample("MS")
        .agg(rides=("MILES", "size"), miles_total=("MILES", "sum"), duration_total_min=("duration", "sum"))
    )

    weekly = (
        work.set_index("start_dt")
        .resample("W-MON")
        .agg(rides=("MILES", "size"), miles_total=("MILES", "sum"), duration_total_min=("duration", "sum"))
    )

    return monthly, weekly
