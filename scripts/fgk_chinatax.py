#!/usr/bin/env python3
"""
fgk.chinatax.gov.cn — 国家税务总局政策法规库（官方总库）直连模块。

同一套 search5 搜索系统也服务 www.chinatax.gov.cn（见 tax_web_search.py），
区别在于 fgk 站点的索引聚焦政策法规文件，且返回结构化字段：
效力状态（xxgk_aging）、税种标签（xxgk_taxPolicy）、文号（govDoc）等。

接口：
  搜索:   POST https://fgk.chinatax.gov.cn/search5/search/s
          (siteCode=bm29000002, searchWord, pageNum 从 0 起, pageSize<=50,
           orderBy=1 按发布日期倒序 / 5 按相关度, label=逗号分隔文档类型过滤)
  详情:   POST https://www.chinatax.gov.cn/queryManuscriptAssociation  (id={文章ID})
          注意域名是 www 而非 fgk，否则 404。返回 contentHtml、关联文档、
          版本历史（id=policyFile_{文章ID}）。

Usage:
  python fgk_chinatax.py "增值税" --size 10
  python fgk_chinatax.py --latest --pages 2
  python fgk_chinatax.py --detail 5238560
"""

import argparse
import html
import json
import re
import sys
import time
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://fgk.chinatax.gov.cn"
SEARCH_URL = f"{BASE_URL}/search5/search/s"
DETAIL_URL = "https://www.chinatax.gov.cn/queryManuscriptAssociation"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{BASE_URL}/zcfgk/index.html",
}
TIMEOUT = 15
RETRIES = 3

# 解读/问答类标签，抓法规文件时默认排除
NON_DOCUMENT_LABELS = ("文字政策解读", "政策问答", "政策指引", "图解解读", "视频解读")

# 文章 ID 从 url 的第二个 c{n} 提取，如 /zcfgk/c100012/c5251854/content.html
_ARTICLE_ID_RE = re.compile(r"/c(\d+)/content\.html")


def _clean_text(value: str) -> str:
    """去除搜索高亮 <span> 等标记并解码 HTML 实体。"""
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).strip()


def _parse_tax_categories(item: dict) -> list:
    """xxgk_taxPolicy / xxgk_son_taxPolicy 是 JSON 字符串数组。"""
    categories = []
    for field in ("xxgk_taxPolicy", "xxgk_son_taxPolicy"):
        raw = item.get(field) or ""
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = [raw] if raw else []
        else:
            parsed = raw or []
        if isinstance(parsed, list):
            categories.extend(str(c) for c in parsed if c)
    seen = set()
    return [c for c in categories if not (c in seen or seen.add(c))]


def _extract_article_id(url: str) -> str:
    m = _ARTICLE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _norm_date(value: str) -> str:
    """pubDate 形如 '2026-08-10 00:00:00'，截断为 '2026-08-10'。"""
    value = (value or "").strip()
    return value[:10] if len(value) >= 10 else value


def _to_record(item: dict) -> Optional[dict]:
    """search5 原始条目 → 统一记录（web 系 schema + fgk 增强字段）。"""
    title = _clean_text(item.get("title", ""))
    if not title:
        return None
    url = item.get("url") or item.get("snapshotUrl", "")
    if url.startswith("//"):
        url = "https:" + url
    gov_doc = item.get("govDoc") or {}
    date = _norm_date(item.get("pubDate", ""))
    record = {
        "id": _extract_article_id(url) or item.get("id", ""),
        "title": title,
        "url": url,
        "date": date,
        "publish_date": date,
        "document_number": gov_doc.get("docNum") or gov_doc.get("docNo") or "",
        "issuing_authority": _clean_text(item.get("pubName", "")),
        "doc_type": _clean_text(item.get("xxgk_effectLevel", "")),
        "status": _clean_text(item.get("xxgk_aging", "")),
        "tax_categories": _parse_tax_categories(item),
        "source": "fgk.chinatax.gov.cn",
        "snippet": _clean_text(item.get("shortContent", ""))[:200],
        "column": _clean_text(item.get("column", "")),
        "label": _clean_text(item.get("label", "")),
    }
    return record


