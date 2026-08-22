#!/usr/bin/env python3
"""
法规版本链与效力追踪器 (Law Version Chain & Effect Tracker)

NPC API 只返回时效状态码(sxx),不提供前后版本关联。本模块通过三种信号
构建版本链,可靠性从高到低:
  1. 文本确认 —— 新法施行条款中的"《旧法》同时废止"(解析 DOCX 全文)
  2. 状态推断 —— 同族内 sxx=1/2 的旧版本 → 更新的现行版本
  3. 时间线   —— 同族按公布日期排序的演进序列

用法:
  python scripts/tax_versions.py --query 增值税              # 查看某税种版本时间线
  python scripts/tax_versions.py --check "增值税暂行条例"      # 效力问答:还能用吗
  python scripts/tax_versions.py --build --out docs/data/versions.json
"""
import argparse
import html
import json
import os
import re
import sys
import time

import requests
import urllib3
urllib3.disable_warnings()

from tax_detail import get_download_url, _parse_docx_from_bytes

BASE_URL = "https://flk.npc.gov.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://flk.npc.gov.cn/",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
VERIFY_SSL = False

SXX_NAMES = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效"}

# 20 个税种/征管领域的母法族种子(税收优惠无母法,不设族);
# aliases 为同族异名(如 进出口关税条例 → 关税族)
SEEDS = [
    {"base": "增值税", "keyword": "增值税"},
    {"base": "消费税", "keyword": "消费税"},
    {"base": "关税", "keyword": "关税", "aliases": ["进出口关税"]},
    {"base": "企业所得税", "keyword": "企业所得税"},
    {"base": "个人所得税", "keyword": "个人所得税"},
    {"base": "房产税", "keyword": "房产税"},
    {"base": "土地增值税", "keyword": "土地增值税"},
    {"base": "契税", "keyword": "契税"},
    {"base": "城镇土地使用税", "keyword": "城镇土地使用税"},
    {"base": "车船税", "keyword": "车船税"},
    {"base": "车辆购置税", "keyword": "车辆购置税"},
    {"base": "船舶吨税", "keyword": "船舶吨税"},
    {"base": "烟叶税", "keyword": "烟叶税"},
    {"base": "耕地占用税", "keyword": "耕地占用税"},
    {"base": "印花税", "keyword": "印花税"},
    {"base": "城市维护建设税", "keyword": "城市维护建设税"},
    {"base": "资源税", "keyword": "资源税"},
    {"base": "环境保护税", "keyword": "环境保护税"},
    {"base": "税收征收管理", "keyword": "税收征收管理法"},
    {"base": "发票", "keyword": "发票管理办法"},
]

# 文书类型后缀,长者优先,避免"管理办法"被截成"…管"+"理法"
DOC_TYPES = [
    "暂行条例实施细则", "实施细则", "实施条例", "实施办法",
    "暂行条例", "暂行办法", "管理办法", "暂行规定",
    "条例", "规定", "办法", "法",
]
# 文书层级注释:推断后继时优先同文书类型(修正版→新版),已足够,无需全局层级表

REVISION_RE = re.compile(r"[（(](\d{4})年(?:修正|修订|修正文本|文本|修正本)?[)）]\s*$")
TAG_RE = re.compile(r"<[^>]+>")
QUOTED_TITLE_RE = re.compile(r"《([^《》]+)》")
EFFECTIVE_DATE_RE = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起施行")


# ─────────────────────────── 纯逻辑层(离线可测) ───────────────────────────

def clean_title(text):
    """去除 NPC 高亮标记与 HTML 实体。"""
    if not text:
        return ""
    return html.unescape(TAG_RE.sub("", text)).strip()


