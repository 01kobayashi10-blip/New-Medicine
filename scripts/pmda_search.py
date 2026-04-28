"""PMDA 医療用医薬品 添付文書等情報検索（Web フォーム POST）から候補を取得する。

公式サイトの利用条件・個人情報保護方針に従い、短い間隔を空けてアクセスする。
ネットワークを無効化する場合は環境変数 ``PMDA_SEARCH_DISABLED=1`` を設定する。
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass

BASE_ORIGIN = "https://www.pmda.go.jp"
IYAKU_SEARCH_URL = BASE_ORIGIN + "/PmdaSearch/iyakuSearch/"
USER_AGENT = (
    "New-Medicine/1.0 (+https://github.com) "
    "PMDA iyakuSearch client; respectful crawl"
)

_MAX_NAME_LEN = 80
_MAX_CANDIDATES = 60
_DEFAULT_MIN_INTERVAL = 1.2

_throttle_lock = threading.Lock()
_last_request_mono: float = 0.0


@dataclass(frozen=True)
class PmdaCandidate:
    label: str
    detail_url: str


def throttle_pmda_http() -> None:
    """検索以外の PMDA 系 GET（添付文書 HTML/PDF 等）の直前にも呼ぶ。間隔は PMDA_SEARCH_MIN_INTERVAL_SEC。"""
    try:
        delay = float(os.environ.get("PMDA_SEARCH_MIN_INTERVAL_SEC", _DEFAULT_MIN_INTERVAL))
    except ValueError:
        delay = _DEFAULT_MIN_INTERVAL
    global _last_request_mono
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_mono + delay - now
        if wait > 0:
            time.sleep(wait)
        _last_request_mono = time.monotonic()


def _throttle() -> None:
    if os.environ.get("PMDA_SEARCH_DISABLED", "").strip() in ("1", "true", "yes"):
        return
    throttle_pmda_http()


def _parse_form_pairs(html: str) -> list[tuple[str, str]]:
    m = re.search(
        r'<form[^>]*name="iyakuSearchActionForm"[^>]*>(.*?)</form>',
        html,
        re.DOTALL | re.I,
    )
    if not m:
        return []
    frag = m.group(1)
    pairs: list[tuple[str, str]] = []

    for inp in re.finditer(r"<input\s+([^>]+)>", frag, re.I | re.DOTALL):
        attrs = inp.group(1)
        nm = re.search(r'name="([^"]+)"', attrs, re.I)
        if not nm:
            continue
        name = nm.group(1)
        ty = re.search(r'type="([^"]+)"', attrs, re.I)
        typ = (ty.group(1) if ty else "text").lower()
        if typ in ("button", "image"):
            continue
        vm = re.search(r'value="([^"]*)"', attrs, re.I)
        val = vm.group(1) if vm else ""
        if typ == "checkbox":
            if re.search(r"checked", attrs, re.I):
                pairs.append((name, val))
            continue
        if typ == "radio":
            if re.search(r"checked", attrs, re.I):
                pairs.append((name, val))
            continue
        pairs.append((name, val))

    for sel in re.finditer(
        r"<select\s+([^>]+)>(.*?)</select>", frag, re.DOTALL | re.I
    ):
        sat = sel.group(1)
        body = sel.group(2)
        nm = re.search(r'name="([^"]+)"', sat, re.I)
        if not nm:
            continue
        name = nm.group(1)
        opt_val: str | None = None
        for o in re.finditer(
            r"<option\s+([^>]*)>(.*?)</option>", body, re.DOTALL | re.I
        ):
            oa = o.group(1)
            if re.search(r"selected", oa, re.I):
                vm = re.search(r'value="([^"]*)"', oa, re.I)
                opt_val = vm.group(1) if vm else ""
                break
        if opt_val is None:
            first = re.search(r"<option\s+([^>]*)>", body, re.I)
            if first:
                vm = re.search(r'value="([^"]*)"', first.group(1), re.I)
                opt_val = vm.group(1) if vm else ""
            else:
                opt_val = ""
        pairs.append((name, opt_val))

    return pairs


def _form_pairs_to_ordered_dict(pairs: list[tuple[str, str]]) -> OrderedDict[str, str]:
    od: OrderedDict[str, str] = OrderedDict()
    for k, v in pairs:
        od[k] = v
    return od


def _parse_result_rows(html: str) -> list[tuple[str, str, str]]:
    """各行 (相対 path, 一般名リンクテキスト, 販売名セルテキスト)。"""
    pat = re.compile(
        r"href=['\"](/PmdaSearch/iyakuDetail/GeneralList/\d+)['\"]"
        r"[^>]*>([^<]+)</a></div></td>\s*<td><div>([^<]*)</div></td>",
        re.I,
    )
    return pat.findall(html)


def _merge_rows(rows: list[tuple[str, str, str]]) -> list[PmdaCandidate]:
    """同一 GeneralList に複数剤形がある場合は販売名をまとめ、1 候補にする。"""
    by_path: OrderedDict[str, list[str]] = OrderedDict()
    general_by_path: OrderedDict[str, str] = OrderedDict()
    for path, general, hon in rows:
        label_piece = (hon or "").strip() or (general or "").strip()
        if path not in by_path:
            by_path[path] = []
            general_by_path[path] = (general or "").strip()
        if label_piece and label_piece not in by_path[path]:
            by_path[path].append(label_piece)
    out: list[PmdaCandidate] = []
    for path, pieces in by_path.items():
        if not pieces:
            g = general_by_path.get(path, "")
            pieces = [g] if g else []
        label = "／".join(pieces) if pieces else path.rsplit("/", 1)[-1]
        url = BASE_ORIGIN + path
        out.append(PmdaCandidate(label=label, detail_url=url))
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _one_search(
    q: str,
    timeout: int,
    name_radio: str,
    match_radio: str,
    list_rows: str,
) -> list[PmdaCandidate]:
    """GET フォーム取得 → POST 検索 → 候補化（1 サイクル）。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", USER_AGENT)]

    try:
        req0 = urllib.request.Request(
            IYAKU_SEARCH_URL,
            headers={"User-Agent": USER_AGENT},
        )
        with opener.open(req0, timeout=timeout) as r0:
            html0 = r0.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return []

    pairs = _parse_form_pairs(html0)
    if not pairs:
        return []
    fields = _form_pairs_to_ordered_dict(pairs)
    fields["nameWord"] = q
    fields["iyakuHowtoNameSearchRadioValue"] = name_radio
    fields["howtoMatchRadioValue"] = match_radio
    fields["ListRows"] = list_rows
    fields["howtoRdSearchSel"] = fields.get("howtoRdSearchSel") or "or"
    fields["dispColumnsList[0]"] = "1"
    fields["btnA.x"] = "5"
    fields["btnA.y"] = "5"

    body = urllib.parse.urlencode(list(fields.items())).encode("utf-8")
    req1 = urllib.request.Request(
        IYAKU_SEARCH_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": IYAKU_SEARCH_URL,
            "Origin": BASE_ORIGIN,
        },
    )
    try:
        with opener.open(req1, timeout=timeout) as r1:
            out_html = r1.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return []

    if "該当する情報はありません" in out_html:
        return []

    rows = _parse_result_rows(out_html)
    if not rows:
        return []
    return _merge_rows(rows)


