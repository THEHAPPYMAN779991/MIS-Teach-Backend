"""Read-only access to VectorRAG/GraphRAG experiment artifacts."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


REPORT_ROOT = Path(__file__).resolve().parents[1] / "logs" / "rag_comparison"
_RUN_ID = re.compile(r"^[0-9_]+$")


def _run_dirs() -> List[Path]:
    if not REPORT_ROOT.exists():
        return []
    return sorted(
        (path for path in REPORT_ROOT.iterdir() if path.is_dir() and _RUN_ID.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )


def resolve_run_id(run_id: str) -> Optional[str]:
    if run_id == "latest":
        directories = _run_dirs()
        return directories[0].name if directories else None
    return run_id if _RUN_ID.fullmatch(run_id or "") else None


def list_reports(limit: int = 20) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    for directory in _run_dirs()[:limit]:
        manifest_path = directory / "manifest.json"
        summary_path = directory / "summary.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {"run_id": directory.name}
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = None
        reports.append({"run_id": directory.name, "manifest": manifest, "summary": summary})
    return reports


def read_report_markdown(run_id: str = "latest") -> Optional[str]:
    resolved = resolve_run_id(run_id)
    if not resolved:
        return None
    path = REPORT_ROOT / resolved / "report.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_report_json(run_id: str = "latest") -> Optional[Dict[str, Any]]:
    resolved = resolve_run_id(run_id)
    if not resolved:
        return None
    directory = REPORT_ROOT / resolved
    result: Dict[str, Any] = {"run_id": resolved}
    for key, filename in (
        ("manifest", "manifest.json"),
        ("validation", "dataset_validation.json"),
        ("summary", "summary.json"),
    ):
        path = directory / filename
        if path.exists():
            result[key] = json.loads(path.read_text(encoding="utf-8"))
    return result
