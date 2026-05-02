"""pmda_if_extract のユニットテスト（ネットワーク不要）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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


class TestSummarizeCards(unittest.TestCase):
    def test_generic_fallback_inn_from_section18_when_sec3_empty(self) -> None:
        sec18 = """18.1 作用機序
カルシトニン遺伝子関連ペプチド(CGRP)は片頭痛の病態生理と関連する神経ペプチドである。
アトゲパントはCGRPの受容体への結合を阻害し、CGRP受容体のシグナル伝達を阻害する。
18.2 CGRP受容体に対する結合親和性
アトゲパントは、ヒトCGRP受容体に親和性を示した。
"""
        sec = {
            "pre_ch4_raw": "",
            "section_ident": "",
            "section_3": "",
            "section_4": "片頭痛発作の発症抑制",
            "section_17": "",
            "section_18": sec18,
            "section_11": "",
            "section_6710": "",
        }
        r = pmda_if_extract.summarize_infographic_cards(
            rss_title="アッヴィ　片頭痛発作の発症抑制薬・アクイプタ錠を発売",
            sections=sec,
        )
        self.assertEqual(r["card_generic"], "アトゲパント")

    def test_generic_strips_salt_suffix_from_section3(self) -> None:
        sec = {
            "pre_ch4_raw": "",
            "section_ident": "",
            "section_3": "3.1 組成\n有効成分 1錠中 ツカチニブ エタノール付加物52.4mg\n",
            "section_4": "化学療法歴のあるHER2陽性の手術不能又は再発乳癌における。",
            "section_17": "",
            "section_18": "",
            "section_11": "",
            "section_6710": "",
        }
        r = pmda_if_extract.summarize_infographic_cards(
            rss_title="ファイザー　HER2陽性乳がん治療薬・ツカイザ錠を発売",
            sections=sec,
        )
        self.assertEqual(r["card_generic"], "ツカチニブ")

    def test_generic_from_prech4_ki_line(self) -> None:
        sec = {
            "pre_ch4_raw": "キ. 基準名：ツカチニブエタノール付加物\nウ. 承認\n1. 警告\n注意",
            "section_ident": "1. 警告\n注意",
            "section_3": "",
            "section_4": "化学療法歴のあるHER2陽性の手術不能又は再発乳癌における。",
            "section_17": "",
            "section_18": "",
            "section_11": "",
            "section_6710": "",
        }
        r = pmda_if_extract.summarize_infographic_cards(
            rss_title="ファイザー　HER2陽性乳がん治療薬・ツカイザ錠を発売",
            sections=sec,
        )
        self.assertIn("ツカチニブ", r["card_generic"])

    def test_cards_from_title_and_sections(self) -> None:
        sec = {
            "pre_ch4_raw": "",
            "section_ident": "薬効分類：抗悪性腫瘍剤\nその他",
            "section_3": "3.1 組成\n有効成分（ツカチニブエタノール付加物）\n",
            "section_4": "化学療法歴のあるHER2陽性の手術不能又は再発乳癌における効果。詳細は併用参照。",
            "section_17": "",
            "section_18": "",
            "section_11": "",
            "section_6710": "",
        }
        r = pmda_if_extract.summarize_infographic_cards(
            rss_title="ファイザー　HER2陽性乳がん治療薬・ツカイザ錠を発売",
            sections=sec,
        )
        self.assertIn("ツカイザ", r["card_brand"])
        self.assertIn("ツカチニブ", r["card_generic"])
        self.assertIn("抗悪性", r["card_yakka"])
        self.assertIn("乳癌", r["card_efficacy"])

    def test_generic_from_glued_section3_one_line(self) -> None:
        """PDF 抽出で 3.1 組成〜添加剤が 1 行に潰れる場合でも一般名を拾う。"""
        sec3 = (
            "3.1 組成 販売名 ツカイザ錠50mg ツカイザ錠150mg "
            "有効成分 1錠中 ツカチニブ エタノール付加物52.4mg "
            "(ツカチニブとして50mg) 1錠中 ツカチニブ エタノール付加物157.2mg "
            "(ツカチニブとして150mg) 添加剤 コポビドン 3.2 製剤の性状 販売名"
        )
        g = pmda_if_extract._generic_from_section3(sec3)
        self.assertIn("ツカチニブ", g)
        self.assertIn("エタノール付加物", g)


class TestStructureSection17(unittest.TestCase):
    def test_trials_split_her2climb_like(self) -> None:
        sec17 = """17.1 有効性及び安全性に関する試験
