#!/usr/bin/env python3
"""
fgk.mof.gov.cn — 财政部财政法规管理系统（财政法规库）探测模块。

现状（2026-08 探测结论）：
- 站点在线，但仅支持 http，且对 UA 挑剔（需完整 Chrome UA）。
- 已改版为 layui SPA（入口 /ui/start/#/outerHomePage/outerHome），
  数据全部由前端 XHR 异步加载；旧接口 /lawinfo/*.do 全部 404。
- 视图 JS 模块路径无法静态定位（views/outerHomePage/*.html 可取，
  但对应 *.js 均返回 404），XHR 数据接口未能逆向 → 本模块只提供
  可达性探测，不提供结构化抓取。
- 数据获取降级方案：Bing site:fgk.mof.gov.cn 间接搜索（见
  tax_server.py 解读搜索源列表）。

二期若要直连：用浏览器 DevTools Network 面板抓"新法速递/法规类别导引"
页面的 XHR 请求（layui admin 的 request 封装，response 结构为
{code:200, msg, data}），再在 fetch_latest() 中实现。

Usage:
  python mof_fgk.py            # 探测可达性
"""

import argparse
import json
import re
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "http://fgk.mof.gov.cn"
# 站点会拒绝精简 UA，必须带完整 Chrome UA
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 20


def check_available() -> dict:
    """探测财政法规库可达性，返回状态信息（不做数据抓取）。"""
    try:
        r = requests.get(BASE_URL, headers=HEADERS, timeout=TIMEOUT, verify=False)
        spa_redirect = re.search(r"window\.location\.href='([^']+)'", r.text or "")
        return {
            "available": r.status_code == 200,
            "http_status": r.status_code,
            "url": BASE_URL,
            "mode": "SPA (JS-rendered, XHR data not yet reverse-engineered)",
            "spa_entry": spa_redirect.group(1) if spa_redirect else "",
            "structured_fetch": False,
            "fallback": "Bing site:fgk.mof.gov.cn",
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except requests.RequestException as e:
        return {
            "available": False,
            "url": BASE_URL,
            "error": str(e),
            "structured_fetch": False,
            "fallback": "Bing site:fgk.mof.gov.cn",
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="财政部财政法规库可达性探测")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    info = check_available()
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    status = "✅ 在线" if info["available"] else "❌ 不可达"
    print(f"{status} {BASE_URL}")
    if info.get("spa_entry"):
        print(f"  SPA 入口: {info['spa_entry']}")
    if info.get("error"):
        print(f"  错误: {info['error']}")
    if info["structured_fetch"]:
        print("  结构化抓取: 支持")
    else:
        print(f"  结构化抓取: 暂不支持（XHR 未逆向），降级 {info['fallback']}")


if __name__ == "__main__":
    main()
