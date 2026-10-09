# source-dist —— 视频源分发项目

本项目是一个"源分发"后端：利用 GitHub Actions 每天定时采集并测活视频源，把结果提交到仓库，再通过 jsDelivr CDN 分发给安卓 TV 影视 App（如 FongMi/TV 影視TV）读取。全程不需要自己的服务器。

产出两个文件：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `dist/tvbox.json` | 点播站点列表（maccms JSON 采集接口）+ 直播配置 | App 的"配置地址"填这个 |
| `dist/iptv.m3u` | 直播源 m3u 播放列表 | 由 tvbox.json 内的 lives 引用，也可单独使用 |

另有一个中间产物 `dist/cms_sources.json`，是 CMS 接口测活的原始结果（含每个接口的延迟数据、失败原因），供 `build_config.py` 消费，也可用来人工排查。

## 整体架构

```
                 每天 03:23 UTC（GitHub Actions cron）
                              │
                              ▼
              ┌───────────────────────────────────┐
              │  scripts/fetch_cms.py             │
              │  ├─ 上游：hafrey1/LunaTV-config    │
              │  │   （每天自动维护的 CMS 接口库）  │
              │  └─ 并发测活（ac=videolist，       │
              │     记录状态/耗时/分类与影片数）    │──▶ dist/cms_sources.json
              └───────────────────────────────────┘
              ┌───────────────────────────────────┐
              │  scripts/fetch_iptv.py            │
              │  ├─ 上游：iptv-org cn.m3u 等多个   │
              │  ├─ 解析 / 关键词过滤 / 地址去重    │
              │  └─ 可选：轻量连通性抽测           │──▶ dist/iptv.m3u
              └───────────────────────────────────┘
              ┌───────────────────────────────────┐
              │  scripts/build_config.py          │
              │  组装 FongMi/TV 格式配置           │──▶ dist/tvbox.json
              └───────────────────────────────────┘
                              │
                              ▼
                 Actions 自动 commit + push 到 main
                              │
                              ▼
        jsDelivr CDN 分发（缓存约 12 小时，随仓库更新刷新）
                              │
                              ▼
        App（FongMi/TV）填入配置地址即可加载点播与直播
```

## 分发地址（多通道，App 端任选或备用）

仓库公开且默认分支为 `main` 时，同一份产物有多个可访问的通道，按国内可访问性排列：

**通道一：GitHub 镜像代理（国内直连最稳）**

在 raw 地址前加镜像前缀，前缀可以是现成镜像站（如 `https://gh-proxy.com/`），也可以是自建的 githubproxy（Cloudflare Workers 部署，最可控）：

```
https://gh-proxy.com/https://raw.githubusercontent.com/<用户名>/<仓库名>/main/dist/tvbox.json
https://你的自建代理域名/https://raw.githubusercontent.com/<用户名>/<仓库名>/main/dist/tvbox.json
```

**通道二：jsDelivr CDN（海外稳定，国内时好时坏）**

```
https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/dist/tvbox.json
```

- `@main` 可以换成具体的 commit 哈希或 tag；用分支名时 jsDelivr 有约 12 小时缓存，仓库更新后不会立刻生效，想立即刷新可访问一次 `https://purge.jsdelivr.net/gh/<用户名>/<仓库名>@main/dist/tvbox.json`。
- `dist/` 目录必须随仓库提交（哪怕内容为空时只有 `.gitkeep` 占位），否则 CDN 上没有这些路径。
- 把仓库设为 Public 才能走 jsDelivr 的 gh 分发；Private 仓库无法通过 jsDelivr 访问。
- 公开仓库意味着任何人都能读到这份源列表，这通常正是"分发"的目的，但也请结合下面的法律风险提示自行判断。

**通道三（规划中）：Cloudflare Worker + D1**

Worker 提供接口，D1 存源列表与更新时间记录，绑定自定义域名后国内可访问，作为长期方案，见 tv-plan 项目后续开发。

App（FongMi）支持在配置里填主地址，并在设置中手动切换备用地址，建议主用通道一、备用通道二。

## 部署步骤

前置说明：GitHub 只识别**仓库根目录**下的 `.github/workflows/`，所以本目录里的 workflow 文件在部署时需要放到仓库根。两种部署方式任选其一：

**方式 A（推荐）：把 source-dist 的内容直接作为仓库根**

