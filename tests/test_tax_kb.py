#!/usr/bin/env python3
"""Offline unit tests for tax_kb.py — 纯逻辑层 + 内存 SQLite 全链路,无网络。"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from tax_kb import (
    cn2num, segment_law, bigram, fts_query, should_fetch_fulltext,
    snippet, open_db, upsert_law, replace_articles, search_articles, find_law,
)

PASS = "FAIL"
RESULTS = {"pass": 0, "fail": 0}


def check(name, cond, detail=""):
    if cond:
        RESULTS["pass"] += 1
        print(f"  [PASS] {name}")
    else:
        RESULTS["fail"] += 1
        print(f"  [FAIL] {name} {detail}")


def make_hit(id_, title, status_code=3):
    return {
        "id": id_, "title": title, "category": "法律",
        "issuing_authority": "全国人民代表大会", "publish_date": "2020-08-11",
        "effective_date": "2021-09-01", "status_code": status_code,
        "status": {3: "现行有效", 2: "已修改", 1: "已废止"}.get(status_code),
    }


def test_cn2num():
    """中文数字→int,阿拉伯直转,非法 None。"""
    check("十二", cn2num("十二") == 12)
    check("一百零三", cn2num("一百零三") == 103)
    check("十", cn2num("十") == 10)
    check("两千零一", cn2num("两千零一") == 2001)
    check("阿拉伯 42", cn2num("42") == 42)
    check("非法字", cn2num("第X") is None)
    check("空串", cn2num("") is None)


def test_segment_law():
    """章归属/续段并入/附件截断。"""
    paragraphs = [
        "中华人民共和国契税法",
        "第一章 总则",
        "第一条 在中华人民共和国境内转移土地、房屋权属,承受的单位和个人为契税的纳税人。",
        "第二条 本条例所称转移土地、房屋权属是指下列行为:",
        "(一)国有土地使用权出让;",
        "(二)土地使用权转让;",
        "第二章 税率",
        "第三条 契税税率为百分之三至百分之五。",
        "附表:契税税目税率表",
        "表格外的杂项文字不应入条",
    ]
    arts = segment_law(paragraphs)
    check("法条数 3", len(arts) == 3, f"got {len(arts)}")
    check("seq 递增", [a["seq"] for a in arts] == [1, 2, 3])
    check("章归属", arts[0]["chapter"] == "第一章总则" and arts[2]["chapter"] == "第二章税率")
    check("续段并入", "(二)土地使用权转让" in arts[1]["text"])
    check("附件截断", "附表" not in arts[-1]["text"] and "杂项" not in arts[-1]["text"])
    check("文首段落不入条", all("中华人民共和国契税法" not in a["text"] for a in arts))
    check("无条文空列表", segment_law(["某说明文件", "没有条款"]) == [])


def test_bigram():
    """双字切分与 FTS 短语构造。"""
    check("bigram 三字", bigram("增值税") == "增值 值税")
    check("bigram 去空白", bigram("小规模 纳税人") == bigram("小规模纳税人"))
    check("短语包裹", fts_query("小规模纳税人") == '"小规 规模 模纳 纳税 税人"')
    check("单字返回 None", fts_query("税") is None)


def test_should_fetch_fulltext():
    """增量判定五情形。"""
    now = datetime.now()
    fresh = dict(parse_status="ok", fulltext_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                 status_code=3, status_code_at_fetch=3)
    check("新鲜 ok 不抓", should_fetch_fulltext(fresh, False, now) is False)
    check("refresh 强制", should_fetch_fulltext(fresh, True, now) is True)
    check("pending 抓", should_fetch_fulltext(
        dict(fresh, parse_status="pending"), False, now) is True)
    check("no_docx 重试", should_fetch_fulltext(
        dict(fresh, parse_status="no_docx"), False, now) is True)
    old = (now - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
    check("超 TTL 抓", should_fetch_fulltext(
        dict(fresh, fulltext_fetched_at=old), False, now) is True)
    check("状态变化抓", should_fetch_fulltext(
        dict(fresh, status_code=2, status_code_at_fetch=3), False, now) is True)
    check("脏时间戳抓", should_fetch_fulltext(
        dict(fresh, fulltext_fetched_at="not-a-date"), False, now) is True)


def test_db_roundtrip():
    """内存库:upsert 幂等/法条替换/双字检索/过滤/单字 LIKE 兜底。"""
    db = open_db(":memory:")
    law_id = upsert_law(db, make_hit("bbbs-qi", "中华人民共和国契税法"), "契税", "2026-10-20 10:00:00")
    law_id2 = upsert_law(db, make_hit("bbbs-qi", "中华人民共和国契税法", status_code=2),
                         "契税", "2026-10-20 11:00:00")
    check("重复 upsert 同一 id", law_id == law_id2)
    row = db.execute("SELECT * FROM laws WHERE id=?", (law_id,)).fetchone()
    check("状态已更新", row["status_code"] == 2)

    articles = [
        {"seq": 1, "chapter": "第一章总则", "article_no": "第一条",
         "text": "在中华人民共和国境内转移土地、房屋权属,承受的单位和个人为契税的纳税人。"},
        {"seq": 3, "chapter": "第二章税率", "article_no": "第三条",
         "text": "契税税率为百分之三至百分之五。"},
    ]
    replace_articles(db, law_id, articles)
    replace_articles(db, law_id, articles)  # 整体替换应幂等,不得翻倍
    n = db.execute("SELECT COUNT(*) FROM articles WHERE law_id=?", (law_id,)).fetchone()[0]
    check("法条替换幂等", n == 2, f"got {n}")

    hits = search_articles(db, "房屋权属")  # 原文连续出现"土地、房屋权属"
    check("双字短语命中", len(hits) == 1 and hits[0]["seq"] == 1)
    check("跨标点不误报", search_articles(db, "土地使用权") == [])  # 原文无此连续串
    check("未命中不误报", search_articles(db, "环境保护税法") == [])
    check("税族过滤", search_articles(db, "契税", tax="增值税") == [])
    check("法规名过滤", search_articles(db, "契税", law_title="增值税") == []
          and len(search_articles(db, "契税", law_title="契税法")) >= 1)
    check("单字 LIKE 兜底", len(search_articles(db, "屋")) == 1)

    check("bbbs 精确找法", find_law(db, "bbbs-qi")["id"] == law_id)
    check("标题模糊找法", find_law(db, "契税法")["id"] == law_id)

    snip = snippet(articles[0]["text"], "纳税人")
    check("snippet 含查询词", "纳税人" in snip)
    db.close()


def test_snippet_long():
    """长文摘要截断带省略号。"""
    text = "本条是关于税收征收管理的特别规定," * 30
    s = snippet(text, "特别规定")
    check("长文截断", len(s) < 120 and "特别规定" in s)


def main():
    tests = [test_cn2num, test_segment_law, test_bigram,
             test_should_fetch_fulltext, test_db_roundtrip, test_snippet_long]
    for t in tests:
        print(f"[Test] {t.__name__}")
        t()
    print("─" * 58)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"Results: {RESULTS['pass']}/{total} passed")
    return RESULTS["fail"]


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