17.1.1 海外第II相試験[HER2CLIMB(ONT-380-206)試験]
周術期若しくは手術不能又は再発乳癌に対する化学療法として、612例を対象として二重盲検試験を実施した。
主要評価項目である無増悪生存期間の中央値は本剤群で7.8ヵ月、対照群で5.6ヵ月であった。
本剤群404例において、393例に副作用が認められた。主な副作用は下痢であった。
注1)注釈ダミー
17.1.2 国際共同第II相試験[HER2CLIMB-03(MK-7119-001)試験]
66例を対象として非盲検試験を実施した。
主要評価項目である奏効率は35.4%であった。
副作用は日本人集団53例中53例に認められた。
注1)注釈
"""
        d = pmda_if_extract.structure_section17_trials(sec17)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertGreaterEqual(len(d["trials"]), 2)
        h0 = d["trials"][0]["heading"]
        self.assertIn("HER2CLIMB", h0)
        self.assertIn("612", d["trials"][0]["design"])
        self.assertIn("7.8", d["trials"][0]["result"])
        self.assertIn("副作用", d["trials"][0]["ae_note"])
        self.assertIn("35.4", d["trials"][1]["result"])
        # 図解用の構造化フィールド
        t0 = d["trials"][0]
        self.assertTrue(t0.get("design_lines"))
        self.assertIn("612", t0["design_lines"][0])
        fr0 = t0.get("efficacy_fragments") or []
        em_vals = [x["t"] for x in fr0 if x.get("em")]
        self.assertIn("7.8", em_vals)
        self.assertIn("5.6", em_vals)
        self.assertTrue(any("下痢" in x for x in (t0.get("ae_items") or [])))
        t1 = d["trials"][1]
        fr1 = t1.get("efficacy_fragments") or []
        self.assertIn("35.4", [x["t"] for x in fr1 if x.get("em")])
        self.assertNotIn("17.1.1", t0.get("heading_display", ""))
        self.assertIn("HER2CLIMB", t0.get("heading_display", ""))
        self.assertEqual("無増悪生存期間の中央値", t0.get("primary_endpoint_label", ""))
        self.assertEqual("奏効率", t1.get("primary_endpoint_label", ""))
        self.assertTrue(t0.get("population_lead"))
        self.assertIn("612", t0.get("population_lead", ""))
        rc0 = t0.get("result_compare")
        self.assertIsNotNone(rc0)
        assert rc0 is not None
        self.assertEqual(rc0.get("variant"), "pfs_medians")
        self.assertEqual(rc0.get("b1", {}).get("value"), "7.8")
        self.assertEqual(rc0.get("b2", {}).get("value"), "5.6")
        rc1 = t1.get("result_compare")
        self.assertIsNotNone(rc1)
        assert rc1 is not None
        self.assertEqual(rc1.get("variant"), "orr_pct")
        self.assertEqual(rc1.get("b1", {}).get("value"), "35.4")

    def test_result_compare_three_pfs_full_tail(self) -> None:
        r = (
            "主要評価項目である無増悪生存期間の中央値は本剤群で7.8ヵ月、対照群で5.6ヵ月であり、"
            "ハザード比は0.54(95%信頼区間:0.42,0.71、層別ログランク検定p<0.00001、有意水準（両側)0.05)で、"
            "本剤群で統計学的に有意な延長が認められた。"
        )
        cmp = pmda_if_extract._result_compare_three_from_sec17(r)
        self.assertIsNotNone(cmp)
        assert cmp is not None
        lines = cmp["b3"]["lines"]
        self.assertTrue(any("ハザード比" in x for x in lines))
        self.assertTrue(any("CI" in x for x in lines))
        self.assertTrue(any("p" in x.lower() for x in lines))

    def test_lead_and_bullets_from_paragraph(self) -> None:
        a, b = pmda_if_extract._lead_and_bullets_from_paragraph(
            "一文目である。二文目である。三文目。"
        )
        self.assertIn("一文目", a)
        self.assertEqual(len(b), 2)
        self.assertIn("二文目", b[0])
        self.assertIn("三文目", b[1])
        one, rest = pmda_if_extract._lead_and_bullets_from_paragraph("単文のみ。")
        self.assertEqual(one, "単文のみ。")
        self.assertEqual(rest, [])

    def test_sec17_colon_ci_in_result(self) -> None:
        result = (
            "主要評価項目である無増悪生存期間の中央値は本剤群で7.8ヵ月、対照群で5.6ヵ月であり、"
            "ハザード比は0.54(95%信頼区間:0.42,0.71、層別ログランク検定p<0.00001、有意水準（両側)0.05)で、"
            "本剤群で統計学的に有意な延長が認められた。"
        )
        fr = pmda_if_extract._efficacy_fragments_from_sec17(result)
        em = [x["t"] for x in fr if x.get("em")]
        self.assertIn("0.54", em)
        self.assertIn("0.42", em)
        self.assertIn("0.71", em)

    def test_sec17_design_two_sentences_pop_prot(self) -> None:
        sec17 = """17.1.1 海外第II相試験[HER2CLIMB(ONT-380-206)試験]
