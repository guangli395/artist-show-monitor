# 从零搭建一个云端 24/7 关键词监控推送系统

> 一份可复用的工程笔记。这次做的是"艺人演出 + 特朗普新闻"监控,
> 但同样的架构能复用到:监控竞品价格、追踪行业新闻、监听某 GitHub
> 仓库 issue、监控自家服务故障公告、跟踪某项政策动态... 任何
> "网上某处有更新就告诉我"的需求,都能套这个模板。

---

## 0. 一图看懂

```
       ┌─────────────────────────────────────┐
       │  config.json  (关键词 / 平台开关 / 间隔)  │
       └─────────────────┬───────────────────┘
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   桔子密码票务       新浪艺人聚合        Google News RSS
   (静态 HTML)       (静态 HTML)        (RSS XML)
        │                │                │
        └────────────────┼────────────────┘
                         ▼
              ┌──────────────────┐
              │ 增量去重 + 静默吸收 │
              │   seen.json       │
              └──────┬───────────┘
                     │ 仅"真·新增"
                     ▼
        ┌────────────────────────┐
        │ 推送层 (并联多通道)        │
        ├────────────────────────┤
        │ 企业微信群机器人 (主)      │
        │ PushPlus (次)           │
        └──────┬─────────────────┘
               │
               ▼
         手机企业微信收消息
                     ▲
                     │
       ┌─────────────┴────────────────────┐
       │  GitHub Actions  cron 每 30 分钟  │
       │  - 跑 monitor.py --once --ci      │
       │  - 自动 commit seen.json/latest.md │
       └────────────────────────────────────┘
```

**核心数据流**: 配置 → 多源抓取 → 去重 → 推送
**核心调度模型**: GitHub Actions cron 拉起短任务 (无常驻进程)
**核心持久化**: seen.json 直接 commit 回仓库 (零额外存储)

---

## 1. 选型决策(为什么这么选)

| 维度 | 选择 | 替代方案 | 决策理由 |
|------|------|---------|---------|
| **运行平台** | GitHub Actions | 阿里云 / 自家 VPS / Replit | 完全免费、自带境外节点、自带 cron、Secret 管理、日志可见、commit 可做持久化 |
| **数据源** | 静态 HTML + RSS | 官方 API / Selenium 爬虫 | 选型时**刻意避开**带反爬的页面(showstart/大麦的 x5secdata),选无反爬的页面省心 |
| **去重存储** | `seen.json` 直 commit | Redis / 云数据库 | 监控量小(几百条以内),文件即数据库,零运维零成本 |
| **推送通道** | 企业微信群机器人 | 钉钉 / 飞书 / 短信 / 邮件 / PushPlus | 不需实名认证、个人也能注册企业微信、群机器人 webhook 5 秒搞定 |
| **配置形式** | `config.json` | 环境变量 / YAML | JSON 改起来直观,GitHub 网页里能直接编辑 |
| **语言** | Python + requests | Node.js / Go | 标准库丰富 (xml.etree, hashlib),无依赖最少省心 |

### 关键避坑

1. **PushPlus 微信公众号坑**: 它要求**接收者**实名认证(腾讯风控),而非发送者。所以你用自己的 token 发,粉丝/接收方未实名就拒收。**直接放弃 PushPlus,选企业微信机器人**。

2. **showstart / 大麦反爬坑**: 阿里系 x5secdata 风控指纹,需 curl_cffi 伪造 TLS 指纹,不稳。**直接绕开,改用桔子密码、新浪聚合页这种静态页**。

3. **Twitter / X 数据**: 国内访问问题大,正版 API 又贵。**用 Google News RSS 间接覆盖**(因为路透/BBC等会转发推文要点)。

4. **GitHub 国内推送不稳**: 推 git 可能被墙,**写代码阶段要本地有 VPN**;但 Actions 跑在境外 runner 上,**生产运行不受国内网络影响**。

---

## 2. 项目目录结构

