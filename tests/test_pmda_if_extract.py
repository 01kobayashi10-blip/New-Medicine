"""pmda_if_extract のユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pmda_if_extract  # noqa: E402


class TestPickPdfUrl(unittest.TestCase):
    def test_single_pair(self) -> None:
        pairs = [
            (
                "https://www.pmda.go.jp/PmdaSearch/iyakuDetail/ResultDataSetPDF/x",
                (2026, 4, 15),
                "アクイプタ錠",
            )
        ]
        self.assertEqual(
            pmda_if_extract.pick_pdf_url(pairs, "アッヴィ　アクイプタ錠を発売"),
            pairs[0][0],
        )

    def test_prefers_row_matching_title(self) -> None:
        good = "https://www.pmda.go.jp/a.pdf"
        bad = "https://www.pmda.go.jp/b.pdf"
        pairs = [
            (bad, (2025, 1, 1), "別剤 ダミー錠"),
            (good, (2026, 4, 15), "アクイプタ錠60mg アッヴィ"),
        ]
        self.assertEqual(
            pmda_if_extract.pick_pdf_url(pairs, "アクイプタ錠を発売"),
            good,
        )


class TestExtractPdfPairs(unittest.TestCase):
    def test_relative_href_resolves(self) -> None:
        html = """
        <tr><td>アクイプタ錠</td><td>
        <a href="../ResultDataSetPDF/112130_1190036F3024_1_01">PDF(2026年04月15日)</a>
        </td></tr>
        """
        base = "https://www.pmda.go.jp/PmdaSearch/iyakuDetail/GeneralList/1190036"
        pairs = pmda_if_extract.extract_result_dataset_pdf_pairs(html, base)
        self.assertEqual(len(pairs), 1)
        url, dt, _plain = pairs[0]
        self.assertEqual(dt, (2026, 4, 15))
        self.assertIn("ResultDataSetPDF", url)
        self.assertTrue(url.startswith("https://www.pmda.go.jp/"))


class TestSplitIfSections(unittest.TestCase):
    def test_strips_leading_page_noise_before_ident(self) -> None:
        text = """
002
1

1. 警告
以下警告

4. 効能又は効果
効能本文

5. 効能又は効果に関連する注意
注意
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        self.assertNotIn("002", d["section_ident"])
        self.assertIn("1. 警告", d["section_ident"])

    def test_basic_headings(self) -> None:
        text = """
1. 警告
以下警告

4. 効能又は効果
効能本文

5. 効能又は効果に関連する注意
注意

17. 臨床成績
臨床

18. 薬効薬理
薬理

19. 有効成分に関する理化学的知見
理化

11. 副作用
副作用本文

12. 臨床検査結果に及ぼす影響
検査
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        self.assertIn("効能本文", d["section_4"])
        self.assertNotIn("4. 効能又は効果", d["section_4"].split("\n")[0])
        self.assertIn("臨床", d["section_17"])
        self.assertIn("薬理", d["section_18"])
        self.assertIn("副作用本文", d["section_11"])

    def test_section_6710(self) -> None:
        text = """
6. 用法及び用量
用法

7. 用法及び用量に関連する注意
用法注意

8. 重要な基本的注意
重要

10. 相互作用
相互作用本文

11. 副作用
副
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        self.assertIn("用法", d["section_6710"])
        self.assertIn("相互作用本文", d["section_6710"])


if __name__ == "__main__":
    unittest.main()