def parse_title(title):
    """'中华人民共和国增值税暂行条例' → base=增值税, doc_type=暂行条例。
    括号修正标注(2018年修正)提取为 revision,不计入 base。"""
    t = clean_title(title)
    revision, revision_year = None, None
    m = REVISION_RE.search(t)
    if m:
        revision_year = m.group(1)
        revision = t[m.start():].strip("（）()")
        t = t[:m.start()].strip()
    if t.startswith("中华人民共和国"):
        t = t[len("中华人民共和国"):]
    doc_type = ""
    for dt in DOC_TYPES:
        if t.endswith(dt) and len(t) - len(dt) >= 2:
            doc_type = dt
            t = t[: -len(dt)]
            break
    # 实施类文书二次剥离母法后缀:《税收征收管理法实施细则》→ base 税收征收管理
    if doc_type in ("暂行条例实施细则", "实施细则", "实施条例", "实施办法"):
        for dt2 in DOC_TYPES:
            if t.endswith(dt2) and len(t) - len(dt2) >= 2:
                t = t[: -len(dt2)]
                break
    return {"base": t, "doc_type": doc_type, "revision": revision,
            "revision_year": revision_year}


def normalize_for_match(title):
    """用于版本间标题匹配:去国名前缀、去修正括号,保留文书类型。"""
    p = parse_title(title)
    return f"{p['base']}{p['doc_type']}"


def is_central_authority(name):
    """只保留中央级机关,排除地方法规对族时间线的干扰。"""
    if not name:
        return False
    return ("省" not in name and "市" not in name and "自治区" not in name
            and "特别行政区" not in name)


def belongs_to_family(title, family_bases):
    """family_bases: 族 base 及其别名列表(如 ["关税", "进出口关税"])。"""
    if isinstance(family_bases, str):
        family_bases = [family_bases]
    parsed = parse_title(title)
    return parsed["base"] in family_bases and parsed["doc_type"] in DOC_TYPES


def sort_key(rec):
    """时间线排序:公布日期升序,无日期排最后;同日按生效日期。"""
    return (rec.get("publish_date") or "9999-12-31",
            rec.get("effective_date") or "9999-12-31")


def extract_repeal_refs(paragraphs, own_title):
    """从法规全文提取施行/废止条款:
    返回 {"effective": "YYYY-MM-DD"|None, "repealed": [被废止法规名], "clause": 条款原文}"""
    result = {"effective": None, "repealed": [], "clause": ""}
    own_norm = normalize_for_match(own_title)
    for para in paragraphs or []:
        has_repeal = "废止" in para
        has_effective = EFFECTIVE_DATE_RE.search(para)
        if not (has_repeal or has_effective):
            continue
        if not result["clause"]:
            result["clause"] = para.strip()
        if has_effective and not result["effective"]:
            y, m, d = has_effective.groups()
            result["effective"] = f"{y}-{int(m):02d}-{int(d):02d}"
        if has_repeal:
            for quoted in QUOTED_TITLE_RE.findall(para):
                if normalize_for_match(quoted) != own_norm:
                    result["repealed"].append(quoted)
    return result


def find_current(family_records):
    """族的当前适用版本:sxx=3 优先(未生效的新法施行前,旧条例仍是适用版本),
    法轨优先于条例/办法轨。仅当无 sxx=3 时才返回 sxx=4。"""
    for want in (3, 4):
        laws = [r for r in family_records
                if r["status_code"] == want and r["doc_type"] == "法"]
        if laws:
            return max(laws, key=sort_key)
        any_rec = [r for r in family_records if r["status_code"] == want]
        if any_rec:
            return max(any_rec, key=sort_key)
    return None


