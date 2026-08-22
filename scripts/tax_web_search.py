#!/usr/bin/env python3
"""
Tax Web Search — scrape chinatax.gov.cn as fallback for department-level regulations.

chinatax.gov.cn hosts State Taxation Administration (STA) announcements, policy
interpretations, and operational guides that are NOT in the NPC law database.

Usage:
  python tax_web_search.py "增值税" --size 10
  python tax_web_search.py "小微企业优惠" --size 10 --json
"""

import argparse
import html
import json
import re
import sys
import time
from typing import Optional
from urllib.parse import quote, urljoin

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.chinatax.gov.cn"
# chinatax uses WAS (Web Application Server) search engine
SEARCH_URL = f"{BASE_URL}/was5/web/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 15

# Keep policy-oriented channels and exclude news, media and local activity.
POLICY_CHANNEL_KEYWORDS = (
    "政策公告", "政策法规", "政策解读", "税费优惠政策", "税收优惠政策",
)


def search_chinatax(keyword: str, page: int = 1, size: int = 10) -> dict:
    """
    Search chinatax.gov.cn for tax policy documents via WAS search engine.

    Returns a dict with the same structure as tax_search.search_tax().
    """
    try:
        # The former WAS5 endpoint now returns 404. The current search page
        # submits form data to /search5/search/s and returns JSON.
        search_url = f"{BASE_URL}/search5/search/s"
        payload = {
            "siteCode": "bm29000002",
            "searchWord": keyword,
            "column": "全部",
            "uc": "1",
            "left_right_index": "0",
            "pageSize": str(max(size, 1)),
            "pageNum": str(max(page - 1, 0)),
        }
        filtered_rows = []
        raw_total = 0
        raw_page_size = 10
        target_count = max(page, 1) * max(size, 1)
        page_num = 0
        while len(filtered_rows) < target_count:
            payload["pageNum"] = str(page_num)
            r = requests.post(search_url, data=payload, headers=HEADERS,
                               timeout=TIMEOUT)
            if r.status_code != 200:
                # Fallback: try Baidu site-search (some chinatax pages use this)
                return _baidu_site_search(keyword, size)

            data = r.json()
            search_result = data.get("searchResultAll", {})
            rows = search_result.get("searchTotal", [])
            raw_total = search_result.get("total", raw_total)
            filtered_rows.extend(item for item in rows if _is_policy_result(item))
            if not rows or (page_num + 1) * raw_page_size >= raw_total:
                break
            page_num += 1

        filtered_rows = filtered_rows[(max(page, 1) - 1) * max(size, 1):]
        results = []
        for item in filtered_rows[:size]:
            gov_doc = item.get("govDoc") or {}
            title = _clean_text(item.get("title", ""))
            if not title:
                continue
            results.append({
                "id": item.get("id", ""),
                "title": title,
                "url": item.get("url") or item.get("snapshotUrl", ""),
                "date": item.get("pubDate", ""),
                "document_number": (
                    gov_doc.get("docNo") or gov_doc.get("docNum") or ""
                ),
                "source": "chinatax.gov.cn",
                "snippet": _clean_text(item.get("shortContent", ""))[:200],
                "column": _clean_text(item.get("column", "")),
                "label": _clean_text(item.get("label", "")),
                "channel": _clean_text(item.get("channel_levels_names", "")),
            })

        total = len(results)

    except requests.RequestException as e:
        return _empty_result(keyword, str(e))
    except (ValueError, TypeError, AttributeError) as e:
        return _empty_result(keyword, f"Invalid chinatax response: {e}")

    return {
        "keyword": keyword,
        "total": total,
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "chinatax.gov.cn",
        "raw_total": raw_total,
        "_from_cache": False,
    }


def _clean_text(value: str) -> str:
    """Remove result markup and decode HTML entities from search fields."""
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).strip()


def _is_policy_result(item: dict) -> bool:
    """Return whether a tax-office result belongs to a policy channel."""
    channel_text = " ".join(
        _clean_text(item.get(field, ""))
        for field in ("channel_levels_names", "column", "label")
    )
    return any(keyword in channel_text for keyword in POLICY_CHANNEL_KEYWORDS)


def _empty_result(keyword: str, error: str = "") -> dict:
    return {
        "keyword": keyword,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "chinatax.gov.cn",
        "_error": error,
        "_from_cache": False,
    }


def _baidu_site_search(keyword: str, size: int = 10) -> dict:
    """Fallback: use Baidu site search for chinatax.gov.cn."""
    from urllib.parse import quote
    baidu_url = f"https://www.baidu.com/s?wd=site%3Achinatax.gov.cn+{quote(keyword)}&rn={size}"
    try:
        r = requests.get(baidu_url, headers={**HEADERS, "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return _empty_result(keyword, f"Baidu fallback HTTP {r.status_code}")

        html = r.text
        results = []
        # Baidu search result parsing — tolerant regex
        link_pattern = re.compile(
            r'<a[^>]*href="(https?://[^"]*chinatax\.gov\.cn[^"]*)"[^>]*>'
            r'(.*?)</a>',
            re.DOTALL | re.IGNORECASE
        )
        seen = set()
        for href, title_raw in link_pattern.findall(html):
            if href in seen:
                continue
            seen.add(href)
            title = re.sub(r"<[^>]+>", "", title_raw).strip()
            if len(title) < 5:
                continue
            results.append({"title": title, "url": href, "source": "chinatax.gov.cn (Baidu)"})
            if len(results) >= size:
                break

        return {
            "keyword": keyword,
            "total": len(results),
            "results": results,
            "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "chinatax.gov.cn (Baidu)",
            "_from_cache": False,
        }
    except requests.RequestException as e:
        return _empty_result(keyword, str(e))


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Search chinatax.gov.cn for tax policy documents"
    )
    p.add_argument("keyword", help="Search keyword")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--json", action="store_true", help="Output JSON")

    args = p.parse_args()
    result = search_chinatax(args.keyword, size=args.size)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"🔍 chinatax.gov.cn 搜索 \"{args.keyword}\" | {result['searched_at']}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    print(f"共 {result['total']} 条")
    print()

    for item in result.get("results", []):
        print(f"  📋 {item['title']}")
        if item.get("document_number"):
            print(f"     文号: {item['document_number']}")
        if item.get("date"):
            print(f"     日期: {item['date']}")
        print(f"     {item['url']}")
        print()


if __name__ == "__main__":
    main()
