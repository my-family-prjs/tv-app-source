#!/usr/bin/env python3
"""fetch_cms.py —— 拉取并测活 CMS（maccms JSON）采集接口。

流程：
1. 从 config.json 配置的上游（默认 hafrey1/LunaTV-config 仓库每天自动维护的
   LunaTV-config.json，其 api_site 字段里是各 CMS 采集接口的 name/api/detail）
   拉取接口列表，也支持在 extra_apis 里手工追加接口。
2. 用线程池并发请求每个接口的轻量列表接口（api + ?ac=videolist&pg=1），
   记录 HTTP 状态、响应耗时（毫秒）、返回 JSON 里 class/vod list 是否有效。
3. 按响应耗时升序保留最多 max_keep 个有效接口，连同失败接口的失败原因
   一起写入输出文件（默认 dist/cms_sources.json），供 build_config.py 消费。

用法：
    python3 scripts/fetch_cms.py [--config config.json] [--output dist/cms_sources.json]
    python3 scripts/fetch_cms.py --help

只使用 Python 标准库。
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 保证 scripts/ 目录在 sys.path 中，便于直接以 python3 scripts/fetch_cms.py 运行
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from common import load_config, http_get_text, project_path, write_json, log, with_github_proxy  # noqa: E402


def is_adult_entry(entry, cms_cfg):
    """按名称/域名关键词判断一个接口是否疑似成人源（测活前剔除）。

    命中返回 True。关键词在 config.json 的 cms.adult_filter.name_keywords 里维护。
    """
    adult_cfg = cms_cfg.get("adult_filter", {}) or {}
    if not adult_cfg.get("enabled", True):
        return False
    keywords = [k.lower() for k in adult_cfg.get("name_keywords", []) if k]
    if not keywords:
        return False
    haystack = " ".join([
        str(entry.get("name", "")),
        str(entry.get("key", "")),
        str(entry.get("api", "")),
        str(entry.get("detail", "")),
    ]).lower()
    return any(k in haystack for k in keywords)


def hit_adult_content(data, cms_cfg):
    """检测接口返回内容（分类名 + 影片名）是否命中成人关键词。

    data 是测活响应解析出的 JSON 字典。命中返回命中的关键词字符串，否则返回 None。
    分类名（class）是最可靠的判断依据；影片名（vod_name）也参与检测，
    影片名命中要求至少 2 个不同关键词命中或 1 个强特征词，避免正常影片误伤。
    """
    adult_cfg = cms_cfg.get("adult_filter", {}) or {}
    if not adult_cfg.get("enabled", True):
        return None
    content_keywords = [k for k in adult_cfg.get("content_keywords", []) if k]

    class_hits = []
    class_list = data.get("class") or []
    if isinstance(class_list, list):
        for item in class_list:
            class_name = ""
            if isinstance(item, dict):
                # 标准格式分类字段是 type_name；部分非标源（如大地资源）用 list_name
                class_name = str(item.get("type_name") or item.get("list_name") or "")
            elif isinstance(item, str):
                class_name = item
            for kw in content_keywords:
                if kw in class_name:
                    class_hits.append(kw)
    if class_hits:
        return "分类名命中：%s" % "、".join(sorted(set(class_hits)))

    # 影片名检测：弱特征词要求两个及以上同时出现才判定，强特征词（成人/无码/有码等）单个即判
    strong = {"成人", "无码", "有码", "情色", "三级", "妓", "援交", "麻豆传媒", "国产传媒"}
    vod_list = data.get("list") or []
    vod_hits = set()
    if isinstance(vod_list, list):
        for item in vod_list:
            vod_name = ""
            if isinstance(item, dict):
                vod_name = str(item.get("vod_name") or item.get("name") or "")
            for kw in content_keywords:
                if kw in vod_name:
                    vod_hits.add(kw)
    if len(vod_hits) >= 2:
        return "影片名命中 %d 个关键词：%s" % (len(vod_hits), "、".join(sorted(vod_hits)))
    if vod_hits & strong:
        return "影片名命中强特征词：%s" % "、".join(sorted(vod_hits & strong))
    return None


def parse_upstream_lunatv(data):
    """解析 LunaTV-config.json 格式的上游数据。

    已查证的格式（2026-10）：顶层为 {"cache_time": 7200, "api_site": {...}}，
    api_site 的每个值形如 {"name": "🎬豆瓣资源", "api": "https://.../api.php/provide/vod",
    "detail": "https://..."}。返回统一结构的接口字典列表。
    """
    apis = []
    for key, entry in (data.get("api_site") or {}).items():
        if not isinstance(entry, dict):
            continue
        api = (entry.get("api") or "").strip()
        if not api:
            continue
        name = (entry.get("name") or key).strip()
        apis.append({
            "key": str(key),
            "name": name,
            "api": api,
            "detail": (entry.get("detail") or "").strip(),
            "upstream": "LunaTV-config",
        })
    return apis


def load_upstream_apis(cms_cfg):
    """从所有启用的上游拉取接口列表并合并去重（按 api 地址去重）。"""
    collected = {}
    for source in cms_cfg.get("upstreams", []):
        if not source.get("enabled", True):
            log("上游已禁用，跳过：%s" % source.get("name"))
            continue
        url = source["url"]
        fetch_url = with_github_proxy(url, cms_cfg.get("github_proxy_prefix"))
        log("拉取上游：%s（%s）" % (source.get("name"), fetch_url))
        status, text, error = http_get_text(
            fetch_url,
            timeout=cms_cfg.get("timeout_seconds", 10),
            retries=cms_cfg.get("retries", 2),
            retry_delay=cms_cfg.get("retry_delay_seconds", 1),
            proxy=cms_cfg.get("proxy") or None,
        )
        if error is not None:
            log("  上游拉取失败：%s（继续处理其他上游）" % error)
            continue
        try:
            data = json.loads(text)
        except ValueError as exc:
            log("  上游内容不是合法 JSON：%s" % exc)
            continue
        fmt = (source.get("format") or "lunatv").lower()
        if fmt == "lunatv":
            apis = parse_upstream_lunatv(data)
        else:
            log("  未知上游格式 %s，跳过" % fmt)
            continue
        log("  解析到 %d 个接口" % len(apis))
        for item in apis:
            collected.setdefault(item["api"], item)

    for extra in cms_cfg.get("extra_apis", []):
        api = (extra.get("api") or "").strip()
        if not api:
            continue
        collected.setdefault(api, {
            "key": extra.get("key") or api,
            "name": extra.get("name") or api,
            "api": api,
            "detail": extra.get("detail", ""),
            "upstream": "extra",
        })

    # 成人源过滤（测活前先按名称/域名剔除一遍；内容级检测在测活时再做第二道）
    all_entries = list(collected.values())
    blocked = [e for e in all_entries if is_adult_entry(e, cms_cfg)]
    for entry in blocked:
        log("  成人源剔除（名称/域名命中）：%s —— %s" % (entry.get("name"), entry["api"]))
    kept = [e for e in all_entries if not is_adult_entry(e, cms_cfg)]
    log("成人源名称过滤：共 %d 个接口，剔除 %d 个，待测 %d 个" % (
        len(all_entries), len(blocked), len(kept)))
    return kept


def build_test_url(api, test_path):
    """拼接测试地址：接口自带 query 时用 & 追加，否则用 ?。"""
    test_path = test_path or "?ac=videolist&pg=1"
    if test_path.startswith("?"):
        sep = "&" if "?" in api else "?"
        return api + sep + test_path[1:]
    if test_path.startswith("&"):
        return api + test_path
    return api + ("&" if "?" in api else "?") + test_path


def check_one_api(entry, cms_cfg):
    """测活单个接口，返回结果字典（无论成败，都带 reason/错误字段）。"""
    test_url = build_test_url(entry["api"], cms_cfg.get("test_path"))
    started = time.time()
    status, text, error = http_get_text(
        test_url,
        timeout=cms_cfg.get("timeout_seconds", 10),
        retries=cms_cfg.get("retries", 2),
        retry_delay=cms_cfg.get("retry_delay_seconds", 1),
        user_agent=cms_cfg.get("user_agent"),
    )
    elapsed_ms = int((time.time() - started) * 1000)

    result = {
        "key": entry.get("key", ""),
        "name": entry.get("name", ""),
        "api": entry["api"],
        "detail": entry.get("detail", ""),
        "upstream": entry.get("upstream", ""),
        "test_url": test_url,
        "ok": False,
        "http_status": status,
        "latency_ms": elapsed_ms,
        "class_count": 0,
        "vod_count": 0,
        "reason": "",
    }

    if error is not None:
        result["reason"] = error
        return result
    if status != 200:
        result["reason"] = "HTTP 状态码非 200：%s" % status
        return result

    try:
        data = json.loads(text)
    except ValueError as exc:
        result["reason"] = "响应不是合法 JSON：%s" % exc
        return result
    if not isinstance(data, dict):
        result["reason"] = "响应 JSON 顶层不是对象"
        return result

    adult_hit = hit_adult_content(data, cms_cfg)
    if adult_hit:
        result["reason"] = "疑似成人内容（%s）" % adult_hit
        return result

    class_list = data.get("class") or []
    vod_list = data.get("list") or []
    result["class_count"] = len(class_list) if isinstance(class_list, list) else 0
    result["vod_count"] = len(vod_list) if isinstance(vod_list, list) else 0

    min_vod = cms_cfg.get("min_vod_count", 1)
    if result["vod_count"] < min_vod:
        result["reason"] = "videolist 返回的影片列表为空（%d 条，要求至少 %d 条）" % (
            result["vod_count"], min_vod)
        return result

    result["ok"] = True
    result["reason"] = "有效"
    return result


def main():
    parser = argparse.ArgumentParser(
        description="拉取上游 CMS 接口列表并并发测活，输出带延迟数据的有效接口 JSON。")
    parser.add_argument("--config", default=None, help="配置文件路径（默认为项目根目录 config.json）")
    parser.add_argument("--output", default=None, help="输出文件路径（默认取配置里的 cms.output）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多测活多少个接口（0 表示全部），便于本地快速调试")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cms_cfg = cfg.get("cms", {})
    # proxy / github_proxy_prefix 配置在顶层，下发给各网络请求环节使用
    for key in ("proxy", "github_proxy_prefix"):
        cms_cfg.setdefault(key, cfg.get(key, ""))

    log("== fetch_cms 开始 ==")
    apis = load_upstream_apis(cms_cfg)
    if args.limit > 0:
        apis = apis[:args.limit]
    if not apis:
        log("错误：没有从任何上游解析到接口，任务中止")
        sys.exit(1)
    log("合计 %d 个待测接口" % len(apis))

    concurrency = max(1, int(cms_cfg.get("concurrency", 24)))
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(check_one_api, entry, cms_cfg): entry for entry in apis}
        done_count = 0
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 理论上 check_one_api 内部已兜底
                result = {
                    "key": entry.get("key", ""), "name": entry.get("name", ""),
                    "api": entry["api"], "detail": entry.get("detail", ""),
                    "upstream": entry.get("upstream", ""), "test_url": "",
                    "ok": False, "http_status": None, "latency_ms": 0,
                    "class_count": 0, "vod_count": 0,
                    "reason": "内部异常：%s" % exc,
                }
            results.append(result)
            done_count += 1
            mark = "OK " if result["ok"] else "FAIL"
            log("[%d/%d] %s %s —— %s，%d ms%s" % (
                done_count, len(apis), mark, result["api"],
                result["reason"], result["latency_ms"],
                ("，分类 %d / 影片 %d" % (result["class_count"], result["vod_count"]))
                if result["ok"] else ""))

    ok_results = sorted(
        (r for r in results if r["ok"]),
        key=lambda r: (r["latency_ms"], r["vod_count"]),
    )
    max_keep = int(cms_cfg.get("max_keep", 40))
    kept = ok_results[:max_keep]
    dropped = ok_results[max_keep:]

    output = args.output or cms_cfg.get("output", "dist/cms_sources.json")
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tested_total": len(results),
        "ok_total": len(ok_results),
        "kept_total": len(kept),
        "dropped_by_max_keep": len(dropped),
        "sources": kept,
        "failed": [r for r in results if not r["ok"]],
    }
    write_json(project_path(output), payload)
    log("== fetch_cms 结束：测活 %d 个，有效 %d 个，保留 %d 个（输出 %s）==" % (
        len(results), len(ok_results), len(kept), output))


if __name__ == "__main__":
    main()
