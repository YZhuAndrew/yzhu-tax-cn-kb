#!/usr/bin/env python3
"""Offline unit tests for tax_versions.py — pure chain-building logic, no network."""
import json
import os
import sys
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from tax_versions import (
    parse_title, normalize_for_match, belongs_to_family, is_central_authority,
    extract_repeal_refs, build_edges, find_current, assess_effect, clean_title,
)

PASS = "PASS"
FAIL = "FAIL"


def make_record(title, status, publish, effective="", bbbs=None, doc_type_hint=None):
    parsed = parse_title(title)
    return {
        "bbbs": bbbs or title,  # 用标题当 id,测试里足够
        "title": title,
        "base": parsed["base"],
        "doc_type": parsed["doc_type"],
        "revision": parsed["revision"],
        "publish_date": publish,
        "effective_date": effective,
        "status_code": status,
        "issuing_authority": "国务院",
        "category": "行政法规",
        "url": f"https://flk.npc.gov.cn/detail?id={bbbs or title}",
    }


def test_parse_title():
    """标题解析:前缀剥离、文书类型、修正括号。"""
    tests = [
        ("中华人民共和国增值税法", "增值税", "法", None),
        ("中华人民共和国增值税暂行条例", "增值税", "暂行条例", None),
        ("中华人民共和国税收征收管理法实施细则", "税收征收管理", "实施细则", None),
        ("中华人民共和国发票管理办法", "发票", "管理办法", None),
        ("中华人民共和国企业所得税法（2018年修正）", "企业所得税", "法", "2018"),
        ("中华人民共和国宪法（2018年修正文本）", "宪法", "", "2018"),  # 单字保护:宪法不拆
        ("中华人民共和国契税法", "契税", "法", None),
    ]
    passed = 0
    for title, base, doc_type, rev_year in tests:
        p = parse_title(title)
        ok = (p["base"] == base and p["doc_type"] == doc_type
              and p["revision_year"] == rev_year)
        status = PASS if ok else f"{FAIL} (got {p})"
        print(f"  [{status}] {title} -> {p['base']}/{p['doc_type']}/{p['revision_year']}")
        passed += ok
    return passed, len(tests)


def test_clean_and_match():
    """高亮清洗与匹配归一化。"""
    tests = [
        ("中华人民共和国<em class='highlight'>增值税</em>暂行条例",
         "中华人民共和国增值税暂行条例"),
        ("企业所得税法&amp;实施", "企业所得税法&实施"),
    ]
    passed = 0
    for raw, expected in tests:
        got = clean_title(raw)
        ok = got == expected
        print(f"  [{'PASS' if ok else FAIL}] clean_title -> {got}")
        passed += ok
    # 归一化:修正括号不影响匹配
    a = normalize_for_match("中华人民共和国企业所得税法（2018年修正）")
    b = normalize_for_match("中华人民共和国企业所得税法")
    ok = a == b == "企业所得税法"
    print(f"  [{'PASS' if ok else FAIL}] normalize revision-insensitive: {a} == {b}")
    passed += ok
    return passed, len(tests) + 1


def test_family_filter():
    """族归属与中央机关过滤。"""
    cases = [
        ("中华人民共和国增值税暂行条例实施细则", "增值税", True),
        ("上海市增值税征收管理办法", "增值税", False),   # base 不同(上海市增值税...)
        ("全国人民代表大会常务委员会关于外商投资企业和外国企业适用增值税、消费税、营业税等税收暂行条例的决定", "增值税", False),
    ]
    passed = 0
    for title, base, expected in cases:
        got = belongs_to_family(title, base)
        ok = got == expected
        print(f"  [{'PASS' if ok else f'{FAIL} (got {got})'}] belongs: {title[:30]}...")
        passed += ok
    auth_cases = [
        ("国务院", True), ("全国人民代表大会常务委员会", True),
        ("财政部", True), ("国家税务总局", True),
        ("广东省人民代表大会常务委员会", False), ("深圳市人民代表大会", False),
    ]
    for name, expected in auth_cases:
        got = is_central_authority(name)
        ok = got == expected
        print(f"  [{'PASS' if ok else f'{FAIL} (got {got})'}] authority: {name}")
        passed += ok
    return passed, len(cases) + len(auth_cases)


