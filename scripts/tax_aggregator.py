#!/usr/bin/env python3
"""
Tax Policy Aggregator — concurrent multi-source search with dedup and ranking.

Data source priority:
  1. NPC API (flk.npc.gov.cn) — laws, administrative regulations (highest authority)
  2. fgk.chinatax.gov.cn — STA official regulation library (search5 JSON API)
  3. chinatax.gov.cn — STA announcements, policy interpretations
  4. AnySearch legal domain — supplementary web results
  5. Accounting standards (kjs.mof.gov.cn + casc.org.cn) — local title filter

Usage:
  python tax_aggregator.py "增值税" --size 10
  python tax_aggregator.py "小微企业优惠" --size 10 --json
  python tax_aggregator.py "加计扣除" --sources npc,chinatax  # NPC + chinatax only
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Import sibling modules
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from tax_search import search_tax
from tax_web_search import search_chinatax
from fgk_chinatax import search_fgk

# 会计准则源（kjs+casc）列表的进程内缓存，避免每次搜索都回源
_ACCOUNTING_CACHE = {"rows": [], "fetched_at": 0.0}
_ACCOUNTING_TTL = 600  # 秒


def _search_accounting(keyword: str, size: int = 5) -> list:
    """
    会计准则源：抓取会计司/准则委栏目列表（带 10 分钟缓存），
    按关键词对标题做多词匹配。列表规模小（~80 条），本地过滤即可。
    """
    import time as _time
    now = _time.time()
    if now - _ACCOUNTING_CACHE["fetched_at"] > _ACCOUNTING_TTL or not _ACCOUNTING_CACHE["rows"]:
        from mof_kjs import fetch_all as fetch_kjs
        from casc_org import fetch_all as fetch_casc
        try:
            rows = fetch_kjs(pages=1) + fetch_casc()
        except Exception:
            rows = []
        # 标题去重（两站互为镜像）
        seen, merged = set(), []
        for r in rows:
            key = (r.get("title") or "").replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
        merged.sort(key=lambda x: x.get("date", ""), reverse=True)
        _ACCOUNTING_CACHE["rows"] = merged
        _ACCOUNTING_CACHE["fetched_at"] = now

    tokens = [t for t in keyword.replace("　", " ").split() if t]
    grams = _query_grams(keyword)
    results = []
    for r in _ACCOUNTING_CACHE["rows"]:
        title = r.get("title", "")
        matched = (not tokens or any(t in title for t in tokens)
                   or sum(1 for g in grams if g in title) >= 2)
        if matched:
            results.append({
                "title": title,
                "url": r.get("url", ""),
                "publish_date": r.get("date", ""),
                "date": r.get("date", ""),
                "source": r.get("source", ""),
                "column": r.get("column", ""),
                "snippet": f"[{r.get('column', '')}] {r.get('source', '')}",
            })
            if len(results) >= size:
                break
    return results


def _anysearch_fallback(keyword: str, size: int = 5) -> list:
    """
    Attempt AnySearch via its CLI if available.
    Falls back gracefully if not configured.
    """
    anysearch_dir = Path.home() / ".claude" / "skills" / "anysearch" / "scripts"
    cli_py = anysearch_dir / "anysearch_cli.py"

    if not cli_py.exists():
        return []

    import subprocess
    try:
        result = subprocess.run(
            ["python3", str(cli_py), "search", keyword,
             "--domain", "legal", "--max_results", str(size)],
            capture_output=True, text=True, timeout=20,
            cwd=str(anysearch_dir.parent),
        )
        if result.returncode == 0 and result.stdout.strip():
            # Try to parse the output (it's JSON)
            try:
                data = json.loads(result.stdout)
                return _normalize_anysearch(data, keyword)
            except json.JSONDecodeError:
                # Return raw output as single pseudo-result
                lines = result.stdout.strip().split("\n")[:size]
                return [{"title": l[:100], "url": "", "source": "AnySearch"} for l in lines if l.strip()]
    except Exception:
        pass

    return []


def _normalize_anysearch(data: dict, keyword: str) -> list:
    """Normalize AnySearch output to standard format."""
    results = []
    items = data.get("results", data.get("data", []))
    if isinstance(items, list):
        for item in items[:10]:
            results.append({
                "title": item.get("title", item.get("name", str(item)[:80])),
                "url": item.get("url", item.get("link", "")),
                "snippet": item.get("snippet", item.get("summary", "")),
                "source": "AnySearch",
            })
    return results


def _jaccard_similarity(s1: str, s2: str) -> float:
    """Simple Jaccard similarity on character trigrams for title dedup."""
    if not s1 or not s2:
        return 0.0

    def trigrams(s):
        s = s.lower().strip()
        return {s[i:i+3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}

    t1 = trigrams(s1)
    t2 = trigrams(s2)
    if not t1 or not t2:
        return 0.0

    intersection = len(t1 & t2)
    union = len(t1 | t2)
    return intersection / union if union > 0 else 0.0


def _query_grams(keyword: str) -> set:
    """查询的二元字组（2-gram）集合，按空白分词后逐词生成。"""
    grams = set()
    for token in (keyword or "").replace("　", " ").split():
        for i in range(len(token) - 1):
            grams.add(token[i:i+2])
    return grams


def _relevance_gate(items: list, keyword: str) -> list:
    """
    标题相关度门禁 + 源内重排。

    背景：NPC 系接口（flk.npc.gov.cn）在无真实命中时会返回无关的
    高热法律（宪法/监察法/国防法…），全文模糊模式尤甚（实测
    "小微企业优惠"返回 12152 条、前 50 条无一相关）。这些噪声因
    权威度排名靠前，会挤掉 fgk/chinatax 源里真正相关的部门文件。

    规则：标题命中查询的 ≥2 个 2-gram，或完整包含查询串；
    过滤后按相关度降序、发布日期降序重排。代价是"正文相关但
    标题无关"的全文命中会被一并过滤（此类结果与噪声无法区分），
    需要时用 tax_search.py --scope fulltext 直查。
    """
    grams = _query_grams(keyword)
    if not grams or not items:
        return items
    scored = []
    for item in items:
        title = item.get("title", "")
        full_hit = keyword in title
        score = sum(1 for g in grams if g in title)
        if full_hit or score >= 2:
            item["_relevance"] = score + (len(grams) if full_hit else 0)
            scored.append(item)
    # 两次稳定排序：先日期倒序，再相关度倒序（相关度相同时保持日期倒序）
    scored.sort(key=lambda x: (x.get("publish_date") or x.get("date") or ""), reverse=True)
    scored.sort(key=lambda x: -x.get("_relevance", 0))
    return scored


def deduplicate(results: list, threshold: float = 0.7) -> list:
    """Remove near-duplicate results based on title similarity."""
    deduped = []
    for item in results:
        title = item.get("title", "")
        is_dup = False
        for existing in deduped:
            sim = _jaccard_similarity(title, existing.get("title", ""))
            if sim >= threshold:
                is_dup = True
                break
        if not is_dup:
            deduped.append(item)
    return deduped


def _search_with_prefix_fallback(search_fn, keyword: str, size: int) -> dict:
    """
    search5 系接口（fgk/chinatax）按整串匹配：自然语言查询如
    "小微企业优惠"在标题里不存在整串，会零命中。无命中时逐字缩短
    前缀重试（最多 2 次），返回质量交给 _relevance_gate 用原始
    关键词把关（缩短后的查询是原查询的概念超集，门禁仍有效）。
    """
    result = search_fn(keyword, size=size)
    k = keyword
    tried = 0
    while (not result.get("raw_total") and " " not in k.strip()
           and len(k.strip()) > 3 and tried < 2):
        k = k.strip()[:-1]
        result = search_fn(k, size=size)
        tried += 1
    return result


def aggregate_search(keyword: str, *,
                     size: int = 10,
                     sources: list = None,
                     status: int = 3,
                     scope: str = "fulltext") -> dict:
    """
    Concurrently search multiple data sources and return deduplicated, ranked results.

    Args:
        keyword: search term
        size: results per source
        sources: list of source names ('npc', 'fgk', 'chinatax', 'accounting',
                 'anysearch'). Default: all five.
        status: NPC status filter (default: 3 = effective)
        scope: NPC search scope (default: fulltext)
    """
    if sources is None:
        sources = ["npc", "fgk", "chinatax", "anysearch", "accounting"]

    results = {}
    errors = {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {}

        if "npc" in sources:
            # NPC 全文检索排序不稳定（小 pageSize 时首页常被无关热词占据），
            # 多抓 4 倍再由相关度门禁筛选截断
            futures["npc"] = pool.submit(
                search_tax, keyword, scope=scope, status=status,
                size=min(size * 4, 40)
            )
        if "fgk" in sources:
            futures["fgk"] = pool.submit(
                _search_with_prefix_fallback, search_fgk, keyword, size
            )
        if "chinatax" in sources:
            futures["chinatax"] = pool.submit(
                _search_with_prefix_fallback, search_chinatax, keyword, size
            )
        if "anysearch" in sources:
            futures["anysearch"] = pool.submit(
                _anysearch_fallback, keyword, min(size, 5)
            )
        if "accounting" in sources:
            futures["accounting"] = pool.submit(
                _search_accounting, keyword, size
            )

        for source, future in futures.items():
            try:
                results[source] = future.result(timeout=20)
            except Exception as e:
                errors[source] = str(e)
                results[source] = None

    # Collect all results
    all_items = []
    source_order = {"npc": 0, "fgk": 1, "chinatax": 2, "anysearch": 3, "accounting": 4}

    # NPC results first (highest authority) — 相关度门禁拦截无真实命中时的
    # 高热噪声（宪法/监察法等），见 _relevance_gate 注释
    npc_rows, fgk_rows, chinatax_rows = [], [], []
    npc_data = results.get("npc")
    if npc_data and npc_data.get("total", 0) > 0:
        npc_rows = _relevance_gate(npc_data.get("results", []), keyword)[:size]
    for item in npc_rows:
        item["_source"] = "npc"
        item["_authority_rank"] = 0
        all_items.append(item)

    # fgk.chinatax.gov.cn 官方法规总库 second
    fgk_data = results.get("fgk")
    if fgk_data and fgk_data.get("total", 0) > 0:
        fgk_rows = _relevance_gate(fgk_data.get("results", []), keyword)
    for item in fgk_rows:
        item["_source"] = "fgk"
        item["_authority_rank"] = 1
        if "date" in item and "publish_date" not in item:
            item["publish_date"] = item["date"]
        all_items.append(item)

    # chinatax results
    chinatax_data = results.get("chinatax")
    if chinatax_data and chinatax_data.get("total", 0) > 0:
        chinatax_rows = _relevance_gate(chinatax_data.get("results", []), keyword)
    for item in chinatax_rows:
        item["_source"] = "chinatax"
        item["_authority_rank"] = 2
        # Normalize: chinatax uses date not publish_date
        if "date" in item and "publish_date" not in item:
            item["publish_date"] = item["date"]
        all_items.append(item)

    # AnySearch results
    anysearch_data = results.get("anysearch")
    if isinstance(anysearch_data, list) and anysearch_data:
        for item in anysearch_data:
            item["_source"] = "anysearch"
            item["_authority_rank"] = 3
            all_items.append(item)

    # Accounting standards (kjs + casc) last — different domain
    accounting_data = results.get("accounting")
    if isinstance(accounting_data, list) and accounting_data:
        for item in accounting_data:
            item["_source"] = "accounting"
            item["_authority_rank"] = 4
            all_items.append(item)

    # Deduplicate across sources
    all_items = deduplicate(all_items)

    # Sort: authority rank first, then by date
    all_items.sort(key=lambda x: (
        x.get("_authority_rank", 99),
        # Put items with dates before those without
        0 if x.get("publish_date") else 1,
    ))

    return {
        "keyword": keyword,
        "total_sources": len(sources),
        "total_items": len(all_items),
        "items": all_items[:size * 3],  # Cap total results
        "source_summary": {
            # 门禁后的实际入围条数（原始命中数见各源 raw_total）
            "npc": len(npc_rows),
            "fgk": len(fgk_rows),
            "chinatax": len(chinatax_rows),
            "anysearch": len(anysearch_data) if isinstance(anysearch_data, list) else 0,
            "accounting": len(accounting_data) if isinstance(accounting_data, list) else 0,
        },
        "errors": errors,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Multi-source tax policy search aggregator",
        epilog="""
