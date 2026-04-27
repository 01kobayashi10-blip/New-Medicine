#!/usr/bin/env python3
"""Read reports/generate_queue.json and emit reports/infographic_<hash>.html (v1 skeleton)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

import pmda_search
import query_builder
import rss_pmda_resolve
from jinja2 import Environment, FileSystemLoader, select_autoescape

QUEUE_PATH = ROOT / "reports" / "generate_queue.json"
OVERRIDES_PATH = ROOT / "data" / "pmda_overrides.json"
MULTI_PATH = ROOT / "data" / "pmda_multi_candidates.json"
REPORTS = ROOT / "reports"
TEMPLATE_DIR = ROOT / "templates"


def load_json(path: Path, default):
    if not path.is_file():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def stable_slug(stable_id: str) -> str:
    return hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]


def load_overrides_raw() -> dict:
    return load_json(OVERRIDES_PATH, {"version": 1, "updated_at": None, "overrides": {}})


def override_map(data: dict) -> dict:
    return data.get("overrides") or {}


def prune_multi_for_resolved(ov: dict) -> None:
    """override に載った stable_id は多件キューから削除。"""
    if not MULTI_PATH.is_file():
        return
    data = load_json(MULTI_PATH, {"version": 1, "items": []})
    items = [x for x in data.get("items") or [] if isinstance(x, dict) and x.get("stable_id") not in ov]
    if len(items) != len(data.get("items") or []):
        data["items"] = items
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(MULTI_PATH, data)


def upsert_multi(items_list: list, new_block: dict) -> list:
    by = {x["stable_id"]: x for x in items_list if isinstance(x, dict) and x.get("stable_id")}
    sid = new_block["stable_id"]
    by[sid] = new_block
    return list(by.values())


def render_html(
    *,
    title_display: str,
    stable_id: str,
    rss_link: str,
    pmda_url: str,
    disclaimer: str,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tmpl = env.get_template("infographic_v1.html.j2")
    return tmpl.render(
        title_display=title_display,
        stable_id=stable_id,
        rss_link=rss_link,
        pmda_url=pmda_url,
        disclaimer=disclaimer,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        section_ident="",
        section_4="",
        section_18="",
        section_17="",
        section_11="",
        section_6710="",
    )


def process_item(item: dict, overrides: dict) -> tuple[str | None, str]:
    """Returns (relative path reports/foo.html or None, log reason)."""
    sid = item["stable_id"]
    title = item.get("title") or ""
    link = item.get("link") or ""
    slug = stable_slug(sid)
    out_name = f"infographic_{slug}.html"
    out_path = REPORTS / out_name

    q1 = query_builder.query_pass1(title)
    q2 = query_builder.query_pass2(title)

    if sid in overrides:
        pmda_url = (overrides[sid].get("pmda_package_url") or "").strip()
        disc = (
            "override により PMDA URL が指定されています。章別の自動抜粋は次フェーズで追加予定です。"
        )
        html = render_html(
            title_display=title[:200] + ("…" if len(title) > 200 else ""),
            stable_id=sid,
            rss_link=link,
            pmda_url=pmda_url,
            disclaimer=disc,
        )
        out_path.write_text(html, encoding="utf-8")
        return f"reports/{out_name}", "override"

    c1 = pmda_search.search_candidates(q1)
    query_used = q1
    candidates = c1
    if len(candidates) == 0 and q2:
        candidates = pmda_search.search_candidates(q2)
        query_used = q2 or q1

    if len(candidates) > 1:
        multi = load_json(MULTI_PATH, {"version": 1, "updated_at": None, "items": []})
        multi["version"] = 1
        multi["updated_at"] = datetime.now(timezone.utc).isoformat()
        block = {
            "stable_id": sid,
            "rss_title": title,
            "rss_link": link,
            "query_pass1": q1,
            "query_pass2": q2 if q2 else "",
            "candidate_count": len(candidates),
            "candidates": [
                {"label": c.label, "detail_url": c.detail_url} for c in candidates
            ],
        }
        multi["items"] = upsert_multi(multi.get("items") or [], block)
        save_json(MULTI_PATH, multi)
        return None, "multi"

    pmda_url = ""
    disc = (
        "PMDA 検索で候補は得られませんでした。`data/pmda_overrides.json` に stable_id を追加するか、"
        "RSS タイトル（発売手前・第2クエリ）を見直してください。"
    )
    picked = rss_pmda_resolve.pick_if_single_strong(query_used, candidates)
    if picked:
        pmda_url = picked.detail_url
        disc = (
            "候補 1 件・強一致。章別の自動抜粋は次フェーズで追加予定です。"
        )
    elif len(candidates) == 1:
        disc = "候補 1 件ですが強一致条件を満たさないため、PMDA URL は未設定です。override または検索クエリの調整が必要です。"

    html = render_html(
        title_display=title[:200] + ("…" if len(title) > 200 else ""),
        stable_id=sid,
        rss_link=link,
        pmda_url=pmda_url,
        disclaimer=disc,
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return f"reports/{out_name}", "skeleton"


def append_github_output(**pairs: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            v = v.replace("\n", "%0A") if v else ""
            f.write(f"{k}={v}\n")


def main() -> int:
    ov_data = load_overrides_raw()
    overrides = override_map(ov_data)
    prune_multi_for_resolved(overrides)

    if not QUEUE_PATH.is_file():
        print("No generate_queue.json; skip.")
        append_github_output(infographic_primary="", infographic_paths="", infographic_url="")
        return 0
    queue = load_json(QUEUE_PATH, {"items": []})
    items = queue.get("items") if isinstance(queue, dict) else []
    if not items:
        print("Empty generate queue; skip.")
        append_github_output(infographic_primary="", infographic_paths="", infographic_url="")
        return 0

    written: list[str] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("stable_id"):
            continue
        rel, reason = process_item(it, overrides)
        if rel:
            written.append(rel)
            print(f"Wrote {rel} ({reason})")
        else:
            print(f"Skipped {it.get('stable_id')}: {reason}")

    primary = written[0] if written else ""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    ref = os.environ.get("GITHUB_REF_NAME", "").strip()
    preview_url = ""
    if primary and repo and ref:
        raw = f"https://raw.githubusercontent.com/{repo}/{ref}/{primary}"
        preview_url = f"https://htmlpreview.github.io/?{raw}"
    append_github_output(
        infographic_primary=primary,
        infographic_paths=",".join(written),
        infographic_url=preview_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
