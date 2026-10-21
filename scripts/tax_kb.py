#!/usr/bin/env python3
"""
Tax Knowledge Base — 独立采集器:NPC 法规库全量采集 + DOCX 章条解析 + SQLite 落库 + 法条级检索。

路线图位置:快照层(✅) → 版本链(✅) → 五源整合(✅) → 【本脚本:SQLite 落库】 → RAG/AI 层。

数据来源:flk.npc.gov.cn(国家法律法规数据库),复用 tax_search.search_tax(分页/重试/缓存)、
tax_detail(详情/DOCX 下载/零依赖解析)、tax_versions(20 税族种子 + 标题解析 + 族归属)。

用法:
  python scripts/tax_kb.py --build                    # 全量:20 税族逐族采集落库(增量,可中断续跑)
  python scripts/tax_kb.py --build --tax 契税          # 只采一个税族
  python scripts/tax_kb.py --build --refresh           # 忽略 30 天 TTL,强制重抓全文
  python scripts/tax_kb.py --stats                     # 库内统计(各族法规数/法条数/解析覆盖)
  python scripts/tax_kb.py --search "小规模纳税人"      # 法条级检索(双字 FTS5,支持 --tax/--law 过滤)
  python scripts/tax_kb.py --law <bbbs|标题关键词>      # 查看某法规全部法条

库文件默认 data/kb.sqlite(已 gitignore,本地/CI 工件,不进 git)。
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import requests
import urllib3

from tax_detail import get_download_url, _parse_docx_from_bytes
from tax_search import search_tax
from tax_versions import SEEDS, belongs_to_family, parse_title

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_DB = SCRIPT_DIR.parent / "data" / "kb.sqlite"
DETAIL_TTL_DAYS = 30          # 全文重抓间隔;元数据每次搜索都刷新
DOCX_SLEEP_SEC = 0.8          # 对 NPC 的礼貌间隔
SXX_NAMES = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效"}

CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百零]+章\s*(.*)")
ARTICLE_RE = re.compile(r"^第([一二三四五六七八九十百千零两]+|\d+)条\s*")
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


# ─────────────────────────── 纯逻辑层(离线可测) ───────────────────────────

def cn2num(s: str):
    """中文数字→int:'十二'→12,'一百零三'→103;阿拉伯数字串直接 int;非法返回 None。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(ch not in CN_DIGITS and ch not in "十百千" for ch in s):
        return None
    total, num = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        elif ch == "千":
            total += (num or 1) * 1000
            num = 0
    return total + num


def segment_law(paragraphs):
    """段落列表→[{seq, chapter, article_no, text}]。

    - '第X章' 更新当前章;法条归入所属章
    - '第X条' 起新条;其后的普通段落并入当前条(子项换行拼接)
    - 遇到 '附件/附:/附则之后纯附件' 样式段落停止并入,防止税率表灌进末条
    """
    articles, chapter, current = [], None, None
    collecting = True
    for para in paragraphs:
        m_ch = CHAPTER_RE.match(para)
        if m_ch:
            chapter = re.sub(r"\s+", "", para)[:40]
            collecting = current is not None  # 章标题本身不入条
            continue
        m_ar = ARTICLE_RE.match(para)
        if m_ar and cn2num(m_ar.group(1)) is not None:
            seq = cn2num(m_ar.group(1))
            current = {"seq": seq, "chapter": chapter,
                       "article_no": f"第{m_ar.group(1)}条", "text": para.strip()}
            articles.append(current)
            collecting = True
            continue
        if re.match(r"^(附\s*件|附\s*录|附\s*[:：]|附\s*表)", para):
            collecting = False
            continue
        if current is not None and collecting:
            current["text"] += "\n" + para.strip()
    return articles


def bigram(s: str) -> str:
    """中文双字切分:索引与查询共用,解决 FTS5 unicode61 把 CJK 连串当单 token 的问题。"""
    s = re.sub(r"\s+", "", s)
    return " ".join(s[i:i + 2] for i in range(len(s) - 1))


