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


def _unglue_chapter_headings(t: str) -> str:
    """PDF 由来で「3.1 組成」が前行にくっついている場合に改行を補う。"""
    s = t or ""
    s = re.sub(r"(組成・性状)\s*(3[\.．]1)", r"\1\n\2", s)
    s = re.sub(r"(組成・性状)(3[\.．]1)", r"\1\n\2", s)
    s = re.sub(r"(組成)(3[\.．]1)", r"\1\n\2", s)
    return s


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


def _strip_leading_pdf_noise_lines(s: str) -> str:
    """PDF 先頭の頁番号・断片行などを落とす。"""
    lines = (s or "").split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "":
            i += 1
            continue
        if re.fullmatch(r"[0-9０-９]{1,6}", stripped):
            i += 1
            continue
        if len(stripped) <= 2 and re.fullmatch(r"[A-Za-z0-9０-９]+", stripped):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def _ident_before_chapter4(full_text: str, pos_ch4: int) -> str:
    """章4直前までのうち、可能なら「1. 警告」以降に寄せる。"""
    raw = (full_text or "")[:pos_ch4].strip()
    if not raw:
        return ""
    m1 = re.search(r"(?m)^\s*[1１][\.．]\s*警告", raw)
    if m1:
        return raw[m1.start() :].strip()
    return _strip_leading_pdf_noise_lines(raw)


def _strip_redundant_heading_line(body: str, line_pat: re.Pattern) -> str:
    """本文先頭が章見出しと同一なら 1 行削除（見出しは HTML 側で表示するため）。"""
    lines = (body or "").split("\n")
    if not lines:
        return body or ""
    first = lines[0].strip()
    if line_pat.match(first):
        return "\n".join(lines[1:]).strip()
    return (body or "").strip()


def split_if_sections(text: str, max_len: int) -> dict[str, str]:
    t = normalize_if_headings(normalize_if_text(text))
    t = _unglue_chapter_headings(t)
    # 新記載要領: 「4. 効能又は効果」… 行頭想定
    p3 = re.compile(r"(?m)^\s*3[\.．]\s*組成(?:・性状)?")
    p4 = re.compile(r"(?m)^\s*4[\.．]\s*効能又は効果")
    p5 = re.compile(r"(?m)^\s*5[\.．]\s*効能又は効果に関連する注意")
    p6 = re.compile(r"(?m)^\s*6[\.．]\s*用法及び用量")
    p8 = re.compile(r"(?m)^\s*8[\.．]\s*重要な基本的注意")
    p10 = re.compile(r"(?m)^\s*10[\.．]\s*相互作用")
    p11 = re.compile(r"(?m)^\s*11[\.．]\s*副作用")
    p17 = re.compile(r"(?m)^\s*17[\.．]\s*臨床成績")
    p18 = re.compile(r"(?m)^\s*18[\.．]\s*薬効薬理")
    p19 = re.compile(r"(?m)^\s*19[\.．]\s*有効成分に関する理化学的知見")

    sec3 = _slice_between(t, p3, p4)
    sec3 = _strip_redundant_heading_line(sec3, re.compile(r"^\s*3[\.．]\s*組成(?:・性状)?\s*$"))
    if not (sec3 or "").strip():
        m4a = p4.search(t)
        if m4a:
            prefix = t[: m4a.start()]
            m3f = re.compile(r"3[\.．]\s*組成(?:・性状)?").search(prefix)
            if m3f:
                sec3 = prefix[m3f.start() :].strip()
                sec3 = _strip_redundant_heading_line(
                    sec3, re.compile(r"^\s*3[\.．]\s*組成(?:・性状)?\s*$")
                )
    sec4 = _slice_between(t, p4, p5)
    sec4 = _strip_redundant_heading_line(sec4, re.compile(r"^\s*4[\.．]\s*効能又は効果\s*$"))
    sec17 = _slice_between(t, p17, p18)
    sec17 = _strip_redundant_heading_line(sec17, re.compile(r"^\s*17[\.．]\s*臨床成績\s*$"))
    sec18 = _slice_between(t, p18, p19)
    sec18 = _strip_redundant_heading_line(sec18, re.compile(r"^\s*18[\.．]\s*薬効薬理\s*$"))
    p12 = re.compile(r"(?m)^\s*12[\.．]\s*臨床検査結果に及ぼす影響")
    sec11 = _slice_between(t, p11, p12)
    sec11 = _strip_redundant_heading_line(sec11, re.compile(r"^\s*11[\.．]\s*副作用\s*$"))
    block6 = _slice_between(t, p6, p8)
    block6 = _strip_redundant_heading_line(block6, re.compile(r"^\s*6[\.．]\s*用法及び用量\s*$"))
    block10 = _slice_between(t, p10, p11)
    block10 = _strip_redundant_heading_line(block10, re.compile(r"^\s*10[\.．]\s*相互作用\s*$"))
    sec6710 = "\n\n".join(x for x in (block6, block10) if x).strip()

    m4 = p4.search(t)
    pre_ch4_raw = ""
    if m4:
        pre_ch4_raw = t[: m4.start()].strip()
        ident = _ident_before_chapter4(t, m4.start())
    else:
        ident = ""

    def clip(s: str, *, limit: int | None = None) -> str:
        lim = max_len if limit is None else limit
        s = (s or "").strip()
        if len(s) <= lim:
            return s
        return s[:lim].rstrip() + "\n…（以下省略）"

    return {
        "pre_ch4_raw": clip(pre_ch4_raw, limit=max_len * 2),
        "section_ident": clip(ident, limit=max_len * 2),
        "section_3": clip(sec3, limit=max_len),
        "section_4": clip(sec4),
        "section_17": clip(sec17),
        "section_18": clip(sec18),
        "section_11": clip(sec11),
        "section_6710": clip(sec6710),
    }