患者612例を対象として登録した。二重盲検で本剤群と対照群に割り付けた。
主要評価項目である無増悪生存期間の中央値は本剤群で7.8ヵ月、対照群で5.6ヵ月であった。
副作用は下痢であった。
"""
        d = pmda_if_extract.structure_section17_trials(sec17)
        self.assertIsNotNone(d)
        assert d is not None
        t0 = d["trials"][0]
        self.assertIn("612", t0.get("population_line", ""))
        self.assertIn("二重盲検", t0.get("protocol_line", ""))

    def test_trials_split_release_paren_heading_after_nfkc(self) -> None:
        """試験名が ] で終わらない（試験) / :RELEASE(...) / :3101-...試験）でも見出しを切れる。"""
        sec17 = (
            "17.1 有効性及び安全性に関する試験\n"
            "17.1.1 国内第II/III相試験:RELEASE(M22-056試験)\n"
            "18歳以上の患者523例を対象とした二重盲検試験を実施した。\n"
            "主要評価項目である投与開始12週間における平均MMDのベースラインからの変化量は表1の通りであった。\n"
            "副作用発現頻度は10.3%であった。\n"
            "17.1.2 国際共同第III相試験:PROGRESS(3101-303-002試験)\n"
            "慢性片頭痛患者773例を対象とした。\n"
            "主要評価項目である平均MMDのベースラインからの変化量は表2の通りであった。\n"
            "副作用発現頻度は20.2%であった。\n"
            "17.1.3 国内第III相長期投与試験:3101-306-002試験\n"
            "長期投与試験の本文。\n"
            "主要評価項目である52週間のMMDの推移を図1に示した。\n"
            "副作用発現頻度は16.7%であった。\n"
        )
        d = pmda_if_extract.structure_section17_trials(sec17)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(len(d["trials"]), 3)
        self.assertIn("RELEASE", d["trials"][0]["heading_display"])
        self.assertIn("PROGRESS", d["trials"][1]["heading_display"])
        self.assertIn("3101-306-002", d["trials"][2]["heading_display"])
        self.assertIn(
            "平均MMDのベースラインからの変化量",
            d["trials"][0].get("primary_endpoint_label", ""),
        )
        self.assertIn("副作用発現頻度", d["trials"][0].get("ae_note", ""))


class TestStructureSection18(unittest.TestCase):
    def test_moa_intro_only_from_tucatinib_like_text(self) -> None:
        sec18 = """18.1 作用機序 
ツカチニブは、HER2のキナーゼ活性を阻害することにより、腫瘍の
増殖を抑制すると考えられている18)。 
18.2 抗腫瘍作用 
18.2.1 in vitro 
ツカチニブは、HER2陽性のヒト乳癌由来細胞株(BT-474細胞株等)
に対して、増殖抑制作用を示した19)。 
18.2.2 in vivo 
ツカチニブは、BT-474細胞株を皮下移植した重症複合型免疫不全マ
ウスに対して、腫瘍増殖抑制作用を示した。また、ツカチニブ単独
及びトラスツズマブ単独と比較して、ツカチニブとトラスツズマブ
との併用では腫瘍増殖抑制作用の増強が認められた20)。
"""
        d = pmda_if_extract.structure_section18_moa(sec18)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertIn("HER2", d["intro"])
        self.assertEqual(d["cards"], [])
        self.assertNotIn("BT-474", d["intro"])
        self.assertNotIn("intro_note", d)

    def test_moa_stops_at_generic_18_2_not_only_antitumor(self) -> None:
        """18.2 が抗腫瘍作用以外でも 18.1 で切れ、作用文を優先する。"""
        sec18 = """18.1 作用機序