def test_extract_repeal_refs():
    """从真实契税法施行条款提取废止关联。"""
    paragraphs = [
        "第十五条　纳税人应当在依法办理土地、房屋权属登记手续前申报缴纳契税。",
        "第十六条　本法自2021年9月1日起施行。1997年7月7日国务院发布的《中华人民共和国契税暂行条例》同时废止。",
        "附则条款与废止无关的内容。",
    ]
    refs = extract_repeal_refs(paragraphs, own_title="中华人民共和国契税法")
    ok = (refs["effective"] == "2021-09-01"
          and refs["repealed"] == ["中华人民共和国契税暂行条例"]
          and "同时废止" in refs["clause"])
    print(f"  [{'PASS' if ok else FAIL}] effective={refs['effective']} "
          f"repealed={refs['repealed']}")
    # 自身标题不应出现在废止名单里
    refs2 = extract_repeal_refs(
        ["本法自2026年1月1日起施行。《中华人民共和国增值税法》同时废止。"],
        own_title="中华人民共和国增值税法")
    ok2 = refs2["repealed"] == [] and refs2["effective"] == "2026-01-01"
    print(f"  [{'PASS' if ok2 else FAIL}] self-title excluded: {refs2['repealed']}")
    return (1 if ok else 0) + (1 if ok2 else 0), 2


def test_build_edges():
    """文本确认边优先于推断边;无文本时降级推断。"""
    # 场景1:契税 — 文本确认
    records = [
        make_record("中华人民共和国契税暂行条例", 1, "1997-07-07", "1997-10-01", "old1"),
        make_record("中华人民共和国契税法", 3, "2020-08-11", "2021-09-01", "new1"),
    ]
    text_refs = {"new1": {"effective": "2021-09-01",
                          "repealed": ["中华人民共和国契税暂行条例"],
                          "clause": "第十六条 本法自2021年9月1日起施行。……同时废止。"}}
    edges = build_edges(records, text_refs)
    ok = (len(edges) == 1 and edges[0]["basis"] == "text"
          and edges[0]["from_bbbs"] == "old1" and edges[0]["to_bbbs"] == "new1"
          and edges[0]["effective_date"] == "2021-09-01")
    print(f"  [{'PASS' if ok else f'{FAIL} (got {edges})'}] text-verified edge")
    passed = 1 if ok else 0

    # 场景2:无文本 → 推断边
    edges2 = build_edges(records, {})
    ok2 = (len(edges2) == 1 and edges2[0]["basis"] == "inferred"
           and edges2[0]["to_bbbs"] == "new1")
    print(f"  [{'PASS' if ok2 else f'{FAIL} (got {edges2})'}] inferred edge fallback")
    passed += 1 if ok2 else 0

    # 场景3:多旧版本,文本边覆盖一个,其余推断
    records3 = records + [
        make_record("中华人民共和国契税暂行条例（2019年修订）", 2, "2019-03-02", "", "old2"),
    ]
    edges3 = build_edges(records3, text_refs)
    text_edges = [e for e in edges3 if e["basis"] == "text"]
    inferred = [e for e in edges3 if e["basis"] == "inferred"]
    ok3 = len(text_edges) == 1 and len(inferred) == 1
    detail3 = f"text={len(text_edges)}, inferred={len(inferred)}"
    print(f"  [{'PASS' if ok3 else f'{FAIL} ({detail3})'}] mixed edges")
    passed += 1 if ok3 else 0

    # 场景4:同文书类型优先 —— 已修改的法应连到新版法,而非更早公布的实施条例
    records4 = [
        make_record("中华人民共和国环境保护税法", 2, "2016-12-25", "2018-01-01", "e1"),
        make_record("中华人民共和国环境保护税法实施条例", 3, "2017-12-25", "2018-01-01", "e2"),
        make_record("中华人民共和国环境保护税法", 3, "2025-10-28", "2026-01-01", "e3"),
    ]
    edges4 = build_edges(records4, {})
    ok4 = (len(edges4) == 1 and edges4[0]["from_bbbs"] == "e1"
           and edges4[0]["to_bbbs"] == "e3")
    print(f"  [{'PASS' if ok4 else f'{FAIL} (got {edges4})'}] same-doc-type successor preferred")
    passed += 1 if ok4 else 0
    return passed, 4


