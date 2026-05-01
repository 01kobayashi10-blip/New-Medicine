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
from typing import Any
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


def _inject_sec18_structure_newlines(s: str) -> str:
    """PDF で 18.1 見出しや 18.2 見出しが前行に潰れている場合に改行を補う。"""
    t = _nfkc(s or "")
    t = re.sub(r"(18[\.．]1\s*作用機序)\s*", r"\1\n", t)
    # 18.2 は「抗腫瘍作用」に限らず任意小見出し（例: CGRP 受容体…）の前で改行を補う。18.2.1 は「2」の直後が「.」のため除外。
    t = re.sub(r"(?<![\n])(?<![\d])(18[\.．]2)(?=\s)", r"\n\1", t)
    return t


def _soften_if_reference_markers(s: str) -> str:
    """添付文書由来の参照表記「18)。」を句点に寄せる。"""
    s = re.sub(r"(\d{1,3})\)\s*([。．])", r"\2", s or "")
    # 「23)、 。」のような参照直後の読点ノイズを弱める
    s = re.sub(r"(\d{1,3})\)\s*、\s*", "、", s)
    return s


def _clip_moa_body(s: str, max_chars: int) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


# 図解「作用機序」ブロックの長さ（計画案: 正しさ > 簡潔さ。ソフト超えは可、ハードで打ち止め）
_MOA_T_SOFT = 220
_MOA_T_HARD = 320
_MOA_SENT_MAX = 160
_MOA_MIN_LEN = 25
_MOA_GUIDANCE_NOTE = "詳細は添付文書「18.1 作用機序」をご確認ください。"

_RE_MO_VERB = re.compile(
    r"阻害|拮抗|抑制する|抑制|結合|示した|示す|介在|調節|増強|伝達|活性|作用する|結合する"
)
# 背景「〜は〜である」との区別用（関連するだけでは機序本文とみなさない）
_RE_MECH_VERB = re.compile(r"阻害|拮抗|抑制する|抑制|結合|示した|示す|キナーゼ|伝達|増強")
_RE_MO_SUBJECT = re.compile(r"本剤|当薬")
_RE_MO_TARGET = re.compile(r"HER2|CGRP|受容体|キナーゼ|エストロゲン|アンドロゲン", re.I)


def _moa_has_verb(s: str) -> bool:
    return bool(_RE_MO_VERB.search(s))


def _moa_background_only(s: str) -> bool:
    """病態説明の定義文（は〜である）で、機序らしい動詞を含まないもの。"""
    t = (s or "").strip()
    if _RE_MO_SUBJECT.search(t):
        return False
    if _RE_MECH_VERB.search(t):
        return False
    return bool(re.match(r"^.{0,120}は[^。]{0,200}である[。．]\s*$", t))


def _score_moa_sentence(s: str) -> int:
    sc = 0
    if _moa_background_only(s):
        sc -= 6
    if _RE_MO_SUBJECT.search(s) and _moa_has_verb(s):
        sc += 8
    elif _RE_MO_TARGET.search(s) and _moa_has_verb(s):
        sc += 5
    elif _moa_has_verb(s):
        sc += 2
    if _RE_MO_SUBJECT.search(s):
        sc += 1
    return sc


def _split_moa_sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []
    parts = re.split(r"(?<=[。．])", t)
    out = [p.strip() for p in parts if p.strip()]
    return out


def _clip_one_sentence(s: str, max_chars: int) -> str:
    """1文を max_chars 以内に収める。句点バックオフを優先。"""
    s = re.sub(r"\s+", " ", (s or "").strip())
    if len(s) <= max_chars:
        return s
    window = s[:max_chars]
    cut = max(window.rfind("。"), window.rfind("．"))
    if cut >= _MOA_MIN_LEN:
        return s[: cut + 1].strip()
    return s[: max_chars - 1].rstrip() + "…"


def _strip_sec18_body_if_18_2_leaked(body: str) -> str:
    """境界抽出の失敗時、本文中の 18.2 見出し以降を捨てる。"""
    m = re.search(r"18[\.．]2\s+", body or "")
    if m:
        return body[: m.start()].strip()
    return (body or "").strip()