```
artist-show-monitor/
├── monitor.py                      # 主脚本 (1500 行, 抓取 + 去重 + 推送 + CLI)
├── config.json                     # 用户可改配置
├── seen.json                       # 增量去重状态 (Actions 自动 commit)
├── latest.md                       # 最近一轮的报告 (Actions 自动 commit)
├── requirements.txt                # Python 依赖 (只有一个 requests)
├── README.md                       # 给用户看的快速上手
├── BUILD_FROM_ZERO.md              # 本文档 (复盘 + 复用蓝图)
├── monitor.log                     # 运行日志 (gitignore)
├── .gitignore
└── .github/
    └── workflows/
        └── monitor.yml             # Actions 工作流定义
```

### 各文件职责

| 文件 | 职责 |
|------|------|
| `monitor.py` | 抓取(`fetch_xxx`) → 规范化 → 去重 → 输出/推送 |
| `config.json` | **唯一可改文件**: 关键词、启用哪些平台、轮询间隔、艺人拼音映射、Google News 关键词 |
| `seen.json` | `set` 序列化, 每条记录形如 `"sina:wb_12345"` |
| `latest.md` | 最新一轮发现的 markdown 报告 |
| `monitor.yml` | 定义何时跑、跑什么命令、注入哪些 secret、跑完 commit 什么 |

---

## 3. 代码核心抽象(看这一节就懂全部)

### 3.1 统一的 item 数据结构

每个数据源返回 `list[dict]`,每个 dict 字段约定如下:

```python
{
    "id": "sina-wb_12345",         # 必填: 全局唯一, 用于去重
    "source": "sina",              # 必填: 数据源名
    "keyword": "王一博",           # 必填: 命中的关键词
    "title": "王一博 9 月演唱会信息",
    "url": "https://...",          # 推送/详情链接
    "city": "北京",                # 可选
    "venue": "工人体育场",          # 可选 (Google News 复用此字段塞媒体名)
    "date": "2026-09-15 19:30",    # 可选
    "price_range": "380-1880",     # 可选 (兼容 price 字段)
    "status": "在售",              # 可选 (染色显示)
    "address": "...",              # 可选
}
```

**好处**: 所有 `fetch_xxx` 返回同一种结构,后面去重/打印/推送函数都不用知道源头。

### 3.2 数据源函数模板

```python
def fetch_xxx(keyword, debug=False):
    """从 xxx 平台抓 keyword 相关条目, 返回标准 item list。"""
    try:
        r = requests.get(URL.format(quote(keyword)), headers=HEADERS, timeout=20)
        r.raise_for_status()
        r.encoding = "utf-8"
    except Exception as e:
        log(f"  [xxx] '{keyword}' 拉取失败: {e}", "ERROR")
        return []

    # 解析: 正则 / BeautifulSoup / xml.etree / json.loads(...)
    raw = parse(r.text)

    result = []
    for raw_item in raw:
        if keyword not in raw_item.get("title", ""):
            continue
        result.append({
            "id": f"xxx-{raw_item['id']}",
            "source": "xxx",
            "keyword": keyword,
            "title": raw_item["title"],
            "url": raw_item["url"],
            # ... 其他可选字段
        })

    if debug:
        log(f"  [xxx] '{keyword}' 命中 {len(result)} 条")
    return result
```

注册到分发表:

```python
PLATFORM_FUNCS = {
    "showstart": fetch_showstart,
    "juzimima": fetch_juzimima,
    "sina": fetch_sina_star,
    "googlenews": fetch_googlenews,
    # 加新源就加一行
}
```

### 3.3 去重 + 静默吸收(防刷屏的灵魂)

```python
def run_once(cfg, seen, first_run=False):
    # 抓取前快照"已知数据源"
    known_sources = {s.split(":", 1)[0] for s in seen if ":" in s}

    round_push = []      # 真要推送的: 已知 source 的新增
    round_silent = []    # 静默吸收: 新启用 source 首次抓到 (或 first_run)

    def _ingest(items):
        for it in items:
            key = f"{it['source']}:{it['id']}"
            if key in seen:
                continue  # 已见, 跳过
            seen.add(key)
            # 全局首轮 OR 这个 source 是新启用 -> 静默吸收
            if first_run or it["source"] not in known_sources:
                round_silent.append(it)
            else:
                round_push.append(it)

    # ... 调用各数据源, 喂给 _ingest()
    return round_push  # 只推送真增量
```