def build_edges(family_records, text_refs):
    """构建替代边。text_refs: {bbbs: extract_repeal_refs 结果}。
    文本确认的边优先;剩余 sxx=1/2 旧版按"同族更晚公布的更高层级文书"推断后继。"""
    by_bbbs = {r["bbbs"]: r for r in family_records}
    edges = []
    covered = set()  # 已有入边(被替代)的旧版 bbbs

    # 1) 文本确认边:新法条款点名废止的旧法
    for bbbs, refs in text_refs.items():
        successor = by_bbbs.get(bbbs)
        if not successor:
            continue
        for repealed_title in refs.get("repealed", []):
            target_norm = normalize_for_match(repealed_title)
            for r in family_records:
                if normalize_for_match(r["title"]) == target_norm:
                    edges.append({
                        "from_bbbs": r["bbbs"], "from_title": r["title"],
                        "from_date": r.get("publish_date"),
                        "to_bbbs": bbbs, "to_title": successor["title"],
                        "to_date": successor.get("publish_date"),
                        "basis": "text", "clause": refs.get("clause", ""),
                        "effective_date": refs.get("effective")
                        or successor.get("effective_date"),
                    })
                    covered.add(r["bbbs"])
                    break

    # 2) 状态推断边:sxx=1/2 且无文本入边 → 同族中公布更晚的现行文书;
    #    优先同文书类型(修正版→新版),避免把法错误连到它的实施条例
    for old in family_records:
        if old["status_code"] not in (1, 2) or old["bbbs"] in covered:
            continue
        candidates = [
            r for r in family_records
            if r["bbbs"] != old["bbbs"]
            and r["status_code"] in (3, 4)
            and (r.get("publish_date") or "9999") >= (old.get("publish_date") or "0000")
        ]
        same_type = [r for r in candidates if r["doc_type"] == old["doc_type"]]
        pool = same_type or candidates
        if not pool:
            continue
        successor = min(pool, key=lambda r: r.get("publish_date") or "9999-12-31")
        edges.append({
            "from_bbbs": old["bbbs"], "from_title": old["title"],
            "from_date": old.get("publish_date"),
            "to_bbbs": successor["bbbs"], "to_title": successor["title"],
            "to_date": successor.get("publish_date"),
            "basis": "inferred", "clause": "",
            "effective_date": successor.get("effective_date"),
        })
    return edges


def assess_effect(query, families):
    """效力问答:给定法规名(或 bbbs),返回现在还能不能用、被谁取代、依据。"""
    q_norm = normalize_for_match(query)
    record = None
    family = None
    for fam in families:
        for r in fam["records"]:
            if query == r["bbbs"] or q_norm == normalize_for_match(r["title"]):
                record, family = r, fam
                break
        if record:
            break
    if not record:
        return {"matched": False, "query": query,
                "message": "未在已建族中找到该法规,请用 --build 更新数据或检查名称"}

    sxx = record["status_code"]
    verdict = {3: "effective", 4: "pending", 2: "amended", 1: "abolished"}[sxx]
    successor = None
    if sxx in (1, 2):
        edge = next((e for e in family["edges"] if e["from_bbbs"] == record["bbbs"]), None)
        if edge:
            successor = {
                "title": edge["to_title"],
                "bbbs": edge["to_bbbs"],
                "effective_date": edge.get("effective_date"),
                "basis": "文本确认" if edge["basis"] == "text" else "推断(待文本核验)",
                "clause": edge.get("clause", ""),
            }
        else:
            cur = find_current(family["records"])
            if cur:
                successor = {"title": cur["title"], "bbbs": cur["bbbs"],
                             "effective_date": cur.get("effective_date"),
                             "basis": "推断(同族现行版本)", "clause": ""}
    current = find_current(family["records"])
    return {
        "matched": True,
        "query": query,
        "record": {k: record[k] for k in
                   ("bbbs", "title", "publish_date", "effective_date",
                    "status_code", "issuing_authority")},
        "status_label": SXX_NAMES[sxx],
        "verdict": verdict,
        "verdict_label": {"effective": "🟢 现行有效,可以引用",
                          "pending": "🔵 尚未生效,注意施行日期",
                          "amended": "🟡 已被修改,应查最新版本",
                          "abolished": "🔴 已废止,不可再引用"}[verdict],
        "successor": successor,
        "family_current": {"title": current["title"], "bbbs": current["bbbs"]}
        if current else None,
        "timeline": [{k: r[k] for k in
                      ("bbbs", "title", "publish_date", "effective_date", "status_code")}
                     for r in family["records"]],
    }


# ─────────────────────────── 网络层(带重试) ───────────────────────────

def _retry(fn, retries=3, delay=2):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(delay * attempt)
    raise last