カルシトニン遺伝子関連ペプチド(CGRP)は片頭痛の病態生理と関連する神経ペプチドである。
アトゲパントはCGRPの受容体への結合を阻害し、CGRP受容体のシグナル伝達を阻害する23)。
18.2 CGRP受容体に対する結合親和性
アトゲパントは、ヒトCGRP受容体に親和性を示し、そのKi値は15–26pmol/Lであった(in vitro)。
"""
        d = pmda_if_extract.structure_section18_moa(sec18)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertIn("阻害", d["intro"])
        self.assertIn("CGRP", d["intro"])
        self.assertNotIn("結合親和性", d["intro"])
        self.assertNotIn("Ki値", d["intro"])
        self.assertLessEqual(len(d["intro"]), 330)

    @patch.object(pmda_if_extract, "_MOA_T_HARD", 80)
    def test_moa_truncation_adds_guidance_note(self) -> None:
        """ハード上限で切った場合に誘導文が付く（テスト用に上限を一時的に下げる）。"""
        pad = "ア" * 120
        sec18 = f"""18.1 作用機序
本剤はHER2のキナーゼ活性を阻害することにより腫瘍の増殖を抑制する{pad}。
18.2 抗腫瘍作用
次の文。
"""
        d = pmda_if_extract.structure_section18_moa(sec18)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertIn("intro_note", d)
        self.assertIn("18.1 作用機序", d["intro_note"])
        self.assertLessEqual(len(d["intro"]), 80)


class TestStructureSection11(unittest.TestCase):
    def test_summary_none_without_11_2(self) -> None:
        s = (
            "次の副作用があらわれることがあるので観察を十分に行い異常が認められた場合には投与を中止するなど適切な処置を行うこと。\n"
            "11.1 重大な副作用\n"
            "11.1.1 重度の下痢(10.6%)\n"
            "[7.3参照]\n"
        )
        self.assertIsNone(pmda_if_extract.structure_section11_summary(s))

    def test_two_column_like_snippet(self) -> None:
        s = """
次の副作用があらわれることがあるので、観察を十分に行い、異常が認められた場合には投与を中止するなど適切な処置を行うこと。

11.1 重大な副作用

11.1.1 重度の下痢(10.6%)
[7.3参照]

11.1.2 肝機能障害
高ビリルビン血症(21.9%)、AST増加(20.0%)等を伴う肝機能障害があらわれることがある。[7.3参照]

11.2 その他の副作用
 5%以上 1%以上~5%未満 1%未満

代謝及び栄養障害
食欲減退(20.9%)、低カリウム血症
低血糖

胃腸障害
下痢(72.6%)、悪心(52.1%)、嘔吐(25.3%)
"""
        d = pmda_if_extract.structure_section11_summary(s)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertGreaterEqual(len(d["serious_items"]), 2)
        self.assertEqual(d["serious_items"][0]["num"], "1")
        self.assertIn("下痢", d["serious_items"][0]["heading"])
        symptoms = [x["symptom"] for x in d["other_items"]]
        self.assertTrue(any("72.6" in t for t in symptoms))
        self.assertTrue(any(x["soc"] == "胃腸障害" for x in d["other_items"]))
        self.assertIn("5%以上", d["other_title"])

    def test_aquipta_style_freq_band_table(self) -> None:
        """11.2 が「1%以上 / 0.1〜1%未満」列表で括弧%がない様式（アクイプタ等）。"""
        s = """
次の副作用があらわれることがあるので、観察を十分に行い、異常が認められた場合には投与を中止するなど適切な処置を行うこと。

11.1 重大な副作用

11.1.1 過敏症反応(頻度不明)
アナフィラキシー等。

11.2 その他の副作用

1%以上 0.1～1%未満

消化器
悪心、便秘 —

全身症状
— 疲労

代謝及び栄養障害
食欲減退 —

神経系障害
傾眠 —

臨床検査値
体重減少、ALT/AST増加 —

皮膚及び皮下組織障害
— そう痒症
"""
        d = pmda_if_extract.structure_section11_summary(s)
        self.assertIsNotNone(d)
        assert d is not None
        symptoms = [x["symptom"] for x in d["other_items"]]
        self.assertTrue(any("悪心" in t for t in symptoms))
        self.assertTrue(any("便秘" in t for t in symptoms))
        self.assertTrue(any("疲労" in t for t in symptoms))
        self.assertTrue(any("傾眠" in t for t in symptoms))
        self.assertTrue(any("そう痒症" in t for t in symptoms))
        self.assertTrue(any("1%以上" in t for t in symptoms))
        self.assertTrue(any("0.1" in t for t in symptoms))

    def test_aquipta_pdf_like_single_line_table_rows(self) -> None:
        """pypdf 抽出で「消化器 悪心、便秘 -」のように器官とセルが同一行。"""
        s = """
