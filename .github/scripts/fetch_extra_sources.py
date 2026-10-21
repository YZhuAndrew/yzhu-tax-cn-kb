#!/usr/bin/env python3
"""
Fetch extra regulation sources for the static knowledge base:
  1. fgk.chinatax.gov.cn  最新法规文件   → docs/data/fgk_latest.json
  2. kjs.mof.gov.cn + www.casc.org.cn 会计准则动态 → docs/data/accounting.json

Failure policy (mirrors fetch_tax_data.py, but never blocks the commit —
extras degrading must not stop the NPC data update):
- A failed fetch reuses the previous run's file content (marked "reused": true).
- If a fetch fails and no previous file exists, nothing is written for it.
- The script always exits 0; failures are reported on stdout.
"""
import json
import os
import re
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from fgk_chinatax import fetch_latest as fetch_fgk_latest
from mof_kjs import fetch_all as fetch_kjs
from casc_org import fetch_all as fetch_casc

DATA_DIR = os.path.join(REPO_ROOT, "docs", "data")
FGK_LATEST_COUNT = 200  # ~20 pages of 10 from search5

_TAG_RE = re.compile(r"<[^>]+>")


def _norm_title(title: str) -> str:
    """标题归一（去空白），用于 kjs/casc 跨站镜像去重。"""
    return _TAG_RE.sub("", title or "").replace(" ", "").replace("\u3000", "")


def _merge_accounting(kjs_rows: list, casc_rows: list) -> list:
    """合并会计司+准则委；同一文件两站互发，按标题去重，会计司优先。"""
    merged = []
    seen = set()
    for row in kjs_rows + casc_rows:
        key = _norm_title(row.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda x: x.get("date", ""), reverse=True)
    return merged


def _write(name: str, payload: dict) -> str:
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _reuse_previous(name: str, updated: str):
    """失败时复用上次数据；返回 (payload|None)。"""
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload.get("results"), list) and payload["results"]:
            payload["reused"] = True
            payload["updated"] = updated
            return payload
    except Exception:
        pass
    return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    updated = time.strftime("%Y-%m-%d %H:%M:%S")
    ok_count = fail_count = 0

    # ── 1. 税务总局法规库最新文件 ──────────────────────────────────────
    print(f"Fetching fgk.chinatax.gov.cn latest {FGK_LATEST_COUNT} documents ...")
    try:
        rows = fetch_fgk_latest(total=FGK_LATEST_COUNT)
        if not rows:
            raise RuntimeError("empty result")
        path = _write("fgk_latest.json", {
            "updated": updated,
            "total": len(rows),
            "source": "fgk.chinatax.gov.cn",
            "results": rows,
        })
        print(f"  ok: {len(rows)} rows -> {path} ({os.path.getsize(path)} bytes)")
        ok_count += 1
    except Exception as e:
        print(f"  FAILED: {e}")
        payload = _reuse_previous("fgk_latest.json", updated)
        if payload:
            _write("fgk_latest.json", payload)
            print(f"    -> reused previous data ({payload['total']} rows)")
        fail_count += 1

    # ── 2. 会计准则动态（会计司 + 准则委） ────────────────────────────
    print("Fetching accounting sources (kjs.mof.gov.cn + www.casc.org.cn) ...")
    try:
        kjs_rows = fetch_kjs(pages=1)
        time.sleep(0.3)
        casc_rows = fetch_casc()
        rows = _merge_accounting(kjs_rows, casc_rows)
        if not rows:
            raise RuntimeError("empty result from both sources")
        path = _write("accounting.json", {
            "updated": updated,
            "total": len(rows),
            "sources": ["kjs.mof.gov.cn", "www.casc.org.cn"],
            "results": rows,
        })
        print(f"  ok: {len(rows)} rows (kjs {len(kjs_rows)} + casc {len(casc_rows)}, after dedup) -> {path} ({os.path.getsize(path)} bytes)")
        ok_count += 1
    except Exception as e:
        print(f"  FAILED: {e}")
        payload = _reuse_previous("accounting.json", updated)
        if payload:
            _write("accounting.json", payload)
            print(f"    -> reused previous data ({payload['total']} rows)")
        fail_count += 1

    print(f"\nDone! {ok_count} ok, {fail_count} failed (failures reuse previous data; never blocks CI)")
    # Extras never block the NPC commit step
    sys.exit(0)


if __name__ == "__main__":
    main()
