#!/usr/bin/env python3
"""
www.casc.org.cn — 财政部会计准则委员会静态抓取模块。

页面为静态 .shtml，无需浏览器渲染：
  栏目列表:  https://www.casc.org.cn/{column}/
  文章 URL:  //www.casc.org.cn/{YYYY}/{MMDD}/{ID}.shtml（协议相对地址，
             日期从 URL 解析）

Usage:
  python casc_org.py            # 抓 4 个栏目第 1 页
  python casc_org.py --json
"""

import argparse
import json
import re
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.casc.org.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 15

COLUMNS = {
    "zzfbhjzzfb": "企业会计准则",        # 准则正文发布
    "zzfbkcxplzzfb": "可持续披露准则",  # 企业可持续披露准则
    "gztz": "工作通知",
    "qykjzzjd": "准则解读",
}

# 文章链接：//www.casc.org.cn/2026/0805/284652.shtml（相对/绝对/协议相对均匹配）
_ITEM_RE = re.compile(
    r'<a[^>]*href="(?:(?:https?:)?//www\.casc\.org\.cn)?/(\d{4})/(\d{4})/(\d+)\.shtml"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    return _TAG_RE.sub("", value or "").strip()


def fetch_column(column: str) -> list:
    """抓取一个栏目第 1 页的条目（各栏目更新频率低，首页足够覆盖近期动态）。"""
    url = f"{BASE_URL}/{column}/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            return []
        r.encoding = r.apparent_encoding or "utf-8"
    except requests.RequestException:
        return []

    records = []
    seen = set()
    for year, mmdd, art_id, title_raw in _ITEM_RE.findall(r.text):
        title = _clean(title_raw)
        if not title or art_id in seen:
            continue
        seen.add(art_id)
        records.append({
            "title": title,
            "url": f"{BASE_URL}/{year}/{mmdd}/{art_id}.shtml",
            "date": f"{year}-{mmdd[:2]}-{mmdd[2:4]}",
            "source": "www.casc.org.cn",
            "column": COLUMNS.get(column, column),
        })

    records.sort(key=lambda x: x["date"], reverse=True)
    return records


def fetch_all() -> list:
    """抓取全部栏目并按日期倒序合并。"""
    records = []
    for column in COLUMNS:
        records.extend(fetch_column(column))
        time.sleep(0.3)
    records.sort(key=lambda x: x["date"], reverse=True)
    return records


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="会计准则委员会栏目抓取")
    p.add_argument("--column", choices=list(COLUMNS), help="只抓指定栏目")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    records = fetch_column(args.column) if args.column else fetch_all()

    if args.json:
        print(json.dumps({"total": len(records), "results": records},
                         ensure_ascii=False, indent=2))
        return

    print(f"🏛️ 准则委 共 {len(records)} 条")
    for item in records:
        print(f"  📋 [{item['column']}] {item['title']}")
        print(f"     {item['date']} | {item['url']}")


if __name__ == "__main__":
    main()