def _request_search(payload: dict) -> dict:
    """带退避重试的 search5 调用；全部失败时抛出最后一个异常。"""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(SEARCH_URL, data=payload, headers=HEADERS,
                              timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise last_err


def search_fgk(keyword: str, size: int = 10, *,
               documents_only: bool = True,
               order_by_date: bool = False) -> dict:
    """
    搜索税务官方法规库，返回与 tax_search.search_tax() 同构的结果。

    documents_only=True 时用服务端 column=政策法规 过滤（只留法规文件本体，
    排除新闻/解读/问答），并按 label 再兜底排除解读类。
    """
    try:
        payload = {
            "siteCode": "bm29000002",
            "searchWord": keyword,
            "column": "政策法规" if documents_only else "全部",
            "uc": "1",
            "left_right_index": "0",
            "pageSize": str(max(size, 1)),
            "pageNum": "0",
            "orderBy": "1" if order_by_date else "5",
        }
        # 服务端每页实际最多返回 10 条，翻页凑够 size
        results = []
        raw_total = 0
        page_num = 0
        while len(results) < size:
            payload["pageNum"] = str(page_num)
            data = _request_search(payload)
            search_result = data.get("searchResultAll", {})
            rows = search_result.get("searchTotal", [])
            raw_total = search_result.get("total", raw_total)
            if not rows:
                break
            for item in rows:
                if documents_only and _clean_text(item.get("label", "")) in NON_DOCUMENT_LABELS:
                    continue
                record = _to_record(item)
                if record:
                    results.append(record)
                if len(results) >= size:
                    break
            if (page_num + 1) * 10 >= raw_total:
                break
            page_num += 1
            time.sleep(0.3)

        return {
            "keyword": keyword,
            "total": len(results),
            "results": results,
            "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "fgk.chinatax.gov.cn",
            "raw_total": raw_total,
        }
    except requests.RequestException as e:
        return _empty_result(keyword, str(e))
    except (ValueError, TypeError, AttributeError) as e:
        return _empty_result(keyword, f"Invalid fgk response: {e}")


def fetch_latest(total: int = 200) -> list:
    """
    拉取法规库最新文件（空关键词 + column=政策法规 + 按发布日期倒序）。

    服务端每页固定返回 10 条（pageSize 参数被忽略），循环翻页凑满 total。
    """
    records = []
    seen = set()
    page_num = 0
    while len(records) < total:
        payload = {
            "siteCode": "bm29000002",
            "searchWord": "",
            "column": "政策法规",
            "uc": "1",
            "left_right_index": "0",
            "pageSize": "50",
            "pageNum": str(page_num),
            "orderBy": "1",
        }
        data = _request_search(payload)
        search_result = data.get("searchResultAll", {})
        rows = search_result.get("searchTotal", [])
        if not rows:
            break
        for item in rows:
            record = _to_record(item)
            if record and record["url"] not in seen:
                seen.add(record["url"])
                records.append(record)
        page_num += 1
        time.sleep(0.5)

    return records[:total]


def fetch_detail(article_id: str) -> dict:
    """
    取法规详情：contentHtml 全文、关联文档（母法/解读/问答）、附件。

    article_id 为详情 URL 中的数字 ID（/zcfgk/c100012/c5251854/content.html →
    5251854）。
    """
    try:
        r = requests.post(DETAIL_URL, data={"id": article_id},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        results = (data.get("results") or {}).get("data", {}).get("results", [])
        if not results:
            return {"id": article_id, "error": f"no results (code={data.get('code')})"}

        current, related = results[0], results[1] if len(results) > 1 else {}

        def _link_list(key):
            items = []
            for doc in (related.get(key) or []):
                url = (doc.get("url") or "").replace("zcfgknw", "zcfgk")
                items.append({
                    "title": _clean_text(doc.get("title", doc.get("name", ""))),
                    "url": url,
                })
            return items

        return {
            "id": article_id,
            "title": _clean_text(current.get("title", "")),
            "content_html": current.get("contentHtml", ""),
            "attachments": current.get("resList") or [],
            "channel": _clean_text(str(current.get("channel", ""))),
            "policy_documents": _link_list("policyDocument"),
            "policy_interpretations": _link_list("policyInterpretation"),
            "policy_qa": _link_list("policyQA"),
        }
    except requests.RequestException as e:
        return {"id": article_id, "error": str(e)}
    except (ValueError, TypeError, AttributeError) as e:
        return {"id": article_id, "error": f"Invalid detail response: {e}"}


def fetch_version_history(article_id: str) -> dict:
    """
    取法规版本历史（法律/行政法规/部门规章类适用）：
    id=policyFile_{文章ID} 调同一详情接口，policyFile[] 列出各版本。
    """
    try:
        r = requests.post(DETAIL_URL, data={"id": f"policyFile_{article_id}"},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        results = (data.get("results") or {}).get("data", {}).get("results", [])
        versions = []
        if results:
            for doc in (results[0].get("policyFile") or []):
                versions.append({
                    "title": _clean_text(doc.get("title", doc.get("name", ""))),
                    "url": (doc.get("url") or "").replace("zcfgknw", "zcfgk"),
                    "status": _clean_text(doc.get("xxgk_aging", "")),
                })
        return {"id": article_id, "versions": versions}
    except requests.RequestException as e:
        return {"id": article_id, "error": str(e)}


def _empty_result(keyword: str, error: str = "") -> dict:
    return {
        "keyword": keyword,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "fgk.chinatax.gov.cn",
        "_error": error,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="国家税务总局政策法规库直连工具")
    p.add_argument("keyword", nargs="?", help="搜索关键词")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--latest", action="store_true", help="拉取最新文件列表")
    p.add_argument("--count", type=int, default=30, help="--latest 时抓取的条数")
    p.add_argument("--detail", metavar="ARTICLE_ID", help="抓取法规详情")
    p.add_argument("--versions", metavar="ARTICLE_ID", help="抓取版本历史")
    p.add_argument("--json", action="store_true")

    args = p.parse_args()

    if args.detail:
        out = fetch_detail(args.detail)
    elif args.versions:
        out = fetch_version_history(args.versions)
    elif args.latest:
        rows = fetch_latest(total=args.count)
        out = {"total": len(rows), "results": rows}
    elif args.keyword:
        out = search_fgk(args.keyword, size=args.size)
    else:
        p.print_help()
        return

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if "results" in out:
        print(f"🔍 法规库 共 {out.get('total', len(out['results']))} 条")
        for item in out["results"]:
            print(f"  📋 {item['title']}")
            bits = [b for b in (
                item.get("document_number"), item.get("publish_date") or item.get("date"),
                item.get("status"), "/".join(item.get("tax_categories", [])[:3]),
            ) if b]
            if bits:
                print(f"     {' | '.join(bits)}")
            print(f"     {item.get('url', '')}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    main()