def _brand_from_rss_title(title: str) -> str:
    q3 = query_builder.query_pass3_middle_dot(title)
    if q3 and q3.strip():
        return q3.strip()[:120]
    q1 = query_builder.query_pass1(title)
    if "・" in q1:
        tail = q1.rsplit("・", 1)[-1].strip()
        if tail:
            return tail[:120]
    return (q1 or "")[:120]


def _generic_from_ident(ident: str) -> str:
    """識別領域の「キ. 基準名」等から一般名相当を拾う（章3が空のときのフォールバック）。"""
    s = _nfkc(ident or "")
    if not s:
        return ""
    for pat in (
        r"キ[\.．]\s*基準名[：:\s]*([^\n]+)",
        r"基準名[：:\s]*([^\n]+)",
        r"一般名[（(][^）)]*[）)][：:\s]*([^\n]+)",
        r"一般名[：:\s]*([^\n]+)",
    ):
        m = re.search(pat, s)
        if m:
            line = m.group(1).strip()
            line = re.split(r"[／/]", line)[0].strip()
            if 2 <= len(line) <= 100:
                return line[:100]
    return ""


def _composition_after_yuko(s: str) -> str:
    """「有効成分」以降〜添加剤／3.2 製剤の性状 手前まで（1行に潰れた PDF 用）。"""
    m = re.search(r"有効成分", s)
    if not m:
        return ""
    tail = s[m.end() :]
    end_m = re.search(r"\s+(?:添加剤|3[\.．]2\s|製剤の性状)", tail)
    frag = tail[: end_m.start()] if end_m else tail
    return frag.strip()


def _generic_from_composition_fragment(frag: str) -> str:
    """組成小節テキストから一般名候補（和文・付加物／水和物等で終わる語）を拾う。"""
    f = _nfkc(frag or "").strip()
    if not f:
        return ""
    # 「1錠中 ツカチニブ エタノール付加物52.4mg」等（付加物の「付」は漢字）
    _w = r"[\u3040-\u309f\u30a0-\u30ff\u3400-\u9fff]"
    pat = re.compile(
        rf"(?:^|\s)({_w}+(?:\s+{_w}+){{0,4}}"
        r"(?:エタノール付加物|水和物|塩酸塩|硫酸塩|酒石酸塩|マレイン酸塩|メシル酸塩|付加物))"
        r"(?=\s|\(|$|\d)",
        re.MULTILINE,
    )
    skip_in = ("として", "mg", "μg", "mL", "mｇ", "販売名", "錠中", "注射液", "外形", "識別")
    for m in pat.finditer(f):
        g = m.group(1).strip()
        g = re.sub(r"(?:\s*\()?[\d.]+\s*m[gｇ]\)?\s*$", "", g, flags=re.I).strip()
        if any(x in g for x in skip_in):
            continue
        if re.match(r"^[0-9０-９]", g):
            continue
        if 4 <= len(g) <= 90:
            return g[:90]
    return ""


