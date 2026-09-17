"""Compact model-step outcomes recovered from ActivitySim checkpoints after a run."""

from __future__ import annotations

import filecmp
import fnmatch
import re
from pathlib import Path

import pandas as pd

NON_TABLE_COLUMNS = {"checkpoint_name", "timestamp"}
DEFAULT_CATEGORY_LIMIT = 100
AUTO_SEGMENTS = ("primary_purpose", "tour_type", "ptype", "purpose")
DIAGNOSTIC_TERMS = (
    "accessibility",
    "logsum",
    "prob",
    "utility",
    "time",
    "distance",
    "dist_",
    "cost",
    "shadow",
)


def excluded_component(name):
    """Initialization/coordinator/reporting steps do not represent model choices."""
    words = set(re.split(r"[^a-z0-9]+", name.lower()))
    return (
        bool(words & {"init", "initialize", "summarize"})
        or name.startswith("mp_")
        or name.startswith("write_")
    )


def value_label(value):
    if pd.isna(value):
        return "<null>"
    return str(value)


class PipelineStore:
    """Small read-only adapter for current parquet and legacy HDF pipelines."""

    def __init__(self, path):
        self.path = Path(path)
        self.parquet = self.path.is_dir()
        self.hdf = None if self.parquet else pd.HDFStore(str(self.path), mode="r")

    def close(self):
        if self.hdf is not None:
            self.hdf.close()

    def table_path(self, table, checkpoint):
        path = self.path / table / f"{checkpoint}.parquet"
        return path if path.exists() else path.with_suffix(".pickle.gz")

    def read(self, table, checkpoint=None, columns=None):
        if self.parquet:
            if table == "checkpoints":
                path = self.path / "checkpoints.parquet"
            else:
                path = self.table_path(table, checkpoint)
            if path.suffix == ".parquet":
                return pd.read_parquet(path, columns=columns)
            frame = pd.read_pickle(path)
            return frame if columns is None else frame[columns]
        key = "/checkpoints" if table == "checkpoints" else f"/{table}/{checkpoint}"
        frame = self.hdf[key]
        return frame if columns is None else frame[columns]

    def columns(self, table, checkpoint):
        if self.parquet:
            import pyarrow.parquet as pq

            path = self.table_path(table, checkpoint)
            if path.suffix == ".parquet":
                return pq.read_schema(path).names
            return list(pd.read_pickle(path).columns)
        return list(self.hdf[f"/{table}/{checkpoint}"].columns)

    def rows(self, table, checkpoint):
        if self.parquet:
            import pyarrow.parquet as pq

            path = self.table_path(table, checkpoint)
            if path.suffix == ".parquet":
                return pq.ParquetFile(path).metadata.num_rows
            return len(pd.read_pickle(path))
        storer = self.hdf.get_storer(f"/{table}/{checkpoint}")
        return storer.nrows or len(self.hdf[f"/{table}/{checkpoint}"])

    def changed(self, table, checkpoint, previous):
        """Checkpointing can rewrite untouched worker inputs; ignore exact copies."""
        if not previous:
            return True
        if self.parquet:
            return not filecmp.cmp(
                self.table_path(table, checkpoint),
                self.table_path(table, previous),
                shallow=False,
            )
        return not self.hdf[f"/{table}/{checkpoint}"].equals(
            self.hdf[f"/{table}/{previous}"]
        )


def pipeline_paths(output):
    """Find the parent pipeline or detailed worker pipelines, never empty bases."""
    output = Path(output)
    parquet = sorted(output.glob("*pipeline.parquetpipeline"))
    hdf = sorted(
        path
        for path in output.glob("*pipeline")
        if path.is_file() and not path.name.startswith("final_")
    )
    return parquet + hdf


def previous_version(inventory, row_number, table):
    if row_number == 0 or table not in inventory:
        return None
    value = inventory.iloc[row_number - 1].get(table)
    return value if isinstance(value, str) and value else None