def test_find_current():
    """当前适用版本:未生效新法施行前,现行条例仍优先;无法轨时取任意 sxx=3。"""
    reg = make_record("中华人民共和国房产税暂行条例", 3, "1986-09-15", "1986-10-01", "a")
    ok = find_current([reg])["bbbs"] == "a"
    print(f"  [{'PASS' if ok else FAIL}] no-law family -> regulation current")
    law = make_record("中华人民共和国房产税法", 4, "2026-07-01", "2027-01-01", "b")
    ok2 = find_current([reg, law])["bbbs"] == "a"
    print(f"  [{'PASS' if ok2 else FAIL}] pending law does NOT displace "
          "effective regulation (施行前旧条例仍适用)")
    law_effective = make_record("中华人民共和国房产税法", 3, "2026-07-01", "2026-09-01", "c")
    ok3 = find_current([reg, law, law_effective])["bbbs"] == "c"
    print(f"  [{'PASS' if ok3 else FAIL}] effective law displaces regulation")
    return (1 if ok else 0) + (1 if ok2 else 0) + (1 if ok3 else 0), 3


def test_assess_effect():
    """效力问答:已废止→后继+依据;现行→可引用。"""
    records = [
        make_record("中华人民共和国增值税暂行条例", 1, "2017-11-19", "", "vat-old"),
        make_record("中华人民共和国增值税法", 3, "2024-12-25", "2026-01-01", "vat-new"),
    ]
    edges = build_edges(records, {"vat-new": {
        "effective": "2026-01-01",
        "repealed": ["中华人民共和国增值税暂行条例"],
        "clause": "第三十八条 本法自2026年1月1日起施行。《中华人民共和国增值税暂行条例》同时废止。"}})
    family = {"base": "增值税", "records": records, "edges": edges,
              "current": find_current(records)}

    r1 = assess_effect("中华人民共和国增值税暂行条例", [family])
    ok = (r1["matched"] and r1["verdict"] == "abolished"
          and r1["successor"]["title"] == "中华人民共和国增值税法"
          and r1["successor"]["basis"] == "文本确认"
          and "同时废止" in r1["successor"]["clause"])
    print(f"  [{'PASS' if ok else f'{FAIL} ({json.dumps(r1, ensure_ascii=False)[:200]})'}] "
          "abolished -> successor with clause")
    passed = 1 if ok else 0

    r2 = assess_effect("增值税法", [family])
    ok2 = (r2["matched"] and r2["verdict"] == "effective"
           and r2["successor"] is None
           and r2["family_current"]["bbbs"] == "vat-new")
    print(f"  [{'PASS' if ok2 else FAIL}] current law -> effective")
    passed += 1 if ok2 else 0

    r3 = assess_effect("中华人民共和国烟草专卖法", [family])
    ok3 = not r3["matched"]
    print(f"  [{'PASS' if ok3 else FAIL}] unknown law -> not matched")
    passed += 1 if ok3 else 0
    return passed, 3


def main():
    print("=" * 60)
    print("tax_versions: Offline Unit Tests")
    print("=" * 60)
    total_p = total_n = 0
    for fn in (test_parse_title, test_clean_and_match, test_family_filter,
               test_extract_repeal_refs, test_build_edges, test_find_current,
               test_assess_effect):
        print(f"\n[Test] {fn.__name__}: {fn.__doc__}")
        p, n = fn()
        total_p += p
        total_n += n
    print("\n" + "=" * 60)
    print(f"Results: {total_p}/{total_n} passed")
    print("=" * 60)
    return 0 if total_p == total_n else 1


if __name__ == "__main__":
    sys.exit(main())