def _generic_from_section3(sec3: str) -> str:
    s = _nfkc(sec3 or "")
    if not s:
        return ""
    for pat in (
        r"有効成分[（(]([^）)]+)[）)]",
        r"有効成分\s*として[、,]?\s*([^\s。\n]+(?:\s+[^\s。\n]+){0,5}(?:エタノール付加物|水和物|塩酸塩|マレイン酸塩))",
        r"有効成分[^\n]*\n\s*([^\n]+(?:エタノール付加物|水和物|塩酸塩|付加物)[^\n]*)",
        r"本剤の有効成分は[、,]?\s*([^\s\n。]+(?:\s+[^\s\n。]+){0,6})",
        r"本剤1[^\n]*\n\s*([^\n]+(?:水和物|塩酸塩|エタノール付加物|付加物)[^\n]*)",
    ):
        m = re.search(pat, s)
        if m:
            g = m.group(1).strip()
            if 2 <= len(g) <= 90:
                return g
    comp = _composition_after_yuko(s)
    if comp:
        g = _generic_from_composition_fragment(comp)
        if g:
            return g
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
    for ln in lines:
        # 見出しのみの短い行はスキップ。1 行に 3.1〜添加剤まで潰れている場合はスキップしない
        if re.match(r"^3[\.．]1", ln) and len(ln) <= 48:
            continue
        if re.match(r"^添加剤", ln) or re.match(r"^3[\.．]2", ln) or re.match(r"^製剤の性状", ln):
            break
        if 6 <= len(ln) <= 85 and re.search(r"(水和物|塩酸塩|硫酸塩|付加物|錠|注射液|エタノール)", ln):
            if re.match(r"^3[\.．]", ln):
                continue
            if "販売名" in ln or re.match(r"^[0-9０-９]", ln):
                continue
            return ln[:90]
    return ""


def _efficacy_one_liner(sec4: str, max_chars: int = 200) -> str:
    s = (sec4 or "").strip()
    if not s:
        return ""
    parts: list[str] = []
    n = 0
    for chunk in re.split(r"(?<=[。．])", s):
        if not chunk.strip():
            continue
        parts.append(chunk.strip())
        n += len(chunk)
        if n >= 100 or len(parts) >= 2:
            break
    line = "".join(parts)
    line = re.sub(r"[ \t]+", " ", line).strip()
    if len(line) > max_chars:
        line = line[: max_chars - 1].rstrip() + "…"
    return line


def _yakka_bunrui(ident: str, sec3: str, sec4: str) -> str:
    for block in (ident, sec3, sec4):
        if not block:
            continue
        m = re.search(r"薬効分類[：:\s]*([^\n]+)", block)
        if m:
            return m.group(1).strip()[:120]
    return ""


def summarize_infographic_cards(*, rss_title: str, sections: dict[str, str]) -> dict[str, str]:
    """見本 HTML の上段3カードに近い短文（ルールベース。LLM 不使用）。"""
    ident = sections.get("section_ident") or ""
    pre_ch4 = (sections.get("pre_ch4_raw") or "").strip()
    sec3 = sections.get("section_3") or ""
    sec4 = sections.get("section_4") or ""
    brand = _brand_from_rss_title(rss_title)
    head_for_generic = pre_ch4 or ident
    generic = _generic_from_section3(sec3) or _generic_from_ident(head_for_generic)
    yakka = _yakka_bunrui(pre_ch4 or ident, sec3, sec4)
    eff = _efficacy_one_liner(sec4)
    preview_lim = 1600
    ident_preview = ident if len(ident) <= preview_lim else ident[: preview_lim - 1].rstrip() + "…"
    return {
        "card_brand": brand,
        "card_generic": generic,
        "card_yakka": yakka,
        "card_efficacy": eff,
        "ident_preview": ident_preview,
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