def should_fetch_fulltext(law_row, refresh: bool, now: datetime) -> bool:
    """增量判定:强制刷新 / 未抓过 / 解析异常 / 超过 TTL / 效力状态变了 → 重抓全文。"""
    if refresh:
        return True
    parse_status = law_row["parse_status"]
    if parse_status in (None, "", "pending", "no_docx", "parse_error"):
        return True
    fetched = law_row["fulltext_fetched_at"]
    if not fetched:
        return True
    try:
        age = now - datetime.strptime(fetched[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    if age > timedelta(days=DETAIL_TTL_DAYS):
        return True
    return law_row["status_code"] != law_row["status_code_at_fetch"]


def fts_query(query: str):
    """检索词→FTS5 短语;≥2 字走双字短语,单字返回 None(调用方 LIKE 兜底)。"""
    g = bigram(query)
    return f'"{g}"' if g else None


def snippet(text: str, query: str, width: int = 60) -> str:
    """以查询词首次出现位置为中心截取摘要。"""
    pos = text.replace("\n", "").find(query)
    flat = text.replace("\n", " ")
    if pos < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, pos - width // 3)
    return ("…" if start else "") + flat[start:start + width] + "…"


# ─────────────────────────── SQLite 层 ───────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS laws(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'npc',
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  title_base TEXT, doc_type TEXT, revision TEXT,
  tax_family TEXT,
  category TEXT, issuing_authority TEXT,
  publish_date TEXT, effective_date TEXT,
  status_code INTEGER, status TEXT,
  url TEXT, docx_url TEXT,
  article_count INTEGER DEFAULT 0,
  parse_status TEXT DEFAULT 'pending',
  status_code_at_fetch INTEGER,
  fulltext_fetched_at TEXT,
  discovered_at TEXT, updated_at TEXT,
  UNIQUE(source, source_id));
CREATE INDEX IF NOT EXISTS idx_laws_family ON laws(tax_family);
CREATE INDEX IF NOT EXISTS idx_laws_status ON laws(status_code);
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY,
  law_id INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, chapter TEXT, article_no TEXT, text TEXT NOT NULL,
  UNIQUE(law_id, seq));
CREATE INDEX IF NOT EXISTS idx_articles_law ON articles(law_id);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
  btext, tokenize='unicode61');