def _clip_intro_hard(text: str, hard: int) -> tuple[str, bool]:
    """hard 字以内に収める。句点バックオフを試み、切り詰めたら (text, True)。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= hard:
        return t, False
    window = t[:hard]
    cut = max(window.rfind("。"), window.rfind("．"))
    if cut >= _MOA_MIN_LEN:
        return t[: cut + 1].strip(), True
    clipped = t[: hard - 1].rstrip() + "…"
    return clipped, True


def _pick_moa_intro_from_body181(body181: str) -> tuple[str, bool, str]:
    """
    18.1 本文から図解用 intro を組み立てる。
    戻り値: (intro, truncated_flag, pick_reason)
    """
    sentences = _split_moa_sentences(body181)
    if not sentences:
        intro, trunc = _clip_intro_hard(body181, _MOA_T_HARD)
        return intro, trunc, "no_sentence_split"

    indexed = list(enumerate(sentences))
    indexed.sort(key=lambda ix: (-_score_moa_sentence(ix[1]), ix[0]))
    best_i, best_s = indexed[0]
    s1 = _clip_one_sentence(best_s, _MOA_SENT_MAX)

    chosen = [s1]
    reason = f"best_score={_score_moa_sentence(best_s)}_idx={best_i}"

    need_second = (len(s1) < _MOA_MIN_LEN) or (not _moa_has_verb(s1))
    if need_second and len(indexed) > 1:
        for j, sent in indexed[1:]:
            if j == best_i:
                continue
            if sent.startswith(s1[: min(20, len(s1))]) or s1.startswith(sent[: min(20, len(sent))]):
                continue
            s2 = _clip_one_sentence(sent, _MOA_SENT_MAX)
            merged = "".join(chosen) + s2
            if len(merged) <= _MOA_T_HARD:
                chosen.append(s2)
                reason += f"+second_idx={j}"
                break
            if len("".join(chosen)) + len(s2) > _MOA_T_HARD:
                s2_short = _clip_one_sentence(sent, max(40, _MOA_T_HARD - len("".join(chosen)) - 1))
                if len("".join(chosen)) + len(s2_short) <= _MOA_T_HARD:
                    chosen.append(s2_short)
                    reason += f"+second_clipped_idx={j}"
                break

    merged = "".join(chosen)
    if len(merged) > _MOA_T_HARD:
        merged, trunc = _clip_intro_hard(merged, _MOA_T_HARD)
        return merged, trunc, reason + "_hard_clip"

    return merged, False, reason


def structure_section18_moa(sec18: str) -> dict[str, Any] | None:
    """18 章から 18.1 作用機序の本文のみを抽出（図解のメイン表示用）。該当が無ければ None。"""
    raw = (sec18 or "").strip()
    if not raw or len(raw) < 30:
        return None
    t = _inject_sec18_structure_newlines(raw)
    m181 = re.search(
        r"18[\.．]1\s*作用機序\s*(.*?)(?=18[\.．]2\s+|$)",
        t,
        re.DOTALL | re.I,
    )
    body181 = (m181.group(1).strip() if m181 else "") or ""
    body181 = _strip_sec18_body_if_18_2_leaked(body181)
    if not body181:
        return None
    body181 = _soften_if_reference_markers(body181)
    intro, truncated, _reason = _pick_moa_intro_from_body181(body181)
    out: dict[str, Any] = {"intro": intro, "cards": []}
    if truncated:
        out["intro_note"] = _MOA_GUIDANCE_NOTE
    return out


def _sec11_normalize_dots(s: str) -> str:
    """章 11 用：NFKC するが改行は維持する（_nfkc は全空白を潰すため使わない）。"""
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ln in t.split("\n"):
        u = unicodedata.normalize("NFKC", ln)
        u = u.replace("．", ".").replace("ｰ", "-")
        u = re.sub(r"[ \t\u3000]+", " ", u).strip()
        out.append(u)
    return "\n".join(out)


def _sec11_looks_soc_header(s: str) -> bool:
    """11.2 以降の MedDRA SOC 見出し行の簡易判定（誤検出を減らす）。"""
    s = s.strip()
    if not s or len(s) > 56 or len(s) < 4:
        return False
    if re.match(r"^11\.", s):
        return False
    if "、" in s or "。" in s:
        return False
    if re.search(r"\([\d.]+\s*[%％]", s):
        return False
    return bool(re.search(r"(障害|疾患|寄生虫症)$", s))


def _sec11_line_starts_new_segment(line: str) -> bool:
    ln = line.strip()
    if not ln:
        return False
    if re.match(r"^11\.1\.\d+", ln):
        return True
    if re.match(r"^11\.1\s+", ln) or re.match(r"^11\.1重大", ln) or ln == "11.1":
        return True
    if re.match(r"^11\.2\s+", ln) or ln.startswith("11.2"):
        return True
    if re.match(r"^5%以上", ln) or re.match(r"^5％以上", ln):
        return True
    if re.match(r"^\[\d", ln):
        return True
    return _sec11_looks_soc_header(ln)


def _sec11_merge_broken_lines(text: str) -> list[str]:
    """PDF 由来の不自然改行を前後行結合して整える。"""
    lines = [ln.strip() for ln in _sec11_normalize_dots(text).split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    out: list[str] = []
    for line in lines:
        if not out:
            out.append(line)
            continue
        prev = out[-1]
        if _sec11_line_starts_new_segment(line):
            out.append(line)
            continue
        if _sec11_looks_soc_header(prev) and _sec11_looks_soc_header(line):
            out.append(line)
            continue
        # SOC 見出しの直後は本文行のため結合しない（「胃腸障害」+「下痢(72%)」等の誤結合防止）
        if _sec11_looks_soc_header(prev) and not (
            _sec11_line_starts_new_segment(line) or _sec11_looks_soc_header(line)
        ):
            out.append(line)
            continue
        if prev.endswith(("。", "．", "?", "！", "」", "』", "]", "）", ")")):
            out.append(line)
            continue
        out[-1] = prev + line
    return out


_RE_SEC11_SYM_PCT = re.compile(
    r"([\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9／/・\-‐\s]{1,48}?)\s*[\(（]\s*([\d.]+\s*[%％])\s*[\)）]"
)


def _sec11_extract_symptom_bullets(body: str, soc: str, *, budget: list[int]) -> list[dict[str, str]]:
    """SOC ブロック本文から「名称（x%）」を拾い other_items 用 dict を返す。"""
    if budget[0] <= 0:
        return []
    raw = re.sub(r"\s+", " ", (body or "").strip())
    out: list[dict[str, str]] = []
    for m in _RE_SEC11_SYM_PCT.finditer(raw):
        name = re.sub(r"\s+", "", m.group(1).strip())
        pct = m.group(2).strip().replace("％", "%")
        if len(name) < 2:
            continue
        sym = f"{name}（{pct}）"
        out.append({"symptom": sym, "soc": soc})
        budget[0] -= 1
        if budget[0] <= 0:
            break
    if not out and raw and len(raw) >= 6 and budget[0] > 0:
        frag = _clip_moa_body(raw, 72)
        if frag and not re.match(r"^5%以上", frag):
            out.append({"symptom": frag, "soc": soc})
            budget[0] -= 1
    return out


def structure_section11_summary(sec11: str) -> dict[str, Any] | None:
    """
    11 章テキストから図解用「主な副作用」2カラム要約を組み立てる。
    パース不能時は None（テンプレは従来の全文表示にフォールバック）。
    """
    raw = _sec11_normalize_dots(sec11 or "").strip()
    if len(raw) < 80:
        return None
    lines = _sec11_merge_broken_lines(raw)
    if not lines:
        return None
    text = "\n".join(lines)
    m12 = re.search(r"(?m)^\s*11\.2\s+", text)
    if not m12:
        return None
    head = text[: m12.start()].strip()
    tail = text[m12.start() :].strip()

    serious_items: list[dict[str, str]] = []
    head_lines = [ln.strip() for ln in head.split("\n") if ln.strip()]
    i0 = 0
    for i, ln in enumerate(head_lines):
        if re.match(r"^11\.1\s+", ln) or re.match(r"^11\.1重大", ln):
            i0 = i + 1
            break
    serious_scan = head_lines[i0:]
    cur: dict[str, Any] | None = None
    for ln in serious_scan:
        m = re.match(r"^11\.1\.(\d+)\s+(.+)$", ln)
        if m:
            if cur:
                body = _clip_moa_body(" ".join(cur["body_lines"]), 320)
                serious_items.append(
                    {
                        "num": cur["num"],
                        "heading": _clip_moa_body(cur["heading"], 120),
                        "body": body,
                    }
                )
            cur = {"num": m.group(1), "heading": m.group(2).strip(), "body_lines": []}
        elif cur is not None:
            cur["body_lines"].append(ln)
    if cur:
        body = _clip_moa_body(" ".join(cur["body_lines"]), 320)
        serious_items.append(
            {
                "num": cur["num"],
                "heading": _clip_moa_body(cur["heading"], 120),
                "body": body,
            }
        )

    tail_lines = [ln.strip() for ln in tail.split("\n") if ln.strip()]
    j = 0
    if tail_lines and re.match(r"^11\.2", tail_lines[0]):
        j = 1
    while j < len(tail_lines):
        tl = tail_lines[j]
        if re.match(r"^5%以上", tl) or (
            "5%以上" in tl and "1%" in tl and len(tl) < 80
        ):
            j += 1
            continue
        break

    other_items: list[dict[str, str]] = []
    budget = [14]
    cur_soc = ""
    cur_parts: list[str] = []
    while j < len(tail_lines):
        ln = tail_lines[j]
        if re.match(r"^5%以上", ln) or ("5%以上" in ln and "1%" in ln and len(ln) < 80):
            j += 1
            continue
        if _sec11_looks_soc_header(ln):
            if cur_soc and cur_parts:
                body = " ".join(cur_parts)
                other_items.extend(
                    _sec11_extract_symptom_bullets(body, cur_soc, budget=budget)
                )
            cur_soc = ln.strip()
            cur_parts = []
        else:
            cur_parts.append(ln)
        j += 1
    if cur_soc and cur_parts:
        body = " ".join(cur_parts)
        other_items.extend(
            _sec11_extract_symptom_bullets(body, cur_soc, budget=budget)
        )

    if not serious_items and not other_items:
        return None

    band_5 = bool(re.search(r"5\s*[%％]\s*以上", tail[:1200]))
    other_title = (
        "その他の副作用（5%以上の例）"
        if band_5
        else "その他の副作用（1%以上など）"
    )

    return {
        "intro_note": "以下は頻度区分の例です。実際の症状・対処は必ず医療機関の指示に従ってください。",
        "panel_title": "主な副作用（添付文書の区分）",
        "serious_title": "重大な副作用（例）",
        "serious_items": serious_items[:6],
        "other_title": other_title,
        "other_items": other_items[:14],
    }


def _inject_sec17_subsection_newlines(s: str) -> str:
    """17.1.1 / 17.1.2 等の直前に改行を補う（1 行潰れ PDF 用）。"""
    t = _nfkc(s or "")
    return re.sub(r"(17[\.．]1[\.．]\d+)", r"\n\1", t)


def _trim_sec17_figure_noise(s: str) -> str:
    """図表参照の直前で本文を切る（Kaplan-Meier 等）。"""
    m = re.search(r"盲検下独立中央判定の評価に基づく無増悪生存期間のKaplan", s)
    if m:
        return s[: m.start()].strip()
    return s.strip()


def _split_sec17_trial_paragraphs_core(rest: str) -> tuple[str, str, str]:
    """試験ブロックを（デザイン概要・主要評価・副作用）に分割。クリップ前の1行化テキスト。"""
    s = re.sub(r"\s+", " ", (rest or "").strip())
    if not s:
        return "", "", ""
    m_key = re.search(r"(主要評価項目)", s)
    if not m_key:
        mid = min(360, max(140, len(s) // 2))
        return s[:mid].strip(), s[mid:].strip(), ""
    design = s[: m_key.start()].strip()
    tail = _trim_sec17_figure_noise(s[m_key.start() :])
    m_ae = re.search(
        r"(本剤群\d+例において、|副作用は日本人集団|(?<=[。．])\s*副作用は\d*例中)",
        tail,
    )
    if m_ae:
        result = tail[: m_ae.start()].strip()
        ae_block = tail[m_ae.start() :].strip()
        m_note = re.search(r"\s+注1\)", ae_block)
        if m_note:
            ae_block = ae_block[: m_note.start()].strip()
    else:
        result = tail.strip()
        ae_block = ""
    result = _soften_if_reference_markers(result)
    ae_block = _soften_if_reference_markers(ae_block)
    return design, result, ae_block


def _split_sec17_trial_paragraphs(rest: str) -> tuple[str, str, str]:
    """試験ブロックを（デザイン概要・主要評価・副作用記述）に分割（表示用にクリップ）。"""
    design, result, ae_block = _split_sec17_trial_paragraphs_core(rest)
    return (
        _clip_moa_body(design, 480),
        _clip_moa_body(result, 720),
        _clip_moa_body(ae_block, 520),
    )


def _trial_heading_display(heading: str) -> str:
    """見出しから 17.1.x 番号を除いた表示用タイトル。"""
    h = re.sub(r"\s+", " ", (heading or "").strip())
    return re.sub(r"^17[\.．]1[\.．]\d+\s+", "", h).strip()


def _split_design_population_protocol(
    design: str, *, pop_max: int = 420, prot_max: int = 480
) -> tuple[str, str]:
    """design 全文を対象患者向け・試験デザイン向けの2行に分割（ヒューリスティック）。"""
    t = (design or "").strip()
    if not t:
        return "", ""
    parts = [p.strip() for p in re.split(r"(?<=[。．])\s*", t) if p.strip()]
    if len(parts) >= 2:
        return _clip_moa_body(parts[0], pop_max), _clip_moa_body(" ".join(parts[1:]), prot_max)
    cut = -1
    key_full = "\u8a66\u9a13\u3092\u5b9f\u65bd\u3057\u305f"  # 試験を実施した
    key_short = "\u8a66\u9a13\u3092\u5b9f\u65bd"  # 試験を実施
    if key_full in t:
        cut = t.find(key_full) + len(key_full)
    elif key_short in t:
        cut = t.find(key_short) + len(key_short)
    if cut != -1 and cut < len(t) - 25:
        first = t[:cut].strip().rstrip("\u3002\uff0e")
        rest = t[cut:].strip().lstrip("\u3002\uff0e ")
        if len(rest) > 25:
            return _clip_moa_body(first, pop_max), _clip_moa_body(rest, prot_max)
    return _clip_moa_body(t, pop_max), ""


def _primary_endpoint_label_from_sec17(result: str) -> str:
    """主要評価項目の短いラベル（添付文書に近い語）。"""
    s = (result or "").strip()
    if not s:
        return ""
    m = re.search(
        r"(無増悪生存期間の中央値|無進行生存期間の中央値|総生存期間の中央値|全生存期間の中央値)",
        s,
    )
    if m:
        return m.group(1)
    m2 = re.search(r"主要評価項目である[^。]{0,220}?((?:独立中央判定の評価に基づく)?奏効率)(?:は|を)", s)
    if m2:
        return m2.group(1).strip()[:80]
    m3 = re.search(r"主要評価項目である.*?(奏効率)(?:は|を)", s)
    if m3:
        return m3.group(1)
    return ""


def _labeled_fields_from_sec17(design_full: str, result_full: str) -> dict[str, str]:
    pop, prot = _split_design_population_protocol(design_full)
    return {
        "population_line": pop,
        "protocol_line": prot,
        "primary_endpoint_label": _primary_endpoint_label_from_sec17(result_full),
    }


def _design_lines_from_sec17(design: str, *, max_lines: int = 4, line_max: int = 220) -> list[str]:
    """試験概要を短文行に分割（句点・接続詞で区切る）。"""
    t = (design or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。．])\s*", t) if p.strip()]
    lines: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) < 8:
            continue
        if len(p) > line_max:
            p = p[: line_max - 1].rstrip() + "…"
        lines.append(p)
        if len(lines) >= max_lines:
            break
    if not lines and t:
        lines.append(t[:line_max] + ("…" if len(t) > line_max else ""))
    return lines


def _efficacy_fragments_from_sec17(result: str) -> list[dict[str, Any]]:
    """
    主要評価テキストを表示用フラグメント列に変換（数値を em=True）。
    該当パターンが無い場合は全文を1フラグメント（em=False）で返す。
    """
    s = (result or "").strip()
    if not s:
        return []

    # PFS / OS 等: 中央値 本剤群 X 月 / 対照群 Y 月（「本剤群320例」等は非貪欲マッチで回避）
    m_pfs = re.search(
        r"(主要評価項目である.*?)(本剤群で|本剤群)([\d.]+)\s*ヵ?\s*月\s*([、,])\s*"
        r"(対照群で|対照群)([\d.]+)\s*ヵ?\s*月\s*(であった|となった|であり、|等)",
        s,
    )
    if m_pfs:
        fr: list[dict[str, Any]] = [
            {"t": m_pfs.group(1), "em": False},
            {"t": m_pfs.group(2), "em": False},
            {"t": m_pfs.group(3), "em": True},
            {"t": "ヵ月", "em": False},
            {"t": m_pfs.group(4), "em": False},
            {"t": m_pfs.group(5), "em": False},
            {"t": m_pfs.group(6), "em": True},
            {"t": "ヵ月", "em": False},
            {"t": m_pfs.group(7), "em": False},
        ]
        tail = s[m_pfs.end() :].lstrip()
        if tail.startswith("。"):
            tail = tail[1:].lstrip()
        # ハザード比・95%CI・p
        m_hr = re.search(
            r"ハザード比[はが]?\s*([\d.]+)\s*（\s*95\s*％\s*信頼区間[^（]*（\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*）\s*）",
            tail,
        )
        if not m_hr:
            m_hr = re.search(
                r"ハザード比[はが]?\s*([\d.]+)\s*[（(]\s*95\s*%[^）)]*信頼区間[^）)]*[（(]\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*[）)]",
                tail,
            )
        if not m_hr:
            m_hr = re.search(
                r"ハザード比[はが]?\s*([\d.]+)\s*[（(]\s*95\s*[%％]\s*信頼区間\s*[:：]\s*"
                r"([\d.]+)\s*[,，、]\s*([\d.]+)\s*(?:[）)]|[、,])",
                tail,
            )
        if m_hr:
            fr.append({"t": " ハザード比 ", "em": False})
            fr.append({"t": m_hr.group(1), "em": True})
            fr.append({"t": "（95% CI ", "em": False})
            fr.append({"t": m_hr.group(2), "em": True})
            fr.append({"t": "–", "em": False})
            fr.append({"t": m_hr.group(3), "em": True})
            fr.append({"t": "）", "em": False})
            tail = tail[m_hr.end() :]
        m_p = re.search(
            r"(?:層別ログランク検定\s*)?p\s*[<＜]\s*([\d.]+)|p\s*値\s*は\s*([\d.]+)\s*未満",
            tail,
        )
        if m_p:
            pv = m_p.group(1) or m_p.group(2)
            fr.append({"t": "、", "em": False})
            if m_p.group(1):
                fr.append({"t": f"p < {pv}", "em": True})
            else:
                fr.append({"t": f"p値 < {pv}", "em": True})
        return fr

    # 奏効率など
    m_orr = re.search(
        r"(主要評価項目である.*?奏効率は)([\d.]+)\s*[%％]\s*(であった|となった|で)",
        s,
    )
    if m_orr:
        fr2: list[dict[str, Any]] = [
            {"t": m_orr.group(1), "em": False},
            {"t": m_orr.group(2), "em": True},
            {"t": "%", "em": False},
            {"t": m_orr.group(3), "em": False},
        ]
        tail2 = s[m_orr.end() :]
        m_ci = re.search(
            r"\s*（\s*(90|95)\s*[%％]\s*信頼区間[^（]*（\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*）\s*）",
            tail2,
        )
        if m_ci:
            fr2.append({"t": "（", "em": False})
            fr2.append({"t": m_ci.group(1), "em": True})
            fr2.append({"t": "% CI ", "em": False})
            fr2.append({"t": m_ci.group(2), "em": True})
            fr2.append({"t": "–", "em": False})
            fr2.append({"t": m_ci.group(3), "em": True})
            fr2.append({"t": "）", "em": False})
        return fr2

    return [{"t": _clip_moa_body(s, 900), "em": False}]


def _result_compare_three_from_sec17(result: str) -> dict[str, Any] | None:
    """
    主要評価の結果を「比較3ブロック」用データに変換（PFS中央値比較・奏効率＋CI 等）。
    該当パターンが無ければ None。
    """
    s = (result or "").strip()
    if not s:
        return None

    m_pfs = re.search(
        r"(主要評価項目である.*?)(本剤群で|本剤群)([\d.]+)\s*ヵ?\s*月\s*([、,])\s*"
        r"(対照群で|対照群)([\d.]+)\s*ヵ?\s*月\s*(であった|となった|であり、|等)",
        s,
    )
    if m_pfs:
        intro = _clip_moa_body(re.sub(r"\s+", " ", m_pfs.group(1).strip()), 200)
        tail = s[m_pfs.end() :].lstrip()
        if tail.startswith("。"):
            tail = tail[1:].lstrip()
        stat_lines: list[str] = []
        m_hr = re.search(
            r"ハザード比[はが]?\s*([\d.]+)\s*（\s*95\s*％\s*信頼区間[^（]*（\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*）\s*）",
            tail,
        )
        if not m_hr:
            m_hr = re.search(
                r"ハザード比[はが]?\s*([\d.]+)\s*[（(]\s*95\s*%[^）)]*信頼区間[^）)]*[（(]\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*[）)]",
                tail,
            )
        if not m_hr:
            m_hr = re.search(
                r"ハザード比[はが]?\s*([\d.]+)\s*[（(]\s*95\s*[%％]\s*信頼区間\s*[:：]\s*"
                r"([\d.]+)\s*[,，、]\s*([\d.]+)\s*(?:[）)]|[、,])",
                tail,
            )
        if m_hr:
            stat_lines.append(f"ハザード比 {m_hr.group(1)}")
            stat_lines.append(f"95% CI {m_hr.group(2)}–{m_hr.group(3)}")
            tail = tail[m_hr.end() :]
        m_p = re.search(
            r"(?:層別ログランク検定\s*)?p\s*[<＜]\s*([\d.]+)|p\s*値\s*は\s*([\d.]+)\s*未満",
            tail,
        )
        if m_p:
            pv = m_p.group(1) or m_p.group(2)
            if m_p.group(1):
                stat_lines.append(f"p < {pv}")
            else:
                stat_lines.append(f"p値 < {pv}")
        return {
            "variant": "pfs_medians",
            "intro": intro or None,
            "b1": {"title": "本剤群", "subtitle": "中央値", "value": m_pfs.group(3), "unit": "ヵ月"},
            "b2": {"title": "対照群", "subtitle": "中央値", "value": m_pfs.group(6), "unit": "ヵ月"},
            "b3": {"title": "統計", "lines": stat_lines if stat_lines else ["（詳細は添付文書）"]},
        }

    m_orr = re.search(
        r"(主要評価項目である.*?奏効率は)([\d.]+)\s*[%％]\s*(であった|となった|で)",
        s,
    )
    if m_orr:
        pct = m_orr.group(2)
        tail2 = s[m_orr.end() :]
        m_ci = re.search(
            r"\s*（\s*(90|95)\s*[%％]\s*信頼区間[^（]*（\s*([\d.]+)\s*[,，、～〜\-]+\s*([\d.]+)\s*）\s*）",
            tail2,
        )
        ci_label = "信頼区間"
        ci_val = "—"
        if m_ci:
            ci_label = f"{m_ci.group(1)}% 信頼区間"
            ci_val = f"{m_ci.group(2)}–{m_ci.group(3)}"
        note_lines: list[str] = []
        m_note = re.search(r"(事前規定[^。]{8,100}。?)", tail2)
        if not m_note:
            m_note = re.search(r"(事前規定[^。]{8,100}。?)", s)
        if m_note:
            note_lines.append(re.sub(r"\s+", " ", m_note.group(1).strip()))
        if not note_lines:
            note_lines.append("（詳細は添付文書）")
        intro = _clip_moa_body(re.sub(r"\s+", " ", m_orr.group(1).strip()), 160)
        return {
            "variant": "orr_pct",
            "intro": intro or None,
            "b1": {"title": "奏効率", "subtitle": "主要評価", "value": pct, "unit": "%"},
            "b2": {"title": ci_label, "subtitle": "", "value": ci_val, "unit": ""},
            "b3": {"title": "注記", "lines": note_lines},
        }

    return None


def _ae_items_from_sec17(ae_block: str, *, max_items: int = 8) -> list[str]:
    """副作用ブロックから「名称（x.x%）」形式を抽出。"""
    t = (ae_block or "").strip()
    if not t:
        return []
    items: list[str] = []
    for m in re.finditer(
        r"([\u3040-\u30ff\u4e00-\u9fff]{1,14}?)\s*[（(]\s*([\d.]+\s*%)\s*[）)]",
        t,
    ):
        name, pct = m.group(1).strip(), m.group(2).strip()
        if len(name) >= 2:
            items.append(f"{name}（{pct}）")
        if len(items) >= max_items:
            break
    if items:
        return items
    # フォールバック: 「主な副作用は〜」の直後の短い語
    m = re.search(r"主な副作用は([^、。]{2,14})であった", t)
    if m:
        return [m.group(1).strip()]
    return []


def _dosage6710_line_starts_segment(line: str) -> bool:
    """章6・7・10 および減量表見出しの行頭判定（PDF 改行結合用）。"""
    s = line.strip()
    if not s:
        return False
    if re.match(r"^6\s*\.\s*用法及び用量", s):
        return True
    if re.match(r"^7\s*\.\s*用法及び用量に関連", s):
        return True
    if re.match(r"^7\s*\.\s*[1-9]\d*\s+", s):
        return True
    if re.match(r"^10\s*\.", s):
        return True
    if s.startswith("減量・中止") or s.startswith("副作用に対する休薬"):
        return True
    if re.match(r"^通常投与量", s) or re.match(r"^[1-4]段階減量", s):
        return True
    if re.match(r"^体表面積", s):
        return True
    if re.match(r"^ULN[:：]", s) or s.startswith("a)Grade"):
        return True
    return False


def _sec6710_merge_broken_lines(text: str) -> str:
    """6–10 抜粋テキストの PDF 由来改行を結合する。"""
    lines = [ln.strip() for ln in _sec11_normalize_dots(text or "").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    out: list[str] = []
    for line in lines:
        if not out:
            out.append(line)
            continue
        prev = out[-1]
        if _dosage6710_line_starts_segment(line):
            out.append(line)
            continue
        if prev.endswith(
            ("。", "．", "?", "！", "」", "』", "]", "）", ")", "削る。", "する。")
        ):
            out.append(line)
            continue
        out[-1] = prev + line
    return "\n".join(out)


def _dosage_extract_7_subsection(full: str, num: str) -> str:
    """7.n 小節本文（次の 7.m / 減量表 / 10 まで）。"""
    m = re.search(rf"(?m)^7\s*\.\s*{num}\s+", full)
    if not m:
        return ""
    rest = full[m.end() :]
    m_end = re.search(
        r"(?m)^7\s*\.\s*(?:[1-9]|1[0-9])\s+|^減量・中止|^副作用に対する休薬|^10\.",
        rest,
    )
    body = rest[: m_end.start()] if m_end else rest
    body = body.replace("\n", " ")
    body = re.sub(r"\s+", " ", body).strip()
    cut = body.find("減量・中止する場合の投与量")
    if cut != -1:
        body = body[:cut].strip()
    return body


def _dosage_sec6_body_from_merged(merged: str) -> str:
    """
    章6本文（見出し直後〜「7. 用法及び用量に関連」手前まで）。
    PDF 由来で「7.」が行頭でない場合も拾う（^ 依存の正規表現では落ちるため）。
    抜粋が「6. 用法及び用量」行より後ろから始まる場合（章境界で見出しが落ちる）は、
    全文先頭〜「7. 用法及び用量に関連」手前を章6相当として扱う。
    """
    m = re.search(r"6\s*\.\s*用法及び用量", merged)
    if m:
        tail = merged[m.end() :]
        j = re.search(r"7\s*\.\s*用法及び用量に関連", tail)
        body = tail[: j.start()].strip() if j else tail.strip()
        if body.startswith("用法及び用量"):
            body = re.sub(r"^用法及び用量\s*", "", body).strip()
        return body
    jonly = re.search(r"7\s*\.\s*用法及び用量に関連", merged)
    if jonly and jonly.start() > 0:
        return merged[: jonly.start()].strip()
    m71 = re.search(r"7\s*\.\s*1\s+", merged)
    if m71 and m71.start() > 0:
        return merged[: m71.start()].strip()
    return merged.strip()


def _dosage_standard_bullet(sec6_body: str) -> str | None:
    """6 章本文から標準用法の1行メモを組み立てる。"""
    raw = (sec6_body or "").strip()
    if len(raw) < 12:
        return None
    line = unicodedata.normalize("NFKC", raw)
    line = re.sub(r"\s+", " ", line)
    if len(line) < 24:
        return None
    dose_m = re.search(r"1回\s*(\d+)\s*m[gｇ]", line, re.I)
    day_m = re.search(r"1日\s*(\d+)\s*回", line)
    if not dose_m or not day_m:
        return None
    dose, nday = dose_m.group(1), day_m.group(1)
    ing_m = re.search(
        r"(?:通常、|通常)?(?:成人|小児等|小児|患者)には\s*([^\s、。]+(?:\s+[^\s、。]+){0,4}?)\s*として\s*1回",
        line,
    )
    if not ing_m:
        ing_m = re.search(
            r"には\s*([^\s、。]+(?:\s+[^\s、。]+){0,3}?)\s*として\s*1回",
            line,
        )
    ing = ing_m.group(1).strip() if ing_m else "本剤"
    combo_m = re.search(r"(.+?(?:との併用において|併用において))", line)
    if combo_m:
        combo = combo_m.group(1).strip()
        bullet = (
            f"{combo}、通常成人には{ing}として1回{dose}mgを1日{nday}回経口"
        )
    else:
        bullet = f"通常成人には{ing}として1回{dose}mgを1日{nday}回経口"
    if not bullet.endswith("経口") and "経口投与" in line:
        bullet += "投与"
    elif not re.search(r"経口(投与)?$", bullet):
        bullet += "経口"
    bullet += "。"
    if "適宜減量" in line or "減量する" in line:
        bullet += "患者の状態により適宜減量（添付文書「6」「7.3」）。"
    else:
        bullet += "（添付文書「6」）。"
    return bullet


def _dosage_hepatic_bullet(b72: str) -> str | None:
    if not b72 or ("肝" not in b72 and "Child" not in b72):
        return None
    dm = re.search(r"1回\s*(\d+)\s*m[gｇ].{0,120}?1日\s*(\d+)\s*回", b72, re.I | re.DOTALL)
    if dm:
        d1, nfreq = dm.group(1), dm.group(2)
    else:
        dm2 = re.search(r"1回\s*(\d+)\s*m[gｇ]", b72, re.I)
        if not dm2:
            return None
        d1 = dm2.group(1)
        nfreq_m = re.search(r"1日\s*(\d+)\s*回", b72)
        nfreq = nfreq_m.group(1) if nfreq_m else "2"
    return (
        f"重度の肝機能障害（Child-Pugh 分類 C）では開始用量 1 回 {d1}mg を "
        f"1 日 {nfreq} 回（「7.2」）。"
    )


def _dosage_cyp_bullet(b74: str) -> str | None:
    if not b74 or "CYP2C8" not in b74:
        return None
    dm = re.search(r"1回\s*(\d+)\s*m[gｇ].{0,120}?1日\s*(\d+)\s*回", b74, re.I | re.DOTALL)
    if not dm:
        return None
    return (
        f"強い CYP2C8 阻害剤併用時は開始用量 1 回 {dm.group(1)}mg を "
        f"1 日 {dm.group(2)} 回（「7.4」）。"
    )


def structure_dosage_memo(section_6710: str) -> dict[str, Any] | None:
    """
    section_6710（章6・7 + 章10）から図解用の箇条書きメモを組み立てる。
    パース不能・情報不足時は None（テンプレは全文表示にフォールバック）。
    """
    raw = (section_6710 or "").strip()
    if len(raw) < 120:
        return None
    merged = unicodedata.normalize("NFKC", _sec6710_merge_broken_lines(raw))
    if len(merged) < 100:
        return None

    sec6_body = _dosage_sec6_body_from_merged(merged)

    bullets: list[str] = []
    std = _dosage_standard_bullet(sec6_body) if sec6_body else None
    if std:
        bullets.append(std)

    b72 = _dosage_extract_7_subsection(merged, "2")
    hb = _dosage_hepatic_bullet(b72)
    if hb:
        bullets.append(hb)

    b74 = _dosage_extract_7_subsection(merged, "4")
    cb = _dosage_cyp_bullet(b74)
    if cb:
        bullets.append(cb)

    b71 = _dosage_extract_7_subsection(merged, "1")
    b75 = _dosage_extract_7_subsection(merged, "5")
    note71 = ""
    if b71 and ("単独" in b71 or "確立" in b71 or "有効性" in b71):
        first = b71.split("。")[0].strip()
        frag = (first + "。") if first and not first.endswith("。") else (first or "")
        frag = _clip_moa_body(frag, 220)
        if frag:
            note71 = frag + "（「7.1」）"
    note75 = ""
    if b75 and ("カペシタビン" in b75 or "併用" in b75):
        note75 = "カペシタビン用量は添付文書「7.5」に表あり（「7.5」）。"
    if note71 or note75:
        combined = " ".join(x for x in (note71, note75) if x).strip()
        if combined:
            bullets.append(combined)

    if not bullets:
        return None

    return {
        "panel_title": "用法・用量のメモ（概要）",
        "bullets": bullets,
        "source_note": "添付文書「6」「7」「10」より自動要約。",
    }


def _lead_and_bullets_from_paragraph(
    text: str, *, lead_max: int = 200
) -> tuple[str, list[str]]:
    """
    句点で分割し、先頭文を1行リード、それ以降を箇条書きにする。
    先頭文のみが lead_max を超える場合はリードを短縮し、続きを先頭の箇条書きに回す。
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return "", []
    parts = [p.strip() for p in re.split(r"(?<=[。．])\s*", t) if p.strip()]
    if not parts:
        return "", []
    first, tail = parts[0], parts[1:]
    tn = re.sub(r"\s+", " ", first.strip())
    bullets: list[str] = []

    if len(tn) > lead_max:
        raw_cut = tn[: lead_max - 1].rstrip()
        lead = raw_cut + "…"
        rem = tn[len(raw_cut) :].strip()
        if rem:
            if not rem.endswith(("。", "．")):
                rem = rem + "。"
            bullets.append(rem)
    else:
        lead = tn

    for p in tail:
        seg = p.strip()
        if not seg:
            continue
        if not seg.endswith(("。", "．")):
            seg = seg + "。"
        bullets.append(seg)

    return lead, bullets