def search_candidates(query: str, timeout: int = 30) -> list[PmdaCandidate]:
    """販売名等のクエリで PMDA 医療用医薬品検索を行い、候補一覧を返す。

    既定は「販売名のみ」検索だが、0 件のときは同一クエリで「一般名及び販売名」に一度だけフォールバックする
    （環境変数 ``PMDA_SEARCH_NO_RADIO_FALLBACK=1`` で無効化）。
    """
    q = (query or "").strip()
    if not q:
        return []
    if os.environ.get("PMDA_SEARCH_DISABLED", "").strip() in ("1", "true", "yes"):
        return []

    if len(q) > _MAX_NAME_LEN:
        q = q[:_MAX_NAME_LEN]

    name_radio = os.environ.get("PMDA_SEARCH_NAME_RADIO", "3").strip() or "3"
    if name_radio not in ("1", "2", "3"):
        name_radio = "3"
    match_radio = os.environ.get("PMDA_SEARCH_MATCH_RADIO", "1").strip() or "1"
    if match_radio not in ("1", "2"):
        match_radio = "1"
    list_rows = os.environ.get("PMDA_SEARCH_LIST_ROWS", "50").strip() or "50"
    if list_rows not in ("10", "20", "30", "50", "100"):
        list_rows = "50"

    _throttle()
    found = _one_search(q, timeout, name_radio, match_radio, list_rows)
    if found:
        return found
    no_fb = os.environ.get("PMDA_SEARCH_NO_RADIO_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if name_radio == "3" and not no_fb:
        _throttle()
        return _one_search(q, timeout, "1", match_radio, list_rows)
    return []
