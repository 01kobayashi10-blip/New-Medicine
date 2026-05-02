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

import pmda_if_extract
import pmda_search
import query_builder
import rss_pmda_resolve
from jinja2 import Environment, FileSystemLoader, select_autoescape

QUEUE_PATH = ROOT / "reports" / "generate_queue.json"
PREVIEW_MANIFEST_PATH = ROOT / "reports" / "infographic_preview_manifest.json"
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
    source_pdf_url: str = "",
    section_ident: str = "",
    section_4: str = "",
    section_18: str = "",
    section_17: str = "",
    section_11: str = "",
    section_6710: str = "",
    section_6710_plain: str = "",
    card_brand: str = "",
    card_generic: str = "",
    card_yakka: str = "",
    card_efficacy: str = "",
    ident_preview: str = "",
    pharma18: dict | None = None,
    pharma17: dict | None = None,
    pharma11: dict | None = None,
    pharma6710: dict | None = None,
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
        source_pdf_url=source_pdf_url,
        disclaimer=disclaimer,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        section_ident=section_ident,
        section_4=section_4,
        section_18=section_18,
        section_17=section_17,
        section_11=section_11,
        section_6710=section_6710,
        section_6710_plain=section_6710_plain,
        card_brand=card_brand,
        card_generic=card_generic,
        card_yakka=card_yakka,
        card_efficacy=card_efficacy,
        ident_preview=ident_preview,
        pharma18=pharma18,
        pharma17=pharma17,
        pharma11=pharma11,
        pharma6710=pharma6710,
    )


def _empty_sections() -> dict[str, str]:
    return {
        "pre_ch4_raw": "",
        "section_ident": "",
        "section_3": "",
        "section_4": "",
        "section_17": "",
        "section_18": "",
        "section_11": "",
        "section_6710": "",
    }


def _cards_for_title(title: str, sec: dict[str, str]) -> dict[str, str]:
    return pmda_if_extract.summarize_infographic_cards(rss_title=title, sections=sec)


def _section_6710_plain_for_template(raw: str, pharma6710: dict | None) -> str:
    """箇条書き表示時は未使用。フォールバック時は改行結合済みテキストを返す。"""
    s = raw or ""
    if pharma6710 and pharma6710.get("bullets"):
        return s
    fb = pmda_if_extract.format_section_6710_fallback(s)
    return fb if fb.strip() else s


def _http_timeout() -> int:
    try:
        return max(10, int(os.environ.get("PMDA_IF_HTTP_TIMEOUT", "60")))
    except ValueError:
        return 60


