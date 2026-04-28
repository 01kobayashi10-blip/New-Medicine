"""GeneralList HTML から添付文書 PDF URL を解決し、テキスト化して章抜粋する（v1）。"""

from __future__ import annotations

import html as html_module
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

import pmda_search
import query_builder

ALLOWED_FETCH_HOSTS = frozenset({"www.pmda.go.jp", "www.info.pmda.go.jp"})

_RE_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.DOTALL)
_RE_PDF_LINK = re.compile(
    r"""<a\s[^>]*href\s*=\s*["']([^"']*ResultDataSetPDF[^"']*)["'][^>]*>([^<]*)</a>""",
    re.I | re.DOTALL,
)
_RE_PACK_LINK = re.compile(
    r"""href\s*=\s*["']([^"']*info\.pmda\.go\.jp/go/pack/[^"']*)["']""",
    re.I,
)
_RE_PDF_DATE = re.compile(r"PDF\s*\(\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*\)")


def _nfkc(s: str) -> str:
    return query_builder.nfkc(s or "")


def if_fetch_disabled() -> bool:
    return os.environ.get("PMDA_IF_FETCH_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def max_pdf_bytes() -> int:
    raw = os.environ.get("PMDA_IF_MAX_PDF_BYTES", "31457280").strip()
    try:
        return max(1_000_000, int(raw))
    except ValueError:
        return 31_457_280


def max_section_chars() -> int:
    raw = os.environ.get("PMDA_IF_MAX_SECTION_CHARS", "6000").strip()
    try:
        return max(500, int(raw))
    except ValueError:
        return 6000


def is_general_list_url(url: str) -> bool:
    u = (url or "").strip()
    return "pmda.go.jp" in u and "/iyakuDetail/GeneralList/" in u


def _allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.netloc:
        return False
    host = p.netloc.lower()
    if host.startswith("www."):
        pass
    return host in ALLOWED_FETCH_HOSTS


def _fetch_text(url: str, *, timeout: int) -> str:
    if not _allowed_url(url):
        raise ValueError(f"fetch blocked (host): {url!r}")
    pmda_search.throttle_pmda_http()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": pmda_search.USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _fetch_bytes(url: str, *, timeout: int, max_bytes: int) -> bytes:
    if not _allowed_url(url):
        raise ValueError(f"fetch blocked (host): {url!r}")
    pmda_search.throttle_pmda_http()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": pmda_search.USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cl = resp.headers.get("Content-Length")
        if cl:
            try:
                if int(cl) > max_bytes:
                    raise ValueError(f"PDF too large: Content-Length={cl}")
            except ValueError:
                pass
        out = bytearray()
        while len(out) < max_bytes:
            chunk = resp.read(min(65536, max_bytes - len(out)))
            if not chunk:
                break
            out.extend(chunk)
        if len(out) >= max_bytes:
            raise ValueError("PDF read truncated at max_bytes (incomplete file)")
        return bytes(out)


def _abs_url(base: str, href: str) -> str:
    h = html_module.unescape((href or "").strip())
    return urllib.parse.urljoin(base, h)


def _parse_pdf_date(anchor_text: str) -> tuple[int, int, int] | None:
    m = _RE_PDF_DATE.search(anchor_text or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return (y, mo, d)


def extract_result_dataset_pdf_pairs(html: str, base_url: str) -> list[tuple[str, tuple[int, int, int] | None, str]]:
    """各行相当の `<tr>` ごとに (絶対PDF URL, 日付タプル or None, tr の平文抜粋) を列挙。"""
    out: list[tuple[str, tuple[int, int, int] | None, str]] = []
    for tr_m in _RE_TR.finditer(html or ""):
        tr_html = tr_m.group(1)
        if "ResultDataSetPDF" not in tr_html:
            continue
        plain = re.sub(r"<[^>]+>", " ", tr_html)
        plain = _nfkc(re.sub(r"\s+", " ", plain))[:400]
        for lm in _RE_PDF_LINK.finditer(tr_html):
            href, anchor = lm.group(1), lm.group(2)
            abs_u = _abs_url(base_url, href)
            out.append((abs_u, _parse_pdf_date(anchor), plain))
    return out


def pick_pdf_url(
    pairs: list[tuple[str, tuple[int, int, int] | None, str]],
    rss_title: str,
) -> str | None:
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][0]
    q = _nfkc(query_builder.query_pass1(rss_title))
    q_alt = _nfkc(query_builder.query_pass3_middle_dot(rss_title) or "")
    scored: list[tuple[int, tuple[int, int, int] | None, str]] = []
    for url, dt, plain in pairs:
        score = 0
        if q and q in plain:
            score += 100 + len(q)
        if q_alt and q_alt in plain:
            score += 80 + len(q_alt)
        if not score and q:
            for part in re.split(r"[／/・\s]+", q):
                if len(part) >= 2 and part in plain:
                    score += 10
        scored.append((score, dt, url))
    def _date_key(dt: tuple[int, int, int] | None) -> int:
        if not dt:
            return 0
        y, mo, d = dt
        return y * 10000 + mo * 100 + d

    scored.sort(key=lambda x: (-x[0], -_date_key(x[1]), x[2]))
    best_score = scored[0][0]
    if best_score == 0:
        dated = [(dt, u) for _, dt, u in scored if dt]
        if dated:
            dated.sort(key=lambda x: _date_key(x[0]), reverse=True)
            return dated[0][1]
        return pairs[0][0]
    best_urls = [x[2] for x in scored if x[0] == best_score]
    if len(best_urls) == 1:
        return best_urls[0]
    dated = [(dt, u) for sc, dt, u in scored if sc == best_score and dt]
    if dated:
        dated.sort(key=lambda x: _date_key(x[0]), reverse=True)
        return dated[0][1]
    return best_urls[0]


def pdf_bytes_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text()
        except Exception:
            t = ""
        if t:
            parts.append(t)
    return "\n".join(parts)


def normalize_if_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _heading_digit_normalize_line(line: str) -> str:
    """行頭の章番号を半角に寄せる（PDF 抽出で全角番号になりがち）。"""
    m = re.match(r"^(\s*)([０-９]+)([\.．])", line)
    if not m:
        return line
    indent, num, dot = m.group(1), m.group(2), m.group(3)
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return indent + num.translate(trans) + dot + line[m.end() :]


def normalize_if_headings(text: str) -> str:
    lines = []
    for ln in (text or "").split("\n"):
        lines.append(_heading_digit_normalize_line(ln))
    return "\n".join(lines)


def _slice_between(text: str, start_pat: re.Pattern, end_pat: re.Pattern | None) -> str:
    m0 = start_pat.search(text)
    if not m0:
        return ""
    start = m0.start()
    if end_pat:
        m1 = end_pat.search(text, pos=m0.end())
        if m1:
            return text[start : m1.start()].strip()
        return text[start:].strip()
    return text[start:].strip()


def split_if_sections(text: str, max_len: int) -> dict[str, str]:
    t = normalize_if_headings(normalize_if_text(text))
    # 新記載要領: 「4. 効能又は効果」… 行頭想定
    p4 = re.compile(r"(?m)^\s*4[\.．]\s*効能又は効果")
    p5 = re.compile(r"(?m)^\s*5[\.．]\s*効能又は効果に関連する注意")
    p6 = re.compile(r"(?m)^\s*6[\.．]\s*用法及び用量")
    p8 = re.compile(r"(?m)^\s*8[\.．]\s*重要な基本的注意")
    p10 = re.compile(r"(?m)^\s*10[\.．]\s*相互作用")
    p11 = re.compile(r"(?m)^\s*11[\.．]\s*副作用")
    p17 = re.compile(r"(?m)^\s*17[\.．]\s*臨床成績")
    p18 = re.compile(r"(?m)^\s*18[\.．]\s*薬効薬理")
    p19 = re.compile(r"(?m)^\s*19[\.．]\s*有効成分に関する理化学的知見")

    sec4 = _slice_between(t, p4, p5)
    sec17 = _slice_between(t, p17, p18)
    sec18 = _slice_between(t, p18, p19)
    p12 = re.compile(r"(?m)^\s*12[\.．]\s*臨床検査結果に及ぼす影響")
    sec11 = _slice_between(t, p11, p12)
    block6 = _slice_between(t, p6, p8)
    block10 = _slice_between(t, p10, p11)
    sec6710 = "\n\n".join(x for x in (block6, block10) if x).strip()

    m4 = p4.search(t)
    ident = (t[: m4.start()].strip() if m4 else "")[: max_len * 2]

    def clip(s: str) -> str:
        s = (s or "").strip()
        if len(s) <= max_len:
            return s
        return s[: max_len].rstrip() + "\n…（以下省略）"

    return {
        "section_ident": clip(ident),
        "section_4": clip(sec4),
        "section_17": clip(sec17),
        "section_18": clip(sec18),
        "section_11": clip(sec11),
        "section_6710": clip(sec6710),
    }


@dataclass
class ExtractOutcome:
    sections: dict[str, str]
    pdf_url: str
    note: str


def extract_from_general_list(
    general_list_url: str,
    rss_title: str,
    *,
    timeout: int = 60,
) -> tuple[ExtractOutcome | None, str]:
    """GeneralList を取得し PDF を落として章抜粋する。失敗時 (None, reason)。"""
    if if_fetch_disabled():
        return None, "PMDA_IF_FETCH_DISABLED"
    if not is_general_list_url(general_list_url):
        return None, "not_general_list_url"
    try:
        html = _fetch_text(general_list_url.strip(), timeout=timeout)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return None, f"general_list_fetch:{e!s}"

    pairs = extract_result_dataset_pdf_pairs(html, general_list_url.strip())
    pdf_url = pick_pdf_url(pairs, rss_title)
    if not pdf_url:
        return None, "no_ResultDataSetPDF_link"

    try:
        pdf_bytes = _fetch_bytes(pdf_url, timeout=timeout, max_bytes=max_pdf_bytes())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return None, f"pdf_fetch:{e!s}"

    try:
        raw_text = pdf_bytes_to_text(pdf_bytes)
    except Exception as e:
        return None, f"pdf_parse:{e!s}"

    sections = split_if_sections(raw_text, max_section_chars())
    note = ""
    if not any(sections.get(k) for k in ("section_4", "section_17", "section_18", "section_11")):
        note = "sections_empty_maybe_layout"
    reason = "ok" if not note else f"ok:{note}"
    return ExtractOutcome(sections=sections, pdf_url=pdf_url, note=note), reason


def first_pack_url_in_html(html: str, base_url: str) -> str | None:
    m = _RE_PACK_LINK.search(html or "")
    if not m:
        return None
    return _abs_url(base_url, m.group(1))
