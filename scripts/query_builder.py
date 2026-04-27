"""Build PMDA search queries from Mix Online RSS titles (v1 rules)."""

from __future__ import annotations

import re
import unicodedata

RELEASE_MARKERS = (
    "を発売",
    "を国内で発売",
    "を国内販売",
    "を国内発売",
)


def nfkc(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def segment_before_release(title: str) -> str:
    t = title or ""
    for m in RELEASE_MARKERS:
        if m in t:
            return t.split(m, 1)[0].strip()
    return t.strip()


def query_pass1(title: str) -> str:
    """第1回: 発売キーワード手前までを NFKC。v1 では会社名除去は最小（未実施に近い）。"""
    return nfkc(segment_before_release(title))


def query_pass2(title: str) -> str | None:
    """第2回: 最後の「、」の直後〜発売手前。第1と同一なら None（再検索スキップ）。"""
    base = segment_before_release(title)
    if "、" not in base:
        return None
    idx = base.rindex("、")
    sub = base[idx + 1 :].strip()
    sub = nfkc(sub)
    if not sub:
        return None
    q1 = nfkc(base)
    if sub == q1:
        return None
    return sub