def search_all_statuses(keyword, bases, size=30):
    """跨 4 种时效状态搜索,客户端过滤出本族(含别名)中央级法规记录。"""
    records = {}
    for sxx in (1, 2, 3, 4):
        payload = {
            "searchRange": 1, "searchType": 2, "searchContent": keyword,
            "pageNum": 1, "pageSize": min(size, 50),
            "orderByParam": {"order": "", "sort": ""},
            "flfgCodeId": [], "zdjgCodeId": [], "sxx": [sxx],
            "gbrq": [], "sxrq": [], "gbrqYear": [], "xgzlSearch": False,
        }
        try:
            resp = _retry(lambda: requests.post(
                f"{BASE_URL}/law-search/search/list", json=payload,
                headers=HEADERS, timeout=20, verify=VERIFY_SSL).json())
        except Exception as e:
            print(f"    WARN: sxx={sxx} search failed: {e}", file=sys.stderr)
            continue
        # NPC 响应有的接口包 data 层、有的直接顶层返回,两种都兼容
        outer = resp.get("data") or resp
        for row in outer.get("rows") or []:
            title = clean_title(row.get("title") or row.get("flfgname") or "")
            authority = clean_title(row.get("zdjgName") or "")
            if not belongs_to_family(title, bases):
                continue
            if not is_central_authority(authority):
                continue
            parsed = parse_title(title)
            records[row["bbbs"]] = {
                "bbbs": row["bbbs"], "title": title,
                "base": bases[0], "doc_type": parsed["doc_type"],
                "revision": parsed["revision"],
                "publish_date": row.get("gbrq") or "",
                "effective_date": row.get("sxrq") or "",
                "status_code": row.get("sxx", 0),
                "issuing_authority": authority,
                "category": clean_title(row.get("flxz") or ""),
                "url": f"https://flk.npc.gov.cn/detail?id={row['bbbs']}",
            }
        time.sleep(0.3)
    return sorted(records.values(), key=sort_key)


def fetch_law_text(bbbs):
    """下载并解析 DOCX 全文(复用 tax_detail 零依赖解析),失败返回空列表。"""
    try:
        def _download():
            url = get_download_url(bbbs)
            r = requests.get(url, timeout=30, verify=VERIFY_SSL)
            r.raise_for_status()
            return _parse_docx_from_bytes(r.content)
        return _retry(_download)
    except Exception:
        return []


# ─────────────────────────── 构建与输出 ───────────────────────────

def build_family(seed, verify_text=True):
    bases = [seed["base"]] + seed.get("aliases", [])
    records = search_all_statuses(seed["keyword"], bases)
    if not records:
        return None
    text_refs = {}
    if verify_text:
        for rec in records:
            if rec["status_code"] in (3, 4) and rec["doc_type"] == "法":
                paras = fetch_law_text(rec["bbbs"])
                if paras:
                    refs = extract_repeal_refs(paras, rec["title"])
                    if refs["repealed"] or refs["effective"]:
                        text_refs[rec["bbbs"]] = refs
                time.sleep(0.3)
    edges = build_edges(records, text_refs)
    current = find_current(records)
    return {"base": seed["base"], "records": records, "edges": edges,
            "current": current}


def build_dataset(seeds=None, verify_text=True):
    families = []
    for i, seed in enumerate(seeds or SEEDS):
        print(f"[{i+1}/{len(seeds or SEEDS)}] {seed['base']}...", end=" ")
        try:
            fam = build_family(seed, verify_text=verify_text)
            if fam:
                n_text = sum(1 for e in fam["edges"] if e["basis"] == "text")
                print(f"{len(fam['records'])} 条记录, {len(fam['edges'])} 条替代边"
                      f"({n_text} 条文本确认)")
                families.append(fam)
            else:
                print("无记录")
        except Exception as e:
            print(f"FAILED: {e}")
    return families


def family_to_json(fam, updated):
    """输出既供前端展示、又可供 --check 从文件重载的单层结构。"""
    current = fam["current"]
    return {
        "base": fam["base"],
        "updated": updated,
        "current": {k: current[k] for k in
                    ("title", "publish_date", "effective_date", "status_code", "url")}
        if current else None,
        "timeline": [{k: r[k] for k in
                      ("bbbs", "title", "doc_type", "revision", "publish_date",
                       "effective_date", "status_code", "issuing_authority",
                       "category", "url")} for r in fam["records"]],
        "edges": [{
            "from": e["from_title"],
            "from_url": f"https://flk.npc.gov.cn/detail?id={e['from_bbbs']}",
            "from_bbbs": e["from_bbbs"],
            "from_date": e.get("from_date"),
            "to": e["to_title"],
            "to_url": f"https://flk.npc.gov.cn/detail?id={e['to_bbbs']}",
            "to_bbbs": e["to_bbbs"],
            "to_date": e.get("to_date"),
            "basis": e["basis"],
            "effective_date": e.get("effective_date"),
            "clause": e.get("clause", ""),
        } for e in fam["edges"]],
    }