**为什么要这个机制**:
- 第一次启用某数据源, 历史报道几十条全在里面, 一次性推几十条会刷屏
- 把首次抓到的全部进基线, 不推送
- 第二次起, 只有真正"新出现的条目"才推送

### 3.4 推送层(并联多通道)

```python
def notify_new_items(items, debug=False):
    if not items:
        return
    # 并联调用, 互不干扰
    send_pushplus(items, debug=debug)   # 微信公众号
    send_wecom(items, debug=debug)      # 企业微信群机器人
    # 加新通道在这加: send_bark(...), send_telegram(...)

def send_wecom(items, debug=False):
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not webhook:
        return  # 没配 webhook 就跳过, 不算错

    md = _build_wecom_md(items)
    # 4096 字节限制, 必要时切分多包
    for chunk in _split_md(md, max_bytes=4000):
        requests.post(webhook, json={
            "msgtype": "markdown",
            "markdown": {"content": chunk}
        })
```

**关键设计**:
- 通道间**互不依赖**, 一个挂了不影响其他
- 没配 secret 就**静默跳过**, 不报错
- 大消息**自动切分**, 不让企业微信 4096 字节限制截断

### 3.5 CLI 模式

```python
parser.add_argument("--once", action="store_true",       # 跑一轮就退出
                    help="单轮运行, 不进入循环 (cron 触发用)")
parser.add_argument("--ci",   action="store_true",       # CI 报告输出
                    help="把新发现写入 latest.md, 供 Actions commit")
parser.add_argument("--test-notify", action="store_true",# 假数据测推送
                    help="只发一条测试推送, 不抓数据")
parser.add_argument("--debug", action="store_true")
parser.add_argument("--reset", action="store_true")      # 清空基线
```

**`--test-notify` 是神器**: 部署后第一件事就是用它验证推送链路, 比等真数据省时间。

---

## 4. 一步步部署(从零到生产 30 分钟)

### Step 1: 本地准备

```powershell
# 装 Python 3.10+ (官网下载安装包, 勾选 Add to PATH)
python --version

# 创建项目目录
mkdir my-monitor
cd my-monitor

# 装依赖 (基本只需要 requests)
echo "requests>=2.28.0" > requirements.txt
pip install -r requirements.txt
```

### Step 2: 写最小可用版

先做一个最简单的版本能本地跑, **不要一开始就上推送和 CI**, 先把数据源跑通:

```python
# minimal_monitor.py
import requests, json, hashlib
from pathlib import Path

SEEN_FILE = Path("seen.json")
seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()

def fetch_googlenews(keyword):
    url = f"https://news.google.com/rss/search?q={keyword}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    import xml.etree.ElementTree as ET
    r = requests.get(url, timeout=20)
    root = ET.fromstring(r.text)
    items = []
    for it in root.iter("item"):
        title = it.findtext("title").strip()
        link = it.findtext("link").strip()
        uid = hashlib.md5(link.encode()).hexdigest()[:12]
        items.append({"id": f"gnews-{uid}", "title": title, "url": link})
    return items

new_items = []
for item in fetch_googlenews("特朗普"):
    if item["id"] not in seen:
        seen.add(item["id"])
        new_items.append(item)

print(f"新增 {len(new_items)} 条")
for it in new_items[:5]:
    print(f"  - {it['title']}")

SEEN_FILE.write_text(json.dumps(list(seen), ensure_ascii=False))
```

跑两次 `python minimal_monitor.py`:
- 第一次: 应该看到一堆新增
- 第二次: 应该 0 新增 (去重生效)

**这一步通了, 你的核心引擎就稳了。**