1. 在 GitHub 上新建一个 Public 仓库（例如 `source-dist`），不要勾选自动生成 README 的选项（或之后自行处理冲突）。
2. 把本目录（`source-dist/`）里的**全部内容**（`config.json`、`scripts/`、`dist/`、`.github/`）拷贝到你的仓库根目录。此时 workflow 文件已经位于 `仓库根/.github/workflows/update-sources.yml`，无需移动。
3. 执行 git 提交与推送（git 操作请由你自己完成，本项目脚本与文档不代做）：
   ```bash
   git add .
   git commit -m "init: 视频源分发项目"
   git push origin main
   ```
4. 在仓库页面打开 **Settings → Actions → General**，在 "Actions permissions" 里确认 Actions 是允许运行的（默认允许）；Workflow permissions 一项保持默认（"Read repository contents and packages permissions"）即可，因为本项目的 workflow 文件内部已单独声明 `permissions: contents: write`，只授予了写仓库内容的权限。
5. 打开 **Actions** 标签页，选中左侧 "update-sources" 工作流，点击 **Run workflow** 手动触发一次，验证能跑通并在 `dist/` 里产出三个文件。
6. App 里把配置地址填成上面的 jsDelivr 地址（或直接填 `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/dist/tvbox.json`，无 CDN 缓存，更新即时生效，适合测试阶段）。

**方式 B：source-dist 作为已有仓库的子目录**

1. 把整个 `source-dist/` 目录并入你的仓库。
2. **必须**把 `source-dist/.github/workflows/update-sources.yml` 移动（或复制）到仓库根的 `.github/workflows/update-sources.yml`，否则 GitHub 不会识别这个定时任务。
3. 由于脚本在子目录里，需要把 workflow 中三个运行步骤的命令改为带路径的版本，例如：
   ```yaml
   - name: 采集并测活 CMS 点播接口
     run: python3 source-dist/scripts/fetch_cms.py --config source-dist/config.json
   - name: 采集直播源 m3u
     run: python3 source-dist/scripts/fetch_iptv.py --config source-dist/config.json
   - name: 组装 tvbox.json
     run: python3 source-dist/scripts/build_config.py --config source-dist/config.json
   ```
   同时把 `add: "dist"` 改为 `add: "source-dist/dist"`。（脚本内部用相对项目根的方式解析配置里的 `dist/...` 输出路径，用 `--config` 显式指定后，输出仍会落在各自脚本目录所对应项目根的 `dist/` 下，即 `source-dist/dist/`。）
4. 其余步骤同方式 A 的 4～6。

**定时任务的说明**：cron 表达式为 `23 3 * * *`（UTC 03:23，北京时间约 11:23），特意避开整点（GitHub Actions 在整点附近调度拥挤、延迟明显）。GitHub 的定时任务不保证分秒准时，可能延迟几分钟到十几分钟，属正常现象。

## 本地手动运行

所有脚本只用 Python 3 标准库，无需安装任何第三方包：

```bash
python3 scripts/fetch_cms.py --help     # 采集并测活 CMS 接口
python3 scripts/fetch_iptv.py --help    # 采集直播源 m3u
python3 scripts/build_config.py --help  # 组装 tvbox.json

# 本地快速调试（只测前 3 个接口）：
python3 scripts/fetch_cms.py --limit 3
# 强制开启直播源连通性抽测：
python3 scripts/fetch_iptv.py --probe
```

## 配置说明（config.json）