def load_families(path):
    """从数据集文件重载(供 --check 复用,避免重复下载)。"""
    with open(path, encoding="utf-8") as f:
        dataset = json.load(f)
    families = []
    for fam in dataset.get("families", []):
        families.append({
            "base": fam["base"],
            "records": fam["timeline"],
            # 文件中的边是展示字段名,重载为内部字段名供 assess_effect 使用
            "edges": [{
                "from_bbbs": e["from_bbbs"], "from_title": e["from"],
                "to_bbbs": e["to_bbbs"], "to_title": e["to"],
                "basis": e["basis"],
                "effective_date": e.get("effective_date"),
                "clause": e.get("clause", ""),
            } for e in fam["edges"]],
            "current": find_current(fam["timeline"]),
        })
    return families


def print_timeline(fam):
    print(f"\n═══ {fam['base']} 版本时间线 ═══")
    for r in fam["records"]:
        badge = {1: "🔴", 2: "🟡", 3: "🟢", 4: "🔵"}.get(r["status_code"], "❓")
        rev = f"({r['revision']})" if r["revision"] else ""
        dates = f"公布 {r['publish_date'] or '?'}"
        if r["effective_date"]:
            dates += f" · 施行 {r['effective_date']}"
        print(f"  {badge} {r['title']}{rev}")
        print(f"      {dates} · {r['issuing_authority']}")
    for e in fam["edges"]:
        basis = "📄文本确认" if e["basis"] == "text" else "🔍推断"
        eff = f"(新法施行 {e['effective_date']})" if e.get("effective_date") else ""
        if e["from_title"] == e["to_title"]:
            print(f"  └ {e['from_title']}({e.get('from_date')}) "
                  f"──修正替代──▶ 同名新版({e.get('to_date')}) {basis} {eff}")
        else:
            print(f"  └ {e['from_title']} ──替代──▶ {e['to_title']} {basis} {eff}")
        if e.get("clause") and e["basis"] == "text":
            print(f"      条款依据: {e['clause'][:100]}")


def main():
    ap = argparse.ArgumentParser(description="法规版本链与效力追踪器")
    ap.add_argument("--query", help="查看某税种的版本时间线(如 增值税)")
    ap.add_argument("--check", help="效力问答:某法规现在还能不能用")
    ap.add_argument("--build", action="store_true", help="构建全部族的版本链数据集")
    ap.add_argument("--out", default="docs/data/versions.json", help="--build 输出路径")
    ap.add_argument("--data", default="docs/data/versions.json",
                    help="--check 优先读取的数据集路径")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if args.query:
        seed = next((s for s in SEEDS if args.query in (s["base"], s["keyword"])), None)
        if not seed:
            sys.exit(f"未知税种 {args.query};可用: {', '.join(s['base'] for s in SEEDS)}")
        fam = build_family(seed)
        if not fam:
            sys.exit("无记录")
        if args.json:
            print(json.dumps(family_to_json(fam, time.strftime("%Y-%m-%d %H:%M:%S")),
                             ensure_ascii=False, indent=2))
        else:
            print_timeline(fam)
    elif args.check:
        if os.path.exists(args.data):
            print(f"(使用已有数据集 {args.data};如需最新请先 --build)", file=sys.stderr)
            families = load_families(args.data)
        else:
            families = build_dataset(verify_text=True)
        result = assess_effect(args.check, families)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.build:
        families = build_dataset(verify_text=True)
        if not families:
            sys.exit("ERROR: all families failed — keeping existing file untouched")
        updated = time.strftime("%Y-%m-%d %H:%M:%S")
        dataset = {"updated": updated, "families": [
            family_to_json(f, updated) for f in families]}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(dataset, f, ensure_ascii=False, indent=2)
        n_text = sum(1 for fam in families for e in fam["edges"] if e["basis"] == "text")
        print(f"\nWrote {args.out}: {len(families)} families, "
              f"{sum(len(f['edges']) for f in families)} edges ({n_text} text-verified)")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