Examples:
  python tax_aggregator.py "增值税" --size 10
  python tax_aggregator.py "加计扣除" --sources npc,chinatax --json
        """
    )
    p.add_argument("keyword", help="Search keyword")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--sources", default="npc,fgk,chinatax,anysearch,accounting",
                   help="Comma-separated source names (default: npc,fgk,chinatax,anysearch,accounting)")
    p.add_argument("--status", type=int, default=3,
                   help="NPC status filter (3=effective)")
    p.add_argument("--scope", choices=["title", "fulltext"], default="fulltext")
    p.add_argument("--json", action="store_true")

    args = p.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    result = aggregate_search(
        args.keyword,
        size=args.size,
        sources=sources,
        status=args.status,
        scope=args.scope,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"🔍 多源搜索 \"{args.keyword}\" | {result['searched_at']}")
    print(f"   数据源: {', '.join(sources)}")
    print(f"   NPC: {result['source_summary'].get('npc', 0)} 条")
    print(f"   税务法规库: {result['source_summary'].get('fgk', 0)} 条")
    print(f"   税务总局: {result['source_summary'].get('chinatax', 0)} 条")
    print(f"   网页补充: {result['source_summary'].get('anysearch', 0)} 条")
    print(f"   会计准则: {result['source_summary'].get('accounting', 0)} 条")
    if result.get("errors"):
        for src, err in result["errors"].items():
            print(f"   ⚠️ {src}: {err}")
    print()

    source_labels = {"npc": "📜 NPC法规库", "fgk": "📚 税务法规库", "chinatax": "🏛️ 国家税务总局", "anysearch": "🌐 网页补充", "accounting": "📐 会计准则"}
    for item in result.get("items", [])[:20]:
        src = item.get("_source", "")
        label = source_labels.get(src, "")
        print(f"  {label} {item.get('title', '')[:80]}")
        if item.get("publish_date"):
            print(f"     日期: {item['publish_date']}")
        if item.get("id"):
            print(f"     ID: {item['id']}")
        if item.get("url"):
            print(f"     {item['url']}")
        print()


if __name__ == "__main__":
    main()