次の副作用があらわれることがあるので、観察を十分に行い、異常が認められた場合には投与を中止するなど適切な処置を行うこと。

11.1 重大な副作用

11.1.1 過敏症反応(頻度不明)
アナフィラキシー等。

11.2 その他の副作用

1%以上 0.1~1%未満
消化器 悪心、便秘 -
全身症状 - 疲労
代謝及び栄養障害 食欲減退 -
神経系障害 傾眠 -
臨床検査値 体重減少、ALT/AST増加 -
皮膚及び皮下組織障害 - そう痒症
14. 適用上の注意
交付時の注意
"""
        d = pmda_if_extract.structure_section11_summary(s)
        self.assertIsNotNone(d)
        assert d is not None
        symptoms = [x["symptom"] for x in d["other_items"]]
        self.assertTrue(any("悪心" in t for t in symptoms))
        self.assertTrue(any("疲労" in t for t in symptoms))
        self.assertTrue(any("そう痒症" in t for t in symptoms))


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

    def test_section_3_extracted(self) -> None:
        text = """
3. 組成・性状
3.1 組成
有効成分（テスト成分水和物）

4. 効能又は効果
効能

5. 効能又は効果に関連する注意
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        self.assertIn("テスト成分", d.get("section_3", ""))

    def test_section_3_when_heading_glued_to_31(self) -> None:
        text = """
1. 警告
x
3. 組成・性状3.1 組成
有効成分（ツカチニブエタノール付加物）
添加剤 ダミー

4. 効能又は効果
効能本文

5. 効能又は効果に関連する注意
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        self.assertIn("ツカチニブ", d.get("section_3", ""))

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

    def test_section11_stops_at_14_when_chapter12_missing(self) -> None:
        """12 章見出しが PDF に無いとき 14 章で打ち切り、17 章が混ざらないこと。"""
        text = """
11. 副作用
11.2 その他の副作用
表の本文
14. 適用上の注意
交付注意
17. 臨床成績
試験
18. 薬効薬理
機序
"""
        d = pmda_if_extract.split_if_sections(text, max_len=5000)
        s11 = d["section_11"]
        self.assertIn("表の本文", s11)
        self.assertNotIn("17.", s11)
        self.assertNotIn("臨床成績", s11)


class TestStructureDosageMemo(unittest.TestCase):
    def test_tucatinib_like_multiline(self) -> None:
        raw = """
6. 用法及び用量
トラスツズマブ(遺伝子組換え)及びカペシタビンとの併用におい
て、通常、成人にはツカチニブとして1回300mgを1日2回経口投与す
る。なお、患者の状態により適宜減量する。

7. 用法及び用量に関連する注意
7.1 本剤単独投与での有効性及び安全性は確立していない。
7.2 重度の肝機能障害(Child-Pugh分類C)のある患者では、本剤の開
始用量は1回200mgを1日2回とすること。
7.4 強いCYP2C8阻害剤と併用する場合、本剤の開始用量は1回100mgを
1日2回とすること。
7.5 本剤とトラスツズマブ及びカペシタビンを併用する際のカペシタビンの用法及び用量は以下のとおりとすること。

10. 相互作用
10.1 併用禁忌
本文
"""
        r = pmda_if_extract.structure_dosage_memo(raw)
        self.assertIsNotNone(r)
        bullets = r["bullets"]
        self.assertTrue(any("300mg" in b and "ツカチニブ" in b for b in bullets))
        self.assertTrue(any("200mg" in b and "7.2" in b for b in bullets))
        self.assertTrue(any("CYP2C8" in b and "100mg" in b for b in bullets))
        self.assertTrue(any("7.1" in b for b in bullets))
        self.assertTrue(any("7.5" in b and "カペシタビン" in b for b in bullets))

    def test_too_short_returns_none(self) -> None:
        self.assertIsNone(
            pmda_if_extract.structure_dosage_memo("6. 用法及び用量\n用法\n")
        )

    def test_sec7_header_glued_to_sec6_paragraph(self) -> None:
        """PDF 結合で「7. 用法…」が6章本文と同一行に付くと従来の ^7 境界では標準用法が落ちる。"""
        raw = """
