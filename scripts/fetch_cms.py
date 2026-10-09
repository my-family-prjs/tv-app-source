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
from common import load_config, http_get_text, project_path, write_json, log  # noqa: E402


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
        log("拉取上游：%s（%s）" % (source.get("name"), url))
        status, text, error = http_get_text(
            url,
            timeout=cms_cfg.get("timeout_seconds", 10),
            retries=cms_cfg.get("retries", 2),
            retry_delay=cms_cfg.get("retry_delay_seconds", 1),
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

    return list(collected.values())


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