def normalized_component(name):
    return name.split(".", 1)[0].lower().removesuffix("_simulate")


def explicit_rule(profile, component):
    rules = profile.get("component_summaries", {})
    for pattern, rule in rules.items():
        if fnmatch.fnmatchcase(component, pattern):
            return rule
    return None


def inferred_outcomes(component, table, columns, new_columns):
    """Choose conventional choice columns, falling back to a small schema delta."""
    component = normalized_component(component)
    available = set(columns)
    candidates = []

    def add(*names):
        for name in names:
            if name in available and name not in candidates:
                candidates.append(name)

    add(component, component.removesuffix("_choice"))
    if "mode_choice" in component:
        add(component.replace("_choice", ""), "trip_mode", "tour_mode")
    if component.endswith("_scheduling"):
        add("depart" if table == "trips" else "tdd")
    if component.endswith("_purpose"):
        add("purpose")
    if "destination" in component:
        add("destination")
    if component == "parking_location":
        add("parking_zone")
    elif component.endswith("_location"):
        stem = component.removesuffix("_location")
        add(f"{stem}_zone_id", "destination")
    if component.startswith("cdap"):
        add("cdap_activity")
    if component == "vehicle_allocation":
        for column in columns:
            if column.startswith("vehicle_occup_"):
                add(column)
    if "school_escorting" in component:
        for column in columns:
            if column.startswith("school_escorting_"):
                add(column)
    if candidates:
        return candidates

    component_tokens = set(component.split("_")) - {"choice", "simulate", "model"}
    scored = []
    for column in columns:
        lower = column.lower()
        if any(term in lower for term in DIAGNOSTIC_TERMS):
            continue
        tokens = set(lower.split("_"))
        overlap = len(component_tokens & tokens)
        if overlap:
            scored.append((overlap / max(len(component_tokens), 1), overlap, column))
    eligible = [item for item in scored if item[0] >= 0.5 and item[1] >= 2]
    if eligible:
        best = max(score for score, _, _ in eligible)
        return sorted(column for score, _, column in eligible if score == best)[:3]

    fallback = [
        column
        for column in new_columns
        if not any(term in column.lower() for term in DIAGNOSTIC_TERMS)
        and not column.lower().endswith("_id")
    ]
    return fallback if len(fallback) == 1 else []


def inferred_segments(outcomes, columns):
    return [name for name in AUTO_SEGMENTS if name in columns and name not in outcomes][
        :1
    ]


def add_counts(target, series):
    target["count"] = target.get("count", 0) + int(series.notna().sum())
    target["nulls"] = target.get("nulls", 0) + int(series.isna().sum())
    counts = target.setdefault("_counts", {})
    for value, count in series.value_counts(dropna=False).items():
        if not count:
            continue
        key = value_label(value)
        counts[key] = counts.get(key, 0) + int(count)
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if len(numeric):
        stats = target.setdefault("_numeric", {"count": 0, "sum": 0.0})
        stats["count"] += len(numeric)
        stats["sum"] += float(numeric.sum())
        minimum, maximum = float(numeric.min()), float(numeric.max())
        stats["min"] = min(stats.get("min", minimum), minimum)
        stats["max"] = max(stats.get("max", maximum), maximum)


def add_frame(target, frame, outcomes, segments):
    target["rows"] = target.get("rows", 0) + len(frame)
    target["partitions"] = target.get("partitions", 0) + 1
    for outcome in outcomes:
        summary = target.setdefault("outcomes", {}).setdefault(outcome, {})
        add_counts(summary, frame[outcome])
        for segment in segments:
            segmented = summary.setdefault("segments", {}).setdefault(segment, {})
            for segment_value, group in frame.groupby(
                segment, dropna=False, observed=True
            ):
                partial = segmented.setdefault(value_label(segment_value), {})
                add_counts(partial, group[outcome])