6. 用法及び用量
トラスツズマブ(遺伝子組換え)及びカペシタビンとの併用において、通常、成人にはツカチニブとして1回300mgを1日2回経口投与する。なお、患者の状態により適宜減量する。7. 用法及び用量に関連する注意
7.1 本剤単独投与での有効性及び安全性は確立していない。
7.2 重度の肝機能障害(Child-Pugh分類C)のある患者では、本剤の開始用量は1回200mgを1日2回とすること。
7.4 強いCYP2C8阻害剤と併用する場合、本剤の開始用量は1回100mgを1日2回とすること。
7.5 本剤とトラスツズマブ及びカペシタビンを併用する際のカペシタビンの用法及び用量は以下のとおりとすること。

10. 相互作用
10.1 x
"""
        r = pmda_if_extract.structure_dosage_memo(raw)
        self.assertIsNotNone(r)
        self.assertTrue(any("300mg" in b and "ツカチニブ" in b for b in r["bullets"]))

    def test_fullwidth_digits_in_sec6(self) -> None:
        raw = """
6. 用法及び用量
トラスツズマブ及びカペシタビンとの併用において、通常、成人にはツカチニブとして１回３００ｍｇを１日２回経口投与する。なお、患者の状態により適宜減量する。

7. 用法及び用量に関連する注意
7.1 本剤単独投与での有効性及び安全性は確立していない。

10. 相互作用
10.1 x
y
"""
        r = pmda_if_extract.structure_dosage_memo(raw)
        self.assertIsNotNone(r)
        self.assertTrue(any("300mg" in b for b in r["bullets"]))

    def test_sec6_heading_missing_excerpt_starts_with_body(self) -> None:
        """PDF 境界で「6. 用法及び用量」が抜粋に含まれず本文から始まる場合。"""
        raw = """
トラスツズマブ(遺伝子組換え)及びカペシタビンとの併用において、通常、成人にはツカチニブとして1回300mgを1日2回経口投与する。なお、患者の状態により適宜減量する。

7. 用法及び用量に関連する注意
7.1 本剤単独投与での有効性及び安全性は確立していない。
7.2 重度の肝機能障害(Child-Pugh分類C)のある患者では、本剤の開始用量は1回200mgを1日2回とすること。

10. 相互作用
10.1 x
y
"""
        r = pmda_if_extract.structure_dosage_memo(raw)
        self.assertIsNotNone(r)
        self.assertTrue(any("300mg" in b and "ツカチニブ" in b for b in r["bullets"]))

    def test_format_section_6710_fallback_joins_soft_breaks(self) -> None:
        raw = "通常、成人には\nアトゲパントとして"
        out = pmda_if_extract.format_section_6710_fallback(raw)
        self.assertIn("通常、成人には", out)
        self.assertIn("アトゲパントとして", out)
        self.assertNotIn("\nアトゲパント", out)

    def test_structure_dosage_memo_atogepant_like(self) -> None:
        """「1回」が用量直前に無い表記・腎7.2・CYP3A7.3・OATP7.4 を拾う。"""
        raw = """
6. 用法及び用量
通常、成人にはアトゲパントとして60mgを1日1回経口投与する。

7. 用法及び用量に関連する注意
7.1 本剤投与中は症状の経過を十分に観察し、以下のとおり投与継続の可否を考慮すること。
7.2 重度の腎機能障害患者及び末期腎不全患者(クレアチニンクリアンスが30mL/min未満)では、本剤10mgを1日1回経口投与すること。
7.3 強いCYP3A阻害剤と併用する場合は、本剤10mgを1日1回経口投与すること。
7.4 OATP阻害剤と併用する場合は、本剤30mgを1日1回経口投与すること。

10. 相互作用
10.2 併用注意(併用に注意すること)
強いCYP3A阻害剤 本剤の副作用が増強されるおそれがある。
"""
        r = pmda_if_extract.structure_dosage_memo(raw)
        self.assertIsNotNone(r)
        joined = " ".join(r["bullets"])
        self.assertIn("60mg", joined)
        self.assertIn("アトゲパント", joined)
        self.assertIn("7.2", joined)
        self.assertIn("10mg", joined)
        self.assertIn("CYP3A", joined)
        self.assertIn("OATP", joined)
        self.assertIn("30mg", joined)
        self.assertTrue(any("観察" in b or "投与継続" in b for b in r["bullets"]))


if __name__ == "__main__":
    unittest.main()
