"""Embed scripts/tukysa-built.css into reports/infographic_tukysa.html.

初回: CDN 版 head を置換。2回目以降: 既存のインライン <style> ブロックを更新。
事前に: npx tailwindcss@3.4.17 -c scripts/tailwind.tukysa.config.cjs -i scripts/tw-tukysa-input.css -o scripts/tukysa-built.css --minify
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
html_path = ROOT / "reports" / "infographic_tukysa.html"
css_path = ROOT / "scripts" / "tukysa-built.css"

CDN_HEAD = """  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            brand: { dark: '#4c1d95', mid: '#7c3aed', light: '#ede9fe' }
          },
          fontFamily: {
            sans: ['"Hiragino Sans"', '"Hiragino Kaku Gothic ProN"', 'Meiryo', 'sans-serif']
          }
        }
      }
    };
  </script>
  <style>
    @media print {"""

COMMENT = """  <!-- Tailwind JIT inlined (htmlpreview blocks cdn.tailwindcss.com). Regen: npx tailwindcss@3.4.17 -c scripts/tailwind.tukysa.config.cjs -i scripts/tw-tukysa-input.css -o scripts/tukysa-built.css --minify -->
  <style>
"""


def inlined_head(css: str) -> str:
    return f"{COMMENT}{css}\n  </style>\n  <style>\n    @media print {{"


def main() -> None:
    html = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    if CDN_HEAD in html:
        html_path.write_text(html.replace(CDN_HEAD, inlined_head(css), 1), encoding="utf-8")
        print("replaced CDN head -> inline CSS", len(css), "bytes")
        return

    pat = re.compile(
        r"(  <!-- Tailwind JIT inlined[^\n]*\n  <style>\n)(.*?)(\n  </style>\n  <style>\n    @media print \{)",
        re.DOTALL,
    )
    m = pat.search(html)
    if not m:
        raise SystemExit(
            "infographic_tukysa.html: 想定する head 構造が見つかりません（CDN でもインラインでもない）。"
        )
    html_path.write_text(
        pat.sub(lambda x: x.group(1) + css + x.group(3), html, count=1),
        encoding="utf-8",
    )
    print("updated inline CSS block", len(css), "bytes")


if __name__ == "__main__":
    main()
