#!/usr/bin/env python3
"""build_config.py —— 把 fetch_cms.py 的测活结果组装成 tvbox.json。

字段依据（2026-10 已在 FongMi/TV 官方配置文档 fongmi.github.io/TV/config/ 查证）：
- 顶层为 VodConfig：{"sites": [...], "lives": [...]}。
- sites[] 的 Site 对象：type 字段 0=XML HTTP、1=JSON HTTP、3=Spider。
  maccms JSON 采集接口（.../api.php/provide/vod）返回 JSON，因此使用 type=1
  （注意：type=0 是 maccms XML 接口，不要与 JSON 接口混用）。
  key 为唯一标识，name 为显示名，api 为接口地址，searchable/changeable 保持 1，
  timeout 为播放超时秒数。
- lives[] 的 Live 对象：name 为直播配置名，url 指向外部 m3u 列表。
  主配置里的 "./" 相对路径会以配置文件自身位置展开，
  因此 "./iptv.m3u" 在本地与 jsDelivr CDN 下都能正确定位到同目录的 iptv.m3u。

用法：
    python3 scripts/build_config.py [--config config.json] [--output dist/tvbox.json]
    python3 scripts/build_config.py --help

只使用 Python 标准库。
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from common import load_config, project_path, write_json, log  # noqa: E402

EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿️]"
)


def slugify_key(name, api, used_keys):
    """为站点生成稳定的唯一 key：优先用去掉协议后的域名，冲突时加短哈希。"""
    host = ""
    match = re.match(r"https?://([^/?#]+)", api)
    if match:
        host = match.group(1)
    else:
        host = hashlib.md5(api.encode("utf-8")).hexdigest()[:12]
    host = re.sub(r"[^A-Za-z0-9_]+", "_", host).strip("_").lower() or "site"
    key = host
    while key in used_keys:
        digest = hashlib.md5((api + key).encode("utf-8")).hexdigest()[:6]
        key = "%s_%s" % (host, digest)
    used_keys.add(key)
    return key


def clean_name(name, api):
    """去掉名称里的表情符号等装饰字符；为空时回退为域名。"""
    cleaned = EMOJI_PATTERN.sub("", name).strip().strip("-—_ ").strip()
    if not cleaned:
        match = re.match(r"https?://([^/?#]+)", api)
        cleaned = match.group(1) if match else api
    return cleaned


def main():
    parser = argparse.ArgumentParser(
        description="把 fetch_cms.py 产出的有效接口列表组装为 FongMi/TV 可用的 tvbox.json。")
    parser.add_argument("--config", default=None, help="配置文件路径（默认为项目根目录 config.json）")
    parser.add_argument("--input", default=None,
                        help="fetch_cms 产出文件路径（默认取配置里的 tvbox.cms_sources_input）")
    parser.add_argument("--output", default=None, help="输出文件路径（默认取配置里的 tvbox.output）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tvbox_cfg = cfg.get("tvbox", {})

    input_path = project_path(args.input or tvbox_cfg.get(
        "cms_sources_input", "dist/cms_sources.json"))
    if not os.path.isfile(input_path):
        log("错误：找不到 fetch_cms 的产出文件 %s，请先运行 scripts/fetch_cms.py" % input_path)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as fh:
        cms_data = json.load(fh)

    sources = cms_data.get("sources", [])
    if not sources:
        log("错误：%s 里没有任何有效接口（ok_total=%s），任务中止" % (
            input_path, cms_data.get("ok_total")))
        sys.exit(1)

    log("== build_config 开始：读取 %d 个有效 CMS 接口 ==" % len(sources))

    site_timeout = int(tvbox_cfg.get("site_timeout_seconds", 15))
    used_keys = set()
    sites = []
    for source in sources:
        api = source.get("api", "")
        if not api:
            continue
        sites.append({
            "key": slugify_key(source.get("name", ""), api, used_keys),
            "name": clean_name(source.get("name", ""), api),
            "type": 1,  # FongMi 配置字典：0=XML HTTP，1=JSON HTTP；maccms JSON 接口用 1
            "api": api,
            "searchable": 1,
            "changeable": 1,
            "timeout": site_timeout,
        })

    live_url = tvbox_cfg.get("live_url", "./iptv.m3u")
    live = {"name": tvbox_cfg.get("live_name", "直播源"), "url": live_url}
    live_epg = tvbox_cfg.get("live_epg", "")
    if live_epg:
        live["epg"] = live_epg

    tvbox = {
        "sites": sites,
        "lives": [live],
    }

    output = args.output or tvbox_cfg.get("output", "dist/tvbox.json")
    write_json(project_path(output), tvbox)

    log("已生成 %d 个点播站点（type=1，maccms JSON），1 个直播配置（%s）" % (len(sites), live_url))
    log("== build_config 结束：输出 %s ==" % output)


if __name__ == "__main__":
    main()
