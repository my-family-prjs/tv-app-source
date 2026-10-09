#!/usr/bin/env python3
"""fetch_iptv.py —— 下载、解析、过滤直播源 m3u，输出 dist/iptv.m3u。

流程：
1. 依次下载 config.json 里 iptv.sources 中启用的 m3u 地址
   （默认为 iptv-org 公开仓库的中国频道列表 cn.m3u，格式为标准
   #EXTM3U / #EXTINF，属性行含 tvg-id、tvg-logo、group-title）。
2. 解析出频道（属性 + 名称 + 播放地址），按 include_keywords / exclude_keywords
   对频道名做过滤（默认剔除带"测试"、Geo-blocked 等标记的频道）。
3. 可选：对去重后的播放地址做轻量连通性抽测（小流量 GET，
   读满若干字节即断开，失败频道剔除）。默认关闭，可用 --probe 打开。
4. 输出合并去重后的 m3u 到配置指定的输出路径（默认 dist/iptv.m3u）。

用法：
    python3 scripts/fetch_iptv.py [--config config.json] [--output dist/iptv.m3u]
                                  [--probe] [--limit 20]
    python3 scripts/fetch_iptv.py --help

只使用 Python 标准库。
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from common import load_config, http_get_text, project_path, write_text, log, with_github_proxy  # noqa: E402

M3U_HEADER = "#EXTM3U"


def parse_m3u(text):
    """解析 m3u 文本，返回频道字典列表。

    每个频道：{name, url, attrs}，attrs 保留 tvg-id / tvg-logo / group-title。
    不完整的条目（缺名称或缺地址）直接丢弃。
    """
    channels = []
    pending_attrs_line = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            pending_attrs_line = line
            continue
        if line.startswith("#"):
            continue  # 忽略 EXTVLCOPT 等其他标记行
        # 走到这里说明是地址行
        if pending_attrs_line is None:
            continue
        name, attrs = parse_extinf(pending_attrs_line)
        if name and line.lower().startswith(("http://", "https://", "rtmp://", "udp://", "rtspt://")):
            channels.append({"name": name, "url": line, "attrs": attrs})
        pending_attrs_line = None
    return channels


def parse_extinf(line):
    """解析一行 #EXTINF，返回 (频道名, 属性字典)。"""
    # 格式：#EXTINF:-1 attr="value" attr2="value2",频道名
    body = line[len("#EXTINF:"):]
    comma_index = body.find(",")
    if comma_index < 0:
        return "", {}
    attr_part = body[:comma_index].strip()
    name = body[comma_index + 1:].strip()
    attrs = {}
    index = 0
    while index < len(attr_part):
        eq_index = attr_part.find("=", index)
        if eq_index < 0:
            break
        key = attr_part[index:eq_index].strip()
        if key.startswith(":"):
            key = key[1:]
        if eq_index + 1 < len(attr_part) and attr_part[eq_index + 1] == '"':
            close_index = attr_part.find('"', eq_index + 2)
            if close_index < 0:
                break
            attrs[key] = attr_part[eq_index + 2:close_index]
            index = close_index + 1
        else:
            space_index = attr_part.find(" ", eq_index + 1)
            if space_index < 0:
                attrs[key] = attr_part[eq_index + 1:]
                index = len(attr_part)
            else:
                attrs[key] = attr_part[eq_index + 1:space_index]
                index = space_index
    return name, attrs


def parse_txt_genre(text):
    """解析 txt 分类格式（TVBox/iptv-api 常见产出格式）。

    格式：
        央视频道,#genre#
        CCTV-1,http://...
        卫视频道,#genre#
    返回频道字典列表，字段含 name、url、category（所属分类）。
    """
    channels = []
    category = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") and not line.endswith("#genre#"):
            continue
        if line.endswith("#genre#"):
            category = line.split(",", 1)[0].strip().lstrip("#").replace("#genre#", "").strip()
            continue
        comma = line.find(",")
        if comma <= 0:
            continue
        name = line[:comma].strip()
        url = line[comma + 1:].strip()
        if not name or not url.lower().startswith(("http://", "https://", "rtmp://", "udp://", "rtspt://")):
            continue
        channels.append({"name": name, "url": url, "attrs": {}, "category": category})
    return channels