- `cms.upstreams`：CMS 接口的上游来源。默认使用 hafrey1/LunaTV-config 每天自动检测后发布的 `LunaTV-config.json`（格式为 `{"cache_time": ..., "api_site": {键: {"name","api","detail"}}}`，脚本取 `api_site` 里每个条目的 `api` 字段作为采集接口地址）。
- `cms.extra_apis`：手工追加的接口，格式为 `{"name": "...", "api": "https://.../api.php/provide/vod"}`。
- `cms.test_path`：测活用的轻量接口路径，默认 `?ac=videolist&pg=1`（maccms JSON 接口的视频列表接口）。
- `cms.timeout_seconds`（默认 10）、`cms.retries`（默认 2）、`cms.concurrency`（默认 24）：单接口超时、失败重试次数与并发数。
- `cms.max_keep`（默认 40）：测活通过后按响应耗时升序最多保留多少个接口。
- `cms.min_vod_count`（默认 1）：videolist 至少要返回几条影片才算有效。
- `iptv.sources`：直播源 m3u 地址列表，默认为 iptv-org 的中国频道 `cn.m3u`，可自行增删（例如加上 Guovin/iptv-api 等项目发布的 m3u 产物的直链）。
- `iptv.include_keywords` / `iptv.exclude_keywords`：按频道名关键词过滤，默认剔除带 "测试"、"Geo-blocked" 等标记的频道。
- `iptv.probe`：连通性抽测开关与参数。默认关闭（`enabled: false`），因为 GitHub Actions 的服务器在海外，部分国内频道测不通不代表频道真失效；开启后会对每个去重地址做小流量 GET，读满 64KB 即断开，剔除不通的频道。
- `tvbox.*`：tvbox.json 的组装参数（站点超时、直播配置名称与 m3u 引用路径、输出位置）。`live_url` 默认 `./iptv.m3u`，FongMi/TV 会以 tvbox.json 自身位置展开这个相对路径，因此本地与 CDN 下都能正确加载。

## tvbox.json 格式依据

字段以 FongMi/TV（影視TV）官方配置文档（`https://fongmi.github.io/TV/config/`）为准，已在写脚本前实际查证：

- 顶层为 VodConfig，最小可用结构是 `{"sites": [...], "lives": [...]}`。
- `sites[]`：`key`（唯一标识，脚本用接口域名生成并保证不重复）、`name`（显示名）、`type`（**0=XML HTTP、1=JSON HTTP、3=Spider**。maccms JSON 采集接口返回 JSON，因此脚本写入 `type: 1`；`type: 0` 是 maccms XML 接口，不适用于 JSON 接口）、`api`（接口地址）、`searchable`/`changeable`（1，允许搜索与切换）、`timeout`（播放超时秒数）。
- `lives[]`：`name`（直播配置名）+ `url`（外部 m3u 列表地址，`./` 相对路径以配置文件位置展开）。App 会在加载直播时自行解析 m3u 内的 `tvg-id`、`tvg-logo`、`group-title` 属性。

## 目录结构

```
source-dist/
├── README.md                          # 本文件
├── config.json                        # 上游来源与参数配置
├── scripts/
│   ├── common.py                      # 公共工具（配置加载、HTTP、日志）
│   ├── fetch_cms.py                   # CMS 接口采集 + 并发测活
│   ├── fetch_iptv.py                  # 直播源 m3u 采集 + 过滤 + 抽测
│   └── build_config.py                # 组装 tvbox.json
├── .github/workflows/update-sources.yml  # 定时工作流（部署时须在仓库根的 .github/workflows/ 下）
└── dist/                              # 脚本产出目录（由工作流自动提交）
    └── .gitkeep                       # 占位文件，保证空目录能入库
```

`dist/` 是纯产出目录，三个脚本依次运行后会在其中生成 `cms_sources.json`、`iptv.m3u`、`tvbox.json`。本地运行前请先确保 `dist/` 目录存在（已用 `.gitkeep` 占位）。

## 法律风险提示

- 本项目**不提供、不存储、不分发任何影视内容本身**，只输出"采集接口的地址列表"和"直播频道的 m3u 索引"。这些接口与频道由第三方维护，其内容的合法性、安全性本项目无法控制。
- 众多公开的 CMS 采集接口与直播源**很可能包含未经授权的版权内容**。在不同司法辖区，抓取、聚合、再分发此类来源，以及通过自己公开的仓库/CDN 地址把这类内容提供给他人使用，可能构成侵犯著作权或违反当地法律法规；即使你只是"搬运地址列表"，公开分发行为本身也可能带来法律风险。
- 上游项目（LunaTV-config、iptv-org 等）的数据同样来自公开网络抓取，可能随时变更格式或失效，也可能被权利人主张下架。
- 请务必自行了解并遵守所在地区的法律法规，评估自身风险；如有可能，优先使用你确有权利访问和分发的内容来源。使用本项目即表示你理解并自行承担全部风险，本项目作者不承担任何责任。
- 另外，公开仓库的 Actions 运行日志与产出文件任何人可见，请勿在 config.json 或脚本参数中填入任何私密信息。