"""


def open_db(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def get_law(db, source_id: str):
    return db.execute("SELECT * FROM laws WHERE source='npc' AND source_id=?",
                      (source_id,)).fetchone()


def upsert_law(db, hit, family, now_s):
    """搜索结果行→法规元数据 upsert(不动全文字段)。返回 law id。"""
    parsed = parse_title(hit["title"])
    cur = db.execute(
        """INSERT INTO laws(source, source_id, title, title_base, doc_type, revision,
             tax_family, category, issuing_authority, publish_date, effective_date,
             status_code, status, url, discovered_at, updated_at)
           VALUES('npc',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source, source_id) DO UPDATE SET
             title=excluded.title, status_code=excluded.status_code,
             status=excluded.status, updated_at=excluded.updated_at""",
        (hit["id"], hit["title"], parsed.get("base"), parsed.get("doc_type"),
         parsed.get("revision"), family, hit.get("category", ""),
         hit.get("issuing_authority", ""), hit.get("publish_date", ""),
         hit.get("effective_date", ""), hit.get("status_code"),
         hit.get("status", ""), f"https://flk.npc.gov.cn/detail?id={hit['id']}",
         now_s, now_s))
    # ON CONFLICT UPDATE 时 lastrowid 是脏值,统一回表查 id
    return get_law(db, hit["id"])["id"]


def replace_articles(db, law_id: int, articles):
    """整法规替换法条并同步 FTS(rowid 对齐 articles.id)。"""
    db.execute("DELETE FROM articles_fts WHERE rowid IN (SELECT id FROM articles WHERE law_id=?)", (law_id,))
    db.execute("DELETE FROM articles WHERE law_id=?", (law_id,))
    for a in articles:
        cur = db.execute(
            "INSERT INTO articles(law_id, seq, chapter, article_no, text) VALUES(?,?,?,?,?)",
            (law_id, a["seq"], a["chapter"], a["article_no"], a["text"]))
        db.execute("INSERT INTO articles_fts(rowid, btext) VALUES(?,?)",
                   (cur.lastrowid, bigram(a["text"])))


# ─────────────────────────── 采集层(联网) ───────────────────────────

def fetch_docx_bytes(bbbs: str):
    """签名 URL→DOCX bytes;无 word 版返回 None。"""
    url = get_download_url(bbbs, "docx")
    if not url:
        return None
    r = requests.get(url, timeout=60, verify=False)
    r.raise_for_status()
    return r.content


def _search_with_retry(keyword: str, page: int, attempts: int = 3):
    """NPC 搜索瞬时故障(读超时等)重试;tax_search 只兜 429/5xx,不兜超时。"""
    for i in range(attempts):
        try:
            return search_tax(keyword, scope="title", search_type=2, status=None,
                              page=page, size=100)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(3 * (i + 1))


def collect_family(db, seed, args, now: datetime):
    """一个税族:标题模糊搜索(全部效力状态)→族归属过滤→元数据 upsert→增量抓全文。"""
    family, keyword = seed["base"], seed["keyword"]
    bases = [family] + list(seed.get("aliases", []))  # belongs_to_family 收 base+别名列表
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    seen_ids, n_new_fulltext = set(), 0

    for page in range(1, args.max_pages + 1):
        try:
            res = _search_with_retry(keyword, page)
        except Exception as e:
            print(f"    ! {family} 第 {page} 页搜索失败(已重试),跳过该族剩余页: {e}",
                  file=sys.stderr)
            break
        rows = res.get("results", [])
        if not rows:
            break
        family_rows = [h for h in rows if belongs_to_family(h["title"], bases)]
        for hit in family_rows:
            if hit["id"] in seen_ids:
                continue
            seen_ids.add(hit["id"])
            law_id = upsert_law(db, hit, family, now_s)
            law = db.execute("SELECT * FROM laws WHERE id=?", (law_id,)).fetchone()
            if should_fetch_fulltext(law, args.refresh, now):
                try:
                    data = fetch_docx_bytes(hit["id"])
                    if not data:
                        db.execute("UPDATE laws SET parse_status='no_docx',"
                                   " fulltext_fetched_at=? WHERE id=?", (now_s, law_id))
                    else:
                        paragraphs = _parse_docx_from_bytes(data)
                        articles = segment_law(paragraphs)
                        replace_articles(db, law_id, articles)
                        db.execute(
                            "UPDATE laws SET article_count=?, parse_status='ok',"
                            " status_code_at_fetch=?, fulltext_fetched_at=? WHERE id=?",
                            (len(articles), law["status_code"], now_s, law_id))
                        n_new_fulltext += 1
                except Exception as e:
                    db.execute("UPDATE laws SET parse_status='parse_error',"
                               " fulltext_fetched_at=? WHERE id=?", (now_s, law_id))
                    print(f"    ! {hit['title'][:30]} 抓取失败: {e}", file=sys.stderr)
                time.sleep(DOCX_SLEEP_SEC)
        db.commit()
        if res.get("total", 0) <= page * 100:
            break
    return len(seen_ids), n_new_fulltext


# ─────────────────────────── 查询层 ───────────────────────────

def search_articles(db, query, tax=None, law_title=None, limit=20, only_current=False):
    sql = ("SELECT a.seq, a.article_no, a.chapter, a.text,"
           " l.title, l.tax_family, l.status, l.source_id"
           " FROM articles_fts JOIN articles a ON a.id=articles_fts.rowid"
           " JOIN laws l ON l.id=a.law_id"
           " WHERE articles_fts MATCH ?")
    params = [fts_query(query)]
    if params[0] is None:  # 单字:FTS 不适用,LIKE 兜底全表扫(本地库规模可接受)
        sql = ("SELECT a.seq, a.article_no, a.chapter, a.text,"
               " l.title, l.tax_family, l.status, l.source_id"
               " FROM articles a JOIN laws l ON l.id=a.law_id WHERE a.text LIKE ?")
        params = [f"%{query}%"]
    conds = ""
    if tax:
        conds += " AND l.tax_family LIKE ?"
        params.append(f"%{tax}%")
    if law_title:
        conds += " AND l.title LIKE ?"
        params.append(f"%{law_title}%")
    if only_current:
        conds += " AND l.status_code IN (3,4)"
    sql += conds + " ORDER BY l.status_code DESC, l.title, a.seq LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()


def find_law(db, key: str):
    row = db.execute("SELECT * FROM laws WHERE source_id=?", (key,)).fetchone()
    if row:
        return row
    return db.execute("SELECT * FROM laws WHERE title LIKE ? ORDER BY status_code DESC LIMIT 1",
                      (f"%{key}%",)).fetchone()


def show_stats(db):
    built = db.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
    print(f"库建于: {built[0] if built else '(未完整构建)'}")
    total = db.execute("SELECT COUNT(*), SUM(article_count) FROM laws").fetchone()
    print(f"法规 {total[0]} 部 / 法条 {total[1] or 0} 条\n")
    print(f"{'税族':<8}{'法规':>6}{'现行':>6}{'法条':>8}   全文解析")
    for row in db.execute(
            """SELECT tax_family, COUNT(*) n, SUM(status_code=3) cur, SUM(article_count) arts,
                      SUM(parse_status='ok') ok FROM laws GROUP BY tax_family ORDER BY n DESC"""):
        print(f"{row[0] or '-':<10}{row[1]:>5}{row[2] or 0:>6}{row[3] or 0:>8}   {row[4] or 0}/{row[1]}")


# ─────────────────────────── CLI ───────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--build", action="store_true", help="采集落库(增量)")
    p.add_argument("--tax", help="只处理指定税族(如 契税),默认全部 20 族")
    p.add_argument("--refresh", action="store_true", help="忽略 TTL 强制重抓全文")
    p.add_argument("--max-pages", type=int, default=10, help="每族最多搜索页数(页大小 100)")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--search", help="法条级检索")
    p.add_argument("--law", help="按 bbbs 或标题关键词查看全部法条")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--current", action="store_true", help="检索时只看现行有效/尚未生效")
    return p


def main():
    args = build_parser().parse_args()
    if not any([args.build, args.stats, args.search, args.law]):
        build_parser().print_help()
        return
    db = open_db(args.db)
    try:
        if args.build:
            seeds = [s for s in SEEDS if not args.tax or s["base"] == args.tax]
            if args.tax and not seeds:
                sys.exit(f"未知税族: {args.tax}(可用: {'、'.join(s['base'] for s in SEEDS)})")
            now = datetime.now()
            grand = [0, 0]
            for i, seed in enumerate(seeds, 1):
                try:
                    n_laws, n_ft = collect_family(db, seed, args, now)
                except Exception as e:
                    print(f"[{i}/{len(seeds)}] {seed['base']}: 整族采集失败,跳过 — {e}",
                          file=sys.stderr)
                    continue
                grand[0] += n_laws
                grand[1] += n_ft
                print(f"[{i}/{len(seeds)}] {seed['base']}: 新入库/更新法规 {n_laws},"
                      f" 重抓全文 {n_ft}")
            db.execute("INSERT OR REPLACE INTO meta VALUES('built_at', ?)",
                       (now.strftime("%Y-%m-%d %H:%M:%S"),))
            db.commit()
            print(f"\n完成:共 {grand[0]} 部法规,{grand[1]} 部重抓全文 → {args.db}")
        if args.stats:
            show_stats(db)
        if args.search:
            rows = search_articles(db, args.search, tax=args.tax,
                                   law_title=None, limit=args.limit,
                                   only_current=args.current)
            print(f"检索“{args.search}” 命中 {len(rows)} 条:\n")
            for r in rows:
                print(f"[{r['tax_family']}|{r['status']}] 《{r['title']}》{r['article_no']}"
                      f"{'(' + r['chapter'] + ')' if r['chapter'] else ''}")
                print(f"    {snippet(r['text'], args.search)}")
        if args.law:
            law = find_law(db, args.law)
            if not law:
                sys.exit(f"未找到法规: {args.law}")
            arts = db.execute("SELECT * FROM articles WHERE law_id=? ORDER BY seq",
                              (law["id"],)).fetchall()
            print(f"《{law['title']}》[{law['status']}] 公布 {law['publish_date']},"
                  f"施行 {law['effective_date']} — {len(arts)} 条")
            for a in arts:
                print(f"\n{a['article_no']} {'· ' + a['chapter'] if a['chapter'] else ''}")
                print(f"  {a['text']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