def probe_url(url, probe_cfg, user_agent):
    """对单个播放地址做轻量连通性测试。

    小流量 GET（读满 read_limit_bytes 即断开），返回 (url, ok, 描述)。
    """
    status, body, error = None, b"", None
    # 先试 HEAD；很多流媒体服务器不支持 HEAD 或返回异常状态，失败再回退 GET
    status, _, error = probe_once(url, "HEAD", probe_cfg, user_agent)
    if status is not None and 200 <= status < 400:
        return url, True, "HEAD %s" % status
    status, body, error = probe_once(
        url, "GET", probe_cfg, user_agent, probe_cfg.get("read_limit_bytes", 65536))
    if error is not None:
        return url, False, error
    if status is None or not (200 <= status < 400):
        return url, False, "HTTP 状态码 %s" % status
    if len(body) == 0:
        return url, False, "响应体为空"
    return url, True, "GET %s，读到 %d 字节" % (status, len(body))


def probe_once(url, method, probe_cfg, user_agent, max_bytes=0):
    from common import http_get
    return http_get(
        url, timeout=probe_cfg.get("timeout_seconds", 6), retries=0,
        user_agent=user_agent, max_bytes=max_bytes, method=method)


def main():
    parser = argparse.ArgumentParser(
        description="下载并合并多个直播源 m3u，按关键词过滤、可选连通性抽测后输出 dist/iptv.m3u。")
    parser.add_argument("--config", default=None, help="配置文件路径（默认为项目根目录 config.json）")
    parser.add_argument("--output", default=None, help="输出文件路径（默认取配置里的 iptv.output）")
    parser.add_argument("--probe", action="store_true",
                        help="强制启用连通性抽测（覆盖配置里的 probe.enabled）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多探测多少个去重后的地址（0 表示按配置，配置为 0 表示全部）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    iptv_cfg = cfg.get("iptv", {})
    # proxy / github_proxy_prefix 配置在顶层，下发给各网络请求环节使用
    for key in ("proxy", "github_proxy_prefix"):
        iptv_cfg.setdefault(key, cfg.get(key, ""))

    log("== fetch_iptv 开始 ==")
    all_channels = []
    for source in iptv_cfg.get("sources", []):
        if not source.get("enabled", True):
            log("直播源已禁用，跳过：%s" % source.get("name"))
            continue
        url = source["url"]
        fetch_url = with_github_proxy(url, iptv_cfg.get("github_proxy_prefix"))
        log("下载直播源：%s（%s）" % (source.get("name"), fetch_url))
        status, text, error = http_get_text(
            fetch_url,
            timeout=iptv_cfg.get("timeout_seconds", 10),
            retries=iptv_cfg.get("retries", 2),
            retry_delay=iptv_cfg.get("retry_delay_seconds", 1),
            user_agent=iptv_cfg.get("user_agent"),
            proxy=iptv_cfg.get("proxy") or None,
        )
        if error is not None:
            log("  下载失败：%s（继续处理其他源）" % error)
            continue
        channels = parse_m3u(text)
        fmt = (source.get("format") or "auto").lower()
        if fmt == "txt":
            channels = parse_txt_genre(text)
        elif fmt == "m3u":
            channels = parse_m3u(text)
        else:  # auto：按内容特征判断格式
            channels = parse_txt_genre(text) if "#genre#" in text else parse_m3u(text)
        log("  解析到 %d 个频道（%s 格式）" % (len(channels), fmt if fmt != "auto" else "自动识别"))

        # 单源白名单：m3u 类国际源里只保留名字命中关键词的频道（如 iptv-org 只留 CCTV/卫视）
        source_whitelist = [k.lower() for k in source.get("whitelist", []) if k]
        if source_whitelist:
            before = len(channels)
            channels = [c for c in channels
                        if any(k in c["name"].lower() for k in source_whitelist)]
            log("  单源白名单过滤：%d -> %d 个" % (before, len(channels)))

        for channel in channels:
            channel.setdefault("category", channel["attrs"].get("group-title", ""))
            channel["source"] = source.get("name", url)
        all_channels.extend(channels)

    if not all_channels:
        log("错误：没有从任何来源解析到频道，任务中止")
        sys.exit(1)

    # 关键词过滤（对频道名大小写不敏感）
    include_keywords = [k.lower() for k in iptv_cfg.get("include_keywords", []) if k]
    exclude_keywords = [k.lower() for k in iptv_cfg.get("exclude_keywords", []) if k]
    filtered = []
    dropped_include = dropped_exclude = 0
    for channel in all_channels:
        name_lower = channel["name"].lower()
        if include_keywords and not any(k in name_lower for k in include_keywords):
            dropped_include += 1
            continue
        if exclude_keywords and any(k in name_lower for k in exclude_keywords):
            dropped_exclude += 1
            continue
        filtered.append(channel)
    log("关键词过滤：保留 %d 个（不含关键词剔除 %d，含排除词剔除 %d）" % (
        len(filtered), dropped_include, dropped_exclude))

    # 分类白名单：只保留分类名命中正则的频道（txt 分类源里可剔除港澳台、成人等非国内常规分类）
    import re
    category_patterns = [re.compile(p, re.IGNORECASE)
                         for p in iptv_cfg.get("category_whitelist", []) if p]
    if category_patterns:
        before = len(filtered)
        kept = []
        for channel in filtered:
            category = channel.get("category", "")
            # 无分类信息的频道（如普通 m3u 源）不受分类白名单约束
            if not category or any(p.search(category) for p in category_patterns):
                kept.append(channel)
        filtered = kept
        log("分类白名单过滤：%d -> %d 个" % (before, len(filtered)))

    # 按分类优先级排序（category_order 里越靠前的分类排越前），组内保持原有顺序
    category_order = iptv_cfg.get("category_order", [])
    def category_rank(channel):
        category = channel.get("category", "")
        for index, prefix in enumerate(category_order):
            if prefix in category:
                return index
        return len(category_order)
    filtered.sort(key=category_rank)

    # 按播放地址去重，保留先出现的（即优先级靠前的源）
    seen_urls = set()
    unique_channels = []
    for channel in filtered:
        if channel["url"] in seen_urls:
            continue
        seen_urls.add(channel["url"])
        unique_channels.append(channel)
    log("按地址去重：%d -> %d 个频道" % (len(filtered), len(unique_channels)))

    # 可选连通性抽测
    probe_cfg = dict(iptv_cfg.get("probe", {}) or {})
    if args.probe:
        probe_cfg["enabled"] = True
    if probe_cfg.get("enabled", False):
        targets = [ch["url"] for ch in unique_channels]
        sample_limit = int(probe_cfg.get("sample_limit", 0))
        if args.limit > 0:
            sample_limit = args.limit
        if sample_limit > 0:
            targets = targets[:sample_limit]
        log("连通性抽测：%d 个地址，并发 %d" % (
            len(targets), probe_cfg.get("concurrency", 16)))
        probe_results = {}
        with ThreadPoolExecutor(max_workers=int(probe_cfg.get("concurrency", 16))) as pool:
            futures = {pool.submit(probe_url, u, probe_cfg, iptv_cfg.get("user_agent")): u
                       for u in targets}
            done = 0
            for future in as_completed(futures):
                url, ok, detail = future.result()
                probe_results[url] = (ok, detail)
                done += 1
                if not ok:
                    log("  [%d/%d] FAIL %s —— %s" % (done, len(targets), url, detail))
        alive = [ch for ch in unique_channels
                 if ch["url"] not in probe_results or probe_results[ch["url"]][0]]
        dead_count = len(unique_channels) - len(alive)
        log("抽测完成：剔除 %d 个不通地址，剩余 %d 个频道" % (dead_count, len(alive)))
        unique_channels = alive
    else:
        log("连通性抽测未启用（probe.enabled=false，可用 --probe 打开）")

    # 输出 m3u
    github_prefix = iptv_cfg.get("github_proxy_prefix") or ""
    lines = [M3U_HEADER]
    for channel in unique_channels:
        attrs = []
        for attr_key in ("tvg-id", "tvg-logo"):
            value = channel["attrs"].get(attr_key)
            if value:
                # 台标等 GitHub 图片地址统一改写到配置的代理前缀，避免第三方代理失效
                if attr_key == "tvg-logo" and github_prefix:
                    raw_index = value.find("raw.githubusercontent.com")
                    if raw_index >= 0:
                        # 兼容已被其他代理包装过的形式（https://某代理/https://raw.githubusercontent.com/...）
                        value = with_github_proxy("https://" + value[raw_index:], github_prefix)
                attrs.append('%s="%s"' % (attr_key, value))
        # 分类优先级：txt 分类源的 category > m3u 自带 group-title > 来源名
        group_title = (channel.get("category")
                       or channel["attrs"].get("group-title")
                       or channel.get("source", "其他"))
        attrs.append('group-title="%s"' % group_title)
        lines.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), channel["name"]))
        lines.append(channel["url"])
    text_out = "\n".join(lines) + "\n"

    output = args.output or iptv_cfg.get("output", "dist/iptv.m3u")
    write_text(project_path(output), text_out)
    log("== fetch_iptv 结束：共输出 %d 个频道（输出 %s）==" % (len(unique_channels), output))


if __name__ == "__main__":
    main()
