"""RSS タイトル ↔ PMDA 候補の強一致判定（計画 B）。"""

from __future__ import annotations

import re
import unicodedata

from pmda_search import PmdaCandidate
from query_builder import nfkc


def _dosage_prefix(name: str) -> str:
    """「錠」「散」等の手前まで（簡易）。"""
    name = name or ""
    m = re.search(r"(錠|散|注射液|軟膏|ゲル|カプセル|OD錠|口腔内崩壊錠)", name)
    if m:
        return name[: m.start()].strip()
    return name.strip()


def strong_match_b(query: str, candidate_label: str) -> bool:
    """強一致 B: 完全一致 / 前方一致 / 剤形手前一致。"""
    q = nfkc(query)
    lab = nfkc(candidate_label)
    if not q or not lab:
        return False
    if q == lab:
        return True
    if lab.startswith(q):
        return True
    q_pre = _dosage_prefix(q)
    lab_pre = _dosage_prefix(lab)
    if q_pre and lab_pre and q_pre == lab_pre:
        return True
    return False


def pick_if_single_strong(
    query_used: str,
    candidates: list[PmdaCandidate],
) -> PmdaCandidate | None:
    if len(candidates) != 1:
        return None
    c = candidates[0]
    if strong_match_b(query_used, c.label):
        return c
    return None