### Step 3: 申请企业微信群机器人 (3 分钟)

1. 个人手机下载 **企业微信** App, 用任意手机号注册一个公司 (公司名随便填)
2. 创建一个内部群 (拉自己一个人也能建群, 拉小号更好)
3. 群里点右上角 ⋯ → **群机器人** → **添加** → 选"自定义机器人" → 起个名字
4. 复制 Webhook URL (形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx-xxx-xxx`)

测试一下 (PowerShell):

```powershell
$webhook = "你的 webhook URL"
$body = @{
    msgtype = "text"
    text = @{ content = "Hello from PowerShell" }
} | ConvertTo-Json
Invoke-RestMethod -Uri $webhook -Method Post -ContentType "application/json" -Body $body
```

手机上应该收到。

### Step 4: 加推送代码 + 测试模式

把 `send_wecom` 接到主脚本, 加一个 `--test-notify` 标志, 本地用环境变量先验:

```powershell
$env:WECOM_WEBHOOK = "你的 webhook"
python monitor.py --test-notify
```

收到测试卡片就过关。

### Step 5: 创建 GitHub 仓库

1. 浏览器打开 https://github.com/new
2. 名字 (例如 `my-monitor`), Private 即可
3. 不要勾"Initialize with README"
4. 创建后照页面提示在本地跑:

```powershell
git init
git branch -M main
git add .
git commit -m "init"
git remote add origin https://github.com/USERNAME/my-monitor.git
git push -u origin main
```

**国内网络推 GitHub 不稳? 开 VPN, 或用 GitHub Desktop**。

### Step 6: 配置 GitHub Secrets

仓库页面 → **Settings** → 左侧 **Secrets and variables → Actions** → **New repository secret**

加这两个 (没配的就跳过):

| Name | Value |
|------|-------|
| `WECOM_WEBHOOK` | 你的企业微信 webhook URL |
| `PUSHPLUS_TOKEN` | (可选) PushPlus 的 token |

### Step 7: 写 Actions 工作流

`.github/workflows/monitor.yml`:

```yaml
name: monitor
on:
  schedule:
    - cron: "*/30 * * * *"   # 每 30 分钟
  workflow_dispatch:          # 手动触发
    inputs:
      test_push:
        description: "勾上 = 只发测试推送, 不抓数据"
        type: boolean
        default: false

permissions:
  contents: write             # 让 commit-back 能写仓库

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: pip install -r requirements.txt

      - name: Run
        env:
          WECOM_WEBHOOK: ${{ secrets.WECOM_WEBHOOK }}
          PUSHPLUS_TOKEN: ${{ secrets.PUSHPLUS_TOKEN }}
        run: |
          if [ "${{ github.event.inputs.test_push }}" = "true" ]; then
            python monitor.py --test-notify
          else
            python monitor.py --once --ci
          fi

      - name: Commit state
        if: github.event.inputs.test_push != 'true'
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add seen.json latest.md monitor.log 2>/dev/null || true
          git diff --cached --quiet || git commit -m "ci: state update [skip ci]"
          git push
```

### Step 8: 验证

1. 仓库 → Actions 标签 → 选 `monitor` workflow → **Enable workflow** (默认禁用)
2. 点 **Run workflow** → 勾上 `test_push` → 点绿色按钮
3. 30 秒内你的企业微信群应该收到测试推送
4. 关闭 test_push, 再 Run 一次 (跑真数据), 看 Actions 日志

**部署完成。** 之后每 30 分钟自动跑, 你电脑可以关。

---

## 5. 推送通道扩展(填空式)

| 通道 | webhook / API | 字数限制 | 文档 |
|------|---------------|----------|------|
| **企业微信群机器人** | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` | 4096 字节 markdown | [官方](https://developer.work.weixin.qq.com/document/path/91770) |
| **钉钉群机器人** | `https://oapi.dingtalk.com/robot/send?access_token=...` | 20000 字符 | [官方](https://open.dingtalk.com/document/robots/custom-robot-access) |
| **飞书群机器人** | `https://open.feishu.cn/open-apis/bot/v2/hook/...` | 30000 字符 | [官方](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) |
| **Bark (iOS)** | `https://api.day.app/{key}/{title}/{body}` | URL 长度 | [官方](https://github.com/Finb/Bark) |
| **Telegram Bot** | `https://api.telegram.org/bot{TOKEN}/sendMessage` | 4096 字符 | [官方](https://core.telegram.org/bots/api) |
| **PushPlus** | `https://www.pushplus.plus/send` | 单次 | **粉丝实名要求, 不推荐** |
| **Server 酱** | `https://sctapi.ftqq.com/{KEY}.send` | 32KB | [官方](https://sct.ftqq.com/) |
| **邮件 (SMTP)** | `smtplib + aiosmtpd` | 无限制 | Python 标准库 |

每个加新通道的代码模板:

```python
def send_xxx(items, debug=False):
    if not items: return
    token = os.environ.get("XXX_TOKEN", "").strip()
    if not token: return  # 没配就跳过

    body = build_message(items)
    try:
        r = requests.post(API_URL, json={"content": body}, timeout=10)
        if r.status_code == 200:
            log(f"[XXX] 已推送 {len(items)} 条")
        else:
            log(f"[XXX] HTTP {r.status_code}: {r.text[:200]}", "ERROR")
    except Exception as e:
        log(f"[XXX] 推送异常: {e}", "ERROR")
```

然后在 `notify_new_items` 里加一行 `send_xxx(items, debug=debug)`。

---

## 6. 加新数据源(填空式)

只要按 `fetch_xxx` 模板写一个, 注册到 `PLATFORM_FUNCS`, 在 `config.json` 里把开关打开就完事。

### 选源原则(避免反爬陷阱)

按"做起来由易到难"排序:

1. **RSS / Atom feed** ⭐⭐⭐⭐⭐ 最稳, 标准协议, 永远 200
2. **JSON API** (无需鉴权) ⭐⭐⭐⭐
3. **静态 HTML** ⭐⭐⭐⭐ 用 requests + 正则/BeautifulSoup
4. **JSONP / 老式 ajax** ⭐⭐⭐
5. **需登录的 API** ⭐⭐ 麻烦但可做
6. **JS 动态渲染页** ⭐ 需要 playwright/selenium, GHA 上能跑但慢
7. **阿里系反爬页** (showstart/大麦/淘宝) ✗ **直接放弃, 找替代**

### 经典数据源清单

| 想监控 | 推荐源 |
|-------|------|
| 名人新闻 | Google News RSS / 新浪聚合页 |
| GitHub 项目动态 | `https://github.com/USER/REPO/releases.atom` |
| 论坛话题 | RSSHub (有 Reddit / V2EX / 贴吧) |
| 政府公告 | 大多有 RSS, 没有就静态页 |
| 商品价格 | 京东/天猫商详页(静态部分能抓) |
| 股票行情 | sina/eastmoney 的股票数据接口 (JSON) |
| 微博 / B站 | RSSHub (`/weibo/user/UID`, `/bilibili/user/dynamic/UID`) |
| Twitter / X | nitter 实例 (RSS) |
| 演出票务 | 桔子密码 (静态), 秀动 / 大麦慎用 |

---

## 7. 故障排查表

### 问题: 没收到推送

| 检查点 | 怎么查 | 怎么解 |
|-------|-------|--------|
| Actions 跑了吗 | 仓库 Actions 标签看 run 列表 | Workflow 被禁用就启用 |
| 跑成功了吗 | 看 run 是绿勾还是红 X | 红 X 看错误日志 |
| 真有新增吗 | 日志找 `本轮新增 X 条` 或 `本轮无新增` | 没新增 = 正常沉默, 等 |
| 静默吸收了? | 日志找 `首次抓到 X 条, 已静默加入基线` | 等下一轮才会推 |
| 推送被调用了? | 日志找 `[WeCom] 已推送 X 条` | 没看到说明没新增 |
| webhook 失效? | 日志找 `[WeCom] HTTP 4xx/5xx` | 重新生成 webhook |
| Secret 没配? | Settings → Secrets 检查 | 加上重跑 |

### 问题: 推送报错 errcode

**企业微信常见错误码**:

| code | 原因 | 处理 |
|------|------|------|
| 0 | 成功 | 放心 |
| 93000 | webhook 错或被禁 | 重新拿 webhook URL |
| 45009 | 接口调用频率太高 | 加退避或减频率 |
| 40003 | userid 不存在 (个人号没事) | 确认 webhook key |

**PushPlus 错误码 905 = 接收方未实名**: 放弃, 改企业微信。

### 问题: GitHub 推不上去

```
fatal: unable to access 'https://github.com/...': Failed to connect
```

90% 是国内网络 / VPN 问题:

1. 开 VPN, 全局或 PAC 代理
2. 验证: `Test-NetConnection github.com -Port 443`
3. 实在不行用 GitHub Desktop 客户端

### 问题: Actions 提示 "Node.js 16 deprecation"

升级到最新版:

```yaml
- uses: actions/checkout@v4    # 不是 v3
- uses: actions/setup-python@v5 # 不是 v4
```

---

## 8. 维护与扩展(日常操作手册)

### 加 / 删关键词

GitHub 网页编辑 `config.json`:

```
https://github.com/USER/REPO/edit/main/config.json
```

修改这两个字段然后 Commit:

```json
{
  "keywords": ["王一博", "新加的人"],
  "googlenews_keywords": ["特朗普", "马斯克", "比特币"]
}
```

### 改推送频率

`.github/workflows/monitor.yml` 里 `cron`:

| cron | 含义 |
|------|------|
| `*/15 * * * *` | 每 15 分钟 |
| `*/30 * * * *` | 每 30 分钟 (默认) |
| `0 * * * *` | 每整点 |
| `0 9,21 * * *` | 每天 9:00 和 21:00 |
| `0 9 * * 1-5` | 工作日 9:00 |

**注意**: GitHub Actions cron **不保证准时**, 有 5-15 分钟漂移, 高峰期甚至会延误半小时。**不要用于秒级敏感场景**。

### 暂停监控

仓库 → Actions → 点 monitor workflow → 右上 ⋯ → **Disable workflow**

### 重置基线(把所有内容重新当成"新")

本地或 Codespace:

```powershell
echo "[]" > seen.json
git add seen.json
git commit -m "reset baseline"
git push
```

下次跑会把当前所有命中再次入库, 但因为还是 first_run / 未知 source, 不会推送(静默吸收机制保护)。

### 看历史发现

`latest.md` 文件里有最新一轮的所有报告 + 历史滚动追加。直接在 GitHub 仓库主页就能看。

---

## 9. 关键经验总结(做下一个项目前必读)

### 工程层面

1. **先做最小可用版,再加花哨功能**。本项目从 50 行 minimal_monitor 开始,逐步加去重、CI、多源、多推送。每一步都能跑就过关。

2. **分层抽象, 一致的数据结构**。所有 `fetch_xxx` 返回同一种 dict,后面所有处理逻辑都不用关心数据来源。**这是扩展性的关键**。

3. **静默吸收防刷屏**。任何"首次启用"都默认静默基线一轮。这个机制做一次,后面加任何数据源都自动安全。

4. **推送层独立, 多通道并联**。一个通道挂了不影响其他;新通道是简单的"加一行"。

5. **CLI 模式齐全**。`--once`(跑一轮)、`--ci`(报告模式)、`--test-notify`(测推送)、`--debug`(详细日志)、`--reset`(清基线)。**部署期间至少要用三次 `--test-notify`,绝对值得**。

### 选型层面

1. **能选 RSS 不选 JSON, 能选 JSON 不选 HTML, 能选静态 HTML 不选 JS 动态**。开发心智成本由低到高排序。

2. **数据源要先验证再开发**。本地 `curl` 一下看看是不是 200, 看看返回是 HTML 还是 JS-bundle。返回里有没有你要的字段。**5 分钟 curl 节省 5 小时调试**。

3. **永远预留 fallback**。比如新浪聚合页挂了, 还有 Google News 兜底。**别把所有蛋放一个篮子**。

4. **认清"实名认证"的政治风险**。中国互联网很多服务暗藏实名要求(PushPlus、淘宝 API、抖音开放平台...),先做最小验证再投入开发。

### 运维层面

1. **GitHub Actions 是被低估的免费基础设施**。每月 2000 分钟免费额度, cron 定时器 + Secret + 自带日志, 比自家 VPS 省 100 倍精力。

2. **状态文件 commit 回仓库 = 零成本持久化**。比起 Redis / 云数据库便宜得多, 还能从 git 历史里看任意时刻的"基线"。

3. **写好 README, 半年后的自己会感谢你**。

4. **把"如何复刻"也写下来(就像本文)**。下次做同类项目时, 改改细节就能上线。

---

## 10. 进阶方向(下一步可以做的)

| 想法 | 实现思路 | 难度 |
|------|--------|------|
| **AI 摘要** | 调 GPT/Claude/Qwen API 把每条推送的标题 + 链接打包成 3 句话摘要再推送 | 中 |
| **重要度评分** | 按媒体权重 / 关键词组合 给每条打分, 低分不推送 | 中 |
| **聚类去重** | 同一事件不同媒体重复报道, 用 embedding + 余弦相似度合并 | 中高 |
| **关键词联想拓展** | "马斯克" 自动加 "Elon Musk" / "SpaceX" / "Tesla" | 低 |
| **图片附带** | 抓页面 og:image, 推送时附图 (企业微信 markdown 支持图片) | 低 |
| **存历史 SQLite** | 不仅 latest.md, 把所有发现入库, 后续可以做趋势分析 | 中 |
| **Web 控制台** | 用 Cloudflare Pages 搭个静态站, 看历史 + 改 keyword | 中高 |
| **多用户独立监控** | 每个用户一个 fork 仓库 + 自己的 webhook | 高 |
| **更密集触发** | 用 Cloudflare Workers + cron trigger 实现 5 分钟级 | 高 |

---

## 11. 本项目最终交付物清单

- ✓ Python 主脚本 1500 行,3 数据源 + 2 推送通道 + CLI 完整
- ✓ `config.json` 一改即生效
- ✓ GitHub Actions 每 30 分钟自动运行
- ✓ 增量去重 + 静默吸收防刷屏
- ✓ 企业微信卡片格式美化
- ✓ 一键测试推送 (`workflow_dispatch` 勾选 `test_push`)
- ✓ 所有状态自动持久化到仓库

**月成本: ¥0  
人工干预: 添加新关键词时改一次 config.json  
持续运行: 24x7 不间断**

---

## 附: 速查表

```bash
# ── 本地命令 ──
python monitor.py                    # 守护模式 (本地长跑)
python monitor.py --once             # 跑一轮就退出
python monitor.py --once --ci        # 跑一轮 + 写 latest.md
python monitor.py --test-notify      # 测推送, 不抓数据
python monitor.py --reset            # 清基线
python monitor.py --debug            # 详细日志

# ── 环境变量 (本地测推送) ──
$env:WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
$env:PUSHPLUS_TOKEN = "your_token"

# ── git 三剑客 ──
git status
git add -A
git commit -m "msg"
git pull --rebase    # 云端有 ci commit 时必须先 rebase 再 push
git push

# ── GitHub URL 速查 ──
仓库:     https://github.com/USER/REPO
Actions: https://github.com/USER/REPO/actions
Secrets: https://github.com/USER/REPO/settings/secrets/actions
config:  https://github.com/USER/REPO/edit/main/config.json
```

---

**完。**

> 这份文档是从一次真实的从零到生产的实战中提炼的, 覆盖了所有踩过的坑和关键决策。
> 下次再做类似项目, 把这份文档先看一遍, 能少走 80% 的弯路。