def _fill_from_general_list(
    pmda_url: str,
    rss_title: str,
) -> tuple[dict[str, str], str, str]:
    """GeneralList から PDF 抜粋。(sections, source_pdf_url, log_reason)。"""
    empty = _empty_sections()
    if not pmda_url.strip():
        return empty, "", "no_pmda_url"
    if not pmda_if_extract.is_general_list_url(pmda_url):
        return empty, "", "skip_not_general_list"
    out, reason = pmda_if_extract.extract_from_general_list(
        pmda_url.strip(),
        rss_title,
        timeout=_http_timeout(),
    )
    if not out:
        return empty, "", reason
    return out.sections, out.pdf_url, reason


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
        sec, src_pdf, ex_reason = _fill_from_general_list(pmda_url, title)
        if src_pdf:
            disc = (
                "override により指定された GeneralList から添付文書 PDF を取得し、"
                "章別に自動抜粋しました（文字数上限で切り詰めています）。"
                "診療・服薬の判断は必ず最新の添付文書および医療者の指示に従ってください。"
            )
            if "sections_empty" in ex_reason:
                disc += " 章見出しの自動検出が弱い可能性があります（PDF レイアウトによる）。"
        elif pmda_if_extract.is_general_list_url(pmda_url):
            disc = (
                "override の PMDA URL は GeneralList ですが、添付文書の自動取得に失敗しました（"
                f"{ex_reason}）。手動で添付文書を確認するか、URL を修正してください。"
            )
        else:
            disc = (
                "override により PMDA URL が指定されています。この URL は GeneralList 以外のため、"
                "v1 では添付文書の自動抜粋を行っていません（GeneralList の URL を指定すると抜粋されます）。"
            )
        cards = _cards_for_title(title, sec)
        pharma18 = pmda_if_extract.structure_section18_moa(sec.get("section_18") or "")
        pharma17 = pmda_if_extract.structure_section17_trials(sec.get("section_17") or "")
        pharma11 = pmda_if_extract.structure_section11_summary(sec.get("section_11") or "")
        pharma6710 = pmda_if_extract.structure_dosage_memo(sec.get("section_6710") or "")
        s6710 = sec.get("section_6710") or ""
        html = render_html(
            title_display=title[:200] + ("…" if len(title) > 200 else ""),
            stable_id=sid,
            rss_link=link,
            pmda_url=pmda_url,
            disclaimer=disc,
            source_pdf_url=src_pdf,
            section_ident=sec["section_ident"],
            section_4=sec["section_4"],
            section_17=sec["section_17"],
            section_18=sec["section_18"],
            section_11=sec["section_11"],
            section_6710=s6710,
            section_6710_plain=_section_6710_plain_for_template(s6710, pharma6710),
            card_brand=cards["card_brand"],
            card_generic=cards["card_generic"],
            card_yakka=cards["card_yakka"],
            card_efficacy=cards["card_efficacy"],
            ident_preview=cards["ident_preview"],
            pharma18=pharma18,
            pharma17=pharma17,
            pharma11=pharma11,
            pharma6710=pharma6710,
        )
        out_path.write_text(html, encoding="utf-8")
        return f"reports/{out_name}", "override_if" if src_pdf else "override"

    c1 = pmda_search.search_candidates(q1)
    query_used = q1
    candidates = c1
    if len(candidates) == 0 and q2:
        candidates = pmda_search.search_candidates(q2)
        query_used = q2 or q1
    q3 = query_builder.query_pass3_middle_dot(title)
    if len(candidates) == 0 and q3:
        candidates = pmda_search.search_candidates(q3)
        query_used = q3

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
        "RSS タイトルを見直してください（発売手前全文・「、」以降・中黒「・」以降で再検索します）。"
    )
    picked = rss_pmda_resolve.pick_if_single_strong(query_used, candidates)
    sec = _empty_sections()
    src_pdf = ""
    ex_reason = ""
    if picked:
        pmda_url = picked.detail_url
        sec, src_pdf, ex_reason = _fill_from_general_list(pmda_url, title)
        if src_pdf:
            disc = (
                "候補 1 件・強一致。添付文書 PDF から章を自動抜粋しました（文字数上限あり）。"
                "診療・服薬の判断は必ず最新の添付文書および医療者の指示に従ってください。"
            )
            if "sections_empty" in ex_reason:
                disc += " 章見出しの自動検出が弱い可能性があります（PDF レイアウトによる）。"
        else:
            disc = (
                "候補 1 件・強一致。GeneralList は取得できましたが、添付文書 PDF の取得または"
                f"章抜粋に失敗しました（{ex_reason}）。リンク先で添付文書をご確認ください。"
            )
    elif len(candidates) == 1:
        disc = "候補 1 件ですが強一致条件を満たさないため、PMDA URL は未設定です。override または検索クエリの調整が必要です。"

    cards = _cards_for_title(title, sec)
    pharma18 = pmda_if_extract.structure_section18_moa(sec.get("section_18") or "")
    pharma17 = pmda_if_extract.structure_section17_trials(sec.get("section_17") or "")
    pharma11 = pmda_if_extract.structure_section11_summary(sec.get("section_11") or "")
    pharma6710 = pmda_if_extract.structure_dosage_memo(sec.get("section_6710") or "")
    s6710 = sec.get("section_6710") or ""
    html = render_html(
        title_display=title[:200] + ("…" if len(title) > 200 else ""),
        stable_id=sid,
        rss_link=link,
        pmda_url=pmda_url,
        disclaimer=disc,
        source_pdf_url=src_pdf,
        section_ident=sec["section_ident"],
        section_4=sec["section_4"],
        section_17=sec["section_17"],
        section_18=sec["section_18"],
        section_11=sec["section_11"],
        section_6710=s6710,
        section_6710_plain=_section_6710_plain_for_template(s6710, pharma6710),
        card_brand=cards["card_brand"],
        card_generic=cards["card_generic"],
        card_yakka=cards["card_yakka"],
        card_efficacy=cards["card_efficacy"],
        ident_preview=cards["ident_preview"],
        pharma18=pharma18,
        pharma17=pharma17,
        pharma11=pharma11,
        pharma6710=pharma6710,
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return f"reports/{out_name}", "extract" if src_pdf else "skeleton"


def preview_url_for_rel(repo: str, ref: str, rel: str) -> str:
    if not (repo and ref and rel):
        return ""
    raw = f"https://raw.githubusercontent.com/{repo}/{ref}/{rel}"
    return f"https://htmlpreview.github.io/?{raw}"


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
        PREVIEW_MANIFEST_PATH.unlink(missing_ok=True)
        append_github_output(infographic_primary="", infographic_paths="", infographic_url="")
        return 0
    queue = load_json(QUEUE_PATH, {"items": []})
    items = queue.get("items") if isinstance(queue, dict) else []
    if not items:
        print("Empty generate queue; skip.")
        PREVIEW_MANIFEST_PATH.unlink(missing_ok=True)
        append_github_output(infographic_primary="", infographic_paths="", infographic_url="")
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    ref = os.environ.get("GITHUB_REF_NAME", "").strip()
    written: list[str] = []
    manifest_rows: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("stable_id"):
            continue
        rel, reason = process_item(it, overrides)
        if rel:
            written.append(rel)
            title = str(it.get("title") or "").strip() or str(it.get("link") or "").strip()
            manifest_rows.append(
                {
                    "title": title,
                    "preview_url": preview_url_for_rel(repo, ref, rel),
                }
            )
            print(f"Wrote {rel} ({reason})")
        else:
            print(f"Skipped {it.get('stable_id')}: {reason}")

    primary = written[0] if written else ""
    preview_url = preview_url_for_rel(repo, ref, primary) if primary else ""
    save_json(
        PREVIEW_MANIFEST_PATH,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": manifest_rows,
        },
    )
    append_github_output(
        infographic_primary=primary,
        infographic_paths=",".join(written),
        infographic_url=preview_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