def apply_filters(frame, filters):
    for column, accepted in filters.items():
        accepted = accepted if isinstance(accepted, list) else [accepted]
        frame = frame[frame[column].isin(accepted)]
    return frame


def finalize_counts(value, category_limit):
    if isinstance(value, dict):
        for child in list(value.values()):
            finalize_counts(child, category_limit)
        counts = value.pop("_counts", None)
        numeric = value.pop("_numeric", None)
        if counts is not None:
            value["distinct"] = len(counts) - ("<null>" in counts)
            if len(counts) <= category_limit:
                value["counts"] = dict(sorted(counts.items()))
            else:
                value["counts_omitted"] = (
                    f"{len(counts)} values exceeds category limit {category_limit}"
                )
        if numeric:
            total = numeric["sum"]
            value["numeric"] = {
                "min": numeric["min"],
                "max": numeric["max"],
                "sum": total,
                "mean": total / numeric["count"],
            }
    elif isinstance(value, list):
        for child in value:
            finalize_counts(child, category_limit)


def component_summary(output, profile=None):
    """Aggregate model outcomes across all serial or sliced pipeline stores."""
    profile = profile or {}
    category_limit = profile.get(
        "component_summary_category_limit", DEFAULT_CATEGORY_LIMIT
    )
    result = {
        "schema_version": 1,
        "category_limit": category_limit,
        "pipeline_stores": 0,
        "components": {},
    }
    for path in pipeline_paths(output):
        store = PipelineStore(path)
        try:
            inventory = store.read("checkpoints").fillna("")
            result["pipeline_stores"] += 1
            for row_number, (_, row) in enumerate(inventory.iterrows()):
                component = str(row["checkpoint_name"])
                if excluded_component(component):
                    continue
                rule = explicit_rule(profile, component)
                if rule is False:
                    continue
                for table in inventory.columns:
                    if table in NON_TABLE_COLUMNS or row[table] != component:
                        continue
                    if rule and rule.get("table") != table:
                        continue
                    previous = previous_version(inventory, row_number, table)
                    if rule is None and not store.changed(table, component, previous):
                        continue
                    columns = store.columns(table, component)
                    previous_columns = (
                        store.columns(table, previous) if previous else []
                    )
                    new_columns = [c for c in columns if c not in previous_columns]
                    if rule:
                        requested = (
                            list(rule.get("outcomes", []))
                            + list(rule.get("segments", []))
                            + list(rule.get("filters", {}))
                        )
                        missing = [name for name in requested if name not in columns]
                        if missing:
                            raise ValueError(
                                f"component summary {component!r} table {table!r} "
                                f"is missing columns {missing}"
                            )
                    outcomes = (
                        list(rule.get("outcomes", []))
                        if rule
                        else inferred_outcomes(component, table, columns, new_columns)
                    )
                    outcomes = [name for name in outcomes if name in columns]
                    segments = (
                        list(rule.get("segments", []))
                        if rule
                        else inferred_segments(outcomes, columns)
                    )
                    segments = [name for name in segments if name in columns]
                    filters = dict(rule.get("filters", {})) if rule else {}
                    filters = {
                        name: accepted
                        for name, accepted in filters.items()
                        if name in columns
                    }
                    component_result = result["components"].setdefault(
                        component, {"tables": {}}
                    )
                    table_result = component_result["tables"].setdefault(table, {})
                    if filters:
                        table_result["filters"] = filters
                    if outcomes:
                        frame = store.read(
                            table,
                            component,
                            columns=list(
                                dict.fromkeys(outcomes + segments + list(filters))
                            ),
                        )
                        frame = apply_filters(frame, filters)
                        add_frame(table_result, frame, outcomes, segments)
                    else:
                        table_result["rows"] = table_result.get("rows", 0) + store.rows(
                            table, component
                        )
                        table_result["partitions"] = (
                            table_result.get("partitions", 0) + 1
                        )
        finally:
            store.close()
    finalize_counts(result, category_limit)
    return result