def _enrich_sec17_trial_dict(
    design_full: str, result_full: str, ae_full: str
) -> dict[str, Any]:
    """図解用の構造化フィールド。"""
    out: dict[str, Any] = {
        "design_lines": _design_lines_from_sec17(design_full),
        "efficacy_fragments": _efficacy_fragments_from_sec17(result_full),
        "ae_items": _ae_items_from_sec17(ae_full),
    }
    out.update(_labeled_fields_from_sec17(design_full, result_full))
    pop_line = (out.get("population_line") or "").strip()
    prot_line = (out.get("protocol_line") or "").strip()
    pl, pbl = _lead_and_bullets_from_paragraph(pop_line)
    prl, prbl = _lead_and_bullets_from_paragraph(prot_line)
    out["population_lead"] = pl
    out["population_bullets"] = pbl
    out["protocol_lead"] = prl
    out["protocol_bullets"] = prbl
    out["result_compare"] = _result_compare_three_from_sec17(result_full)
    return out


def structure_section17_trials(sec17: str) -> dict[str, Any] | None:
    """
    17.1.x 試験見出しでブロック分割し、カード表示用データを返す。
    17.1.1 形式が検出できなければ None（従来の全文表示にフォールバック）。
    """
    raw = (sec17 or "").strip()
    if not raw or len(raw) < 60:
        return None
    t = _inject_sec17_subsection_newlines(raw)
    if not re.search(r"17[\.．]1[\.．]\d+", t):
        return None
    chunks = re.split(r"(?=17[\.．]1[\.．]\d+\s)", t)
    lead = ""
    trials: list[dict[str, Any]] = []
    for block in chunks:
        b = block.strip()
        if not b:
            continue
        if not re.match(r"^17[\.．]1[\.．]\d+", b):
            if not lead and re.match(r"^17[\.．]1\s", b):
                lead = _clip_moa_body(b, 360)
            continue
        m_ln = re.match(r"^(17[\.．]1[\.．]\d+\s+.+?試験\])", b)
        if not m_ln:
            m_ln = re.match(r"^(17[\.．]1[\.．]\d+\s+[^\n]+)", b)
        if not m_ln:
            continue
        heading = re.sub(r"\s+", " ", m_ln.group(1).strip())
        rest = b[m_ln.end() :].strip()
        if len(rest) < 18:
            continue
        design_f, result_f, ae_f = _split_sec17_trial_paragraphs_core(rest)
        design, result, ae_note = (
            _clip_moa_body(design_f, 480),
            _clip_moa_body(result_f, 720),
            _clip_moa_body(ae_f, 520),
        )
        row: dict[str, Any] = {
            "heading": heading,
            "heading_display": _trial_heading_display(heading),
            "design": design,
            "result": result,
            "ae_note": ae_note,
        }
        row.update(_enrich_sec17_trial_dict(design_f, result_f, ae_f))
        trials.append(row)
    if not trials:
        return None
    return {"lead": lead, "trials": trials}


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
