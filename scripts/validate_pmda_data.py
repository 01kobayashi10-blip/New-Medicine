#!/usr/bin/env python3
"""Validate data/pmda_overrides.json and data/pmda_multi_candidates.json (no JSON Schema; v1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_PATH = ROOT / "data" / "pmda_overrides.json"
MULTI_PATH = ROOT / "data" / "pmda_multi_candidates.json"


def _fail(msg: str) -> None:
    print(f"validate_pmda_data: {msg}", file=sys.stderr)


def _is_https_url(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    p = urlparse(s)
    return p.scheme == "https" and bool(p.netloc)


def validate_overrides(path: Path) -> bool:
    if not path.is_file():
        _fail(f"missing {path.relative_to(ROOT)}")
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail(f"{path.name}: invalid JSON ({e})")
        return False
    if data.get("version") != 1:
        _fail(f"{path.name}: version must be 1")
        return False
    ov = data.get("overrides")
    if not isinstance(ov, dict):
        _fail(f"{path.name}: overrides must be an object")
        return False
    for k, v in ov.items():
        if not isinstance(k, str) or not k.strip():
            _fail(f"{path.name}: override keys must be non-empty strings")
            return False
        if not isinstance(v, dict):
            _fail(f"{path.name}: override value for {k!r} must be an object")
            return False
        url = v.get("pmda_package_url")
        if not isinstance(url, str) or not _is_https_url(url):
            _fail(f"{path.name}: overrides[{k!r}].pmda_package_url must be https URL")
            return False
        for opt in ("yj_code", "note", "locked_at"):
            if opt in v and v[opt] is not None and not isinstance(v[opt], str):
                _fail(f"{path.name}: overrides[{k!r}].{opt} must be string or null")
                return False
    return True


def validate_multi(path: Path) -> bool:
    if not path.is_file():
        _fail(f"missing {path.relative_to(ROOT)}")
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail(f"{path.name}: invalid JSON ({e})")
        return False
    if data.get("version") != 1:
        _fail(f"{path.name}: version must be 1")
        return False
    items = data.get("items")
    if not isinstance(items, list):
        _fail(f"{path.name}: items must be a list")
        return False
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            _fail(f"{path.name}: items[{i}] must be an object")
            return False
        for key in ("stable_id", "rss_title", "rss_link", "query_pass1", "candidate_count"):
            if key not in it:
                _fail(f"{path.name}: items[{i}] missing {key!r}")
                return False
        if "query_pass2" in it and it["query_pass2"] is not None and not isinstance(
            it["query_pass2"], str
        ):
            _fail(f"{path.name}: items[{i}].query_pass2 must be string or null/absent")
            return False
        if not isinstance(it["stable_id"], str) or not it["stable_id"].strip():
            _fail(f"{path.name}: items[{i}].stable_id invalid")
            return False
        if not isinstance(it["candidate_count"], int) or it["candidate_count"] < 0:
            _fail(f"{path.name}: items[{i}].candidate_count invalid")
            return False
        cands = it.get("candidates")
        if not isinstance(cands, list):
            _fail(f"{path.name}: items[{i}].candidates must be a list")
            return False
        for j, c in enumerate(cands):
            if not isinstance(c, dict):
                _fail(f"{path.name}: items[{i}].candidates[{j}] must be object")
                return False
            lab = c.get("label")
            du = c.get("detail_url")
            if not isinstance(lab, str):
                _fail(f"{path.name}: items[{i}].candidates[{j}].label must be string")
                return False
            if not isinstance(du, str) or not _is_https_url(du):
                _fail(f"{path.name}: items[{i}].candidates[{j}].detail_url must be https URL")
                return False
    return True


def main() -> int:
    ok = validate_overrides(OVERRIDES_PATH) and validate_multi(MULTI_PATH)
    if ok:
        print("OK: pmda_overrides.json and pmda_multi_candidates.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
