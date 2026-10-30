"""Write datasets to a local folder or S3 using cloudpathlib.

Layout (Athena/Glue friendly, partitioned by snapshot date):
    <output>/<dataset>/snapshot_date=YYYY-MM-DD/<dataset>.jsonl
    <output>/<dataset>/snapshot_date=YYYY-MM-DD/<dataset>.csv
    <output>/_runs/<run_id>.json        run manifest (counts, errors)
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable

from cloudpathlib import AnyPath, S3Client


def configure_s3(profile: str | None = None, endpoint_url: str | None = None) -> None:
    """Make s3:// paths use a named AWS profile (otherwise the default AWS credential chain)."""
    if profile or endpoint_url:
        S3Client(profile_name=profile, endpoint_url=endpoint_url).set_as_default_client()


def flatten(row: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, name + "."))
        elif isinstance(value, list):
            out[name] = json.dumps(value, ensure_ascii=False)
        else:
            out[name] = value
    return out


def to_csv(rows: Iterable[dict]) -> str:
    flat = [flatten(r) for r in rows]
    columns: list[str] = []
    seen: set[str] = set()
    for r in flat:
        for k in r:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(flat)
    return buf.getvalue()


def to_jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)


def write_dataset(output: str, dataset: str, snapshot_date: str, rows: list[dict], formats=("jsonl", "csv")) -> list[str]:
    folder = AnyPath(output) / dataset / f"snapshot_date={snapshot_date}"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        target = folder / f"{dataset}.{fmt}"
        target.write_text(to_jsonl(rows) if fmt == "jsonl" else to_csv(rows), encoding="utf-8")
        written.append(str(target))
    return written


def write_manifest(output: str, run_id: str, manifest: dict) -> str:
    folder = AnyPath(output) / "_runs"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{run_id}.json"
    target.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return str(target)
