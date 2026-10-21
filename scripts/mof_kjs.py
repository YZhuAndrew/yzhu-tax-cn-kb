#!/usr/bin/env python3
"""
kjs.mof.gov.cn — 财政部会计司（会计准则、制度文件）静态抓取模块。

页面为政府 CMS 静态发布，无需浏览器渲染：
  栏目列表:  https://kjs.mof.gov.cn/{column}/          （第 1 页）
             https://kjs.mof.gov.cn/{column}/index_{N}.htm （第 N+1 页）
  文章 URL:  ./{YYYYMM}/t{YYYYMMDD}_{ID}.htm （日期可直接从 URL 解析）

注意：mof.gov.cn 证书链在 Python 下校验失败，需 verify=False
（与仓库 tax_search.py 的 VERIFY_SSL 策略一致）。

Usage:
  python mof_kjs.py                 # 抓 3 个栏目第 1 页
  python mof_kjs.py --pages 2 --json
"""

import argparse
import json
import re
import time
from urllib.parse import urljoin

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://kjs.mof.gov.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 15

COLUMNS = {
    "zhengcefabu": "政策发布",      # 会计准则/制度正文
    "gongzuotongzhi": "工作通知",  # 征求意见稿、备案名单等
    "zhengcejiedu": "政策解读",    # 答记者问、解读问答
}

# 文章链接：./202608/t20260810_3995181.htm
_ITEM_RE = re.compile(
    r'<a[^>]*href="(\./(\d{6})/t(\d{8})_\d+\.htm)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    return _TAG_RE.sub("", value or "").strip()


def _norm_date(yyyymm: str, dddddd: str) -> str:
    """'202608' + '20260810' → '2026-08-10'（以 URL 为准，比页面文本可靠）。"""
    if len(dddddd) == 8:
        return f"{dddddd[:4]}-{dddddd[4:6]}-{dddddd[6:8]}"
    if len(yyyymm) == 6:
        return f"{yyyymm[:4]}-{yyyymm[4:6]}"
    return ""


def fetch_column(column: str, pages: int = 1) -> list:
    """抓取一个栏目的列表条目，返回按日期倒序的记录。"""
    records = []
    seen = set()
    for page in range(pages):
        if page == 0:
            url = f"{BASE_URL}/{column}/"
        else:
            url = f"{BASE_URL}/{column}/index_{page}.htm"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r.status_code != 200:
                break
            r.encoding = r.apparent_encoding or "utf-8"
        except requests.RequestException:
            break

        found = 0
        for href, yyyymm, date8, title_raw in _ITEM_RE.findall(r.text):
            title = _clean(title_raw)
            if not title or href in seen:
                continue
            seen.add(href)
            records.append({
                "title": title,
                "url": urljoin(url, href),
                "date": _norm_date(yyyymm, date8),
                "source": "kjs.mof.gov.cn",
                "column": COLUMNS.get(column, column),
            })
            found += 1
        if not found:
            break
        if page + 1 < pages:
            time.sleep(0.5)

    records.sort(key=lambda x: x["date"], reverse=True)
    return records


def fetch_all(pages: int = 1) -> list:
    """抓取全部栏目并按日期倒序合并。"""
    records = []
    for column in COLUMNS:
        records.extend(fetch_column(column, pages=pages))
    records.sort(key=lambda x: x["date"], reverse=True)
    return records


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="财政部会计司栏目抓取")
    p.add_argument("--column", choices=list(COLUMNS), help="只抓指定栏目")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    records = fetch_column(args.column, pages=args.pages) if args.column \
        else fetch_all(pages=args.pages)

    if args.json:
        print(json.dumps({"total": len(records), "results": records},
                         ensure_ascii=False, indent=2))
        return

    print(f"🏛️ 会计司 共 {len(records)} 条")
    for item in records:
        print(f"  📋 [{item['column']}] {item['title']}")
        print(f"     {item['date']} | {item['url']}")


if __name__ == "__main__":
    main()
