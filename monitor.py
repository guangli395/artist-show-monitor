# -*- coding: utf-8 -*-
"""
艺人演出上新监控 - 本地验证版

功能：
    定时查询多个票务平台的艺人演出,发现新场次时立即在控制台打印。
    不依赖钉钉或任何推送服务,纯验证"能否实时获取新演出消息"。

用法：
    python monitor.py                # 正常运行(读 config.json)
    python monitor.py --once         # 只跑一轮就退出(调试用)
    python monitor.py --debug        # 打印原始响应,排查接口问题
    python monitor.py --reset        # 清空已见记录,从头开始
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
# curl_cffi 提供 TLS/JA3 指纹伪装, 用于绕过阿里系 x5secdata 等风控
try:
    from curl_cffi.requests import Session as CffiSession
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
LOG_FILE = ROOT / "monitor.log"


# ============================================================
# 日志
# ============================================================
def log(msg, tag="INFO"):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{tag}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def banner(text):
    bar = "=" * 60
    log(bar)
    log(text)
    log(bar)


# ============================================================
# 配置
# ============================================================
def load_config():
    if not CONFIG_FILE.exists():
        log(f"配置文件不存在: {CONFIG_FILE}", "ERROR")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("keywords", [])
    cfg.setdefault("platforms", {"showstart": True, "damai": True})
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("debug", False)
    cfg.setdefault("first_run_preview", 3)
    return cfg


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(
        json.dumps(sorted(list(seen)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# 数据源一:秀动 showstart
# ============================================================
def fetch_showstart(keyword, debug=False):
    """
    秀动 H5 搜索接口。接口结构如果变化,可在 debug 模式下看原始响应。
    """
    candidates = [
        # 候选接口(按可用性顺序尝试)
        {
            "url": "https://sapi.showstart.com/h5/activity/list",
            "method": "POST",
            "json": {
                "pageNo": 1, "pageSize": 50, "keyword": keyword,
                "cityCode": "", "typeId": 0,
            },
        },
        {
            "url": "https://sapi.showstart.com/api/activity/list",
            "method": "POST",
            "json": {"pageNo": 1, "pageSize": 50, "keyword": keyword},
        },
        {
            "url": "https://wap.showstart.com/api/pc/activity/list",
            "method": "GET",
            "params": {"pageNo": 1, "pageSize": 50, "keyword": keyword},
        },
    ]
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wap.showstart.com/",
        "Origin": "https://wap.showstart.com",
    }

    for c in candidates:
        try:
            if c["method"] == "POST":
                r = requests.post(c["url"], json=c.get("json"),
                                  headers=headers, timeout=10)
            else:
                r = requests.get(c["url"], params=c.get("params"),
                                 headers=headers, timeout=10)

            if debug:
                log(f"  [秀动 DEBUG] {c['url']} -> {r.status_code} "
                    f"resp[:200]={r.text[:200]}")

            if r.status_code != 200:
                continue

            data = r.json()
            # 尝试多种结果字段命名
            items = (
                data.get("data", {}).get("result")
                or data.get("data", {}).get("list")
                or data.get("result")
                or data.get("list")
                or []
            )
            if items:
                return _normalize_showstart(items, keyword)
        except Exception as e:
            if debug:
                log(f"  [秀动 DEBUG] {c['url']} 异常: {e}")
            continue
    return []


def _normalize_showstart(items, keyword):
    result = []
    for it in items:
        pid = str(it.get("id") or it.get("activityId") or "")
        if not pid:
            continue
        result.append({
            "source": "showstart",
            "id": pid,
            "keyword": keyword,
            "title": it.get("title") or it.get("name") or "未命名",
            "city": it.get("cityName") or it.get("city") or "",
            "venue": it.get("siteName") or it.get("venueName") or "",
            "date": it.get("startTime") or it.get("showTime") or "",
            "price": it.get("minPrice") or it.get("priceLow") or 0,
            "url": f"https://wap.showstart.com/pages/activity/detail/detail?activityId={pid}",
        })
    return result


# ============================================================
# 数据源二:大麦 H5 搜索
# ============================================================
# 全局 curl_cffi 会话, 保持 cookie (_m_h5_tk) 跨请求复用
_DAMAI_SESSION = None
_DAMAI_LAST_WARMUP = 0
_DAMAI_WARMUP_INTERVAL = 1800  # 30 分钟重新热身一次


def _get_damai_session(debug=False):
    """
    建立/复用一个伪装 Chrome 的 curl_cffi Session。
    访问一次大麦主页拿到 _m_h5_tk 等 cookie, 后续搜索才不会被风控。
    """
    global _DAMAI_SESSION, _DAMAI_LAST_WARMUP
    if not HAS_CFFI:
        return None

    now = time.time()
    if _DAMAI_SESSION is None or (now - _DAMAI_LAST_WARMUP) > _DAMAI_WARMUP_INTERVAL:
        if debug:
            log("  [大麦] 建立新 curl_cffi 会话并热身...")
        _DAMAI_SESSION = CffiSession(impersonate="chrome124")
        try:
            # 先访问主页, 让服务器下发 _m_h5_tk 等 cookie
            _DAMAI_SESSION.get("https://www.damai.cn/", timeout=15)
            _DAMAI_SESSION.get("https://search.damai.cn/", timeout=15)
            _DAMAI_LAST_WARMUP = now
            if debug:
                tk = _DAMAI_SESSION.cookies.get("_m_h5_tk", "(无)")
                log(f"  [大麦] 热身完成, _m_h5_tk={tk[:40]}...")
        except Exception as e:
            log(f"  [大麦] 热身失败: {e}", "WARN")
    return _DAMAI_SESSION


def fetch_damai(keyword, debug=False):
    """
    用 curl_cffi (Chrome 指纹) + session 的方式访问大麦搜索接口。
    关键点: 必须先访问主页让服务器下发 cookie, 否则 100% 命中 x5secdata 风控。
    """
    if not HAS_CFFI:
        log("  [大麦] 未安装 curl_cffi, 跳过 (pip install curl_cffi)", "WARN")
        return []

    session = _get_damai_session(debug=debug)
    if session is None:
        return []

    url = "https://search.damai.cn/searchajax.html"
    params = {
        "keyword": keyword,
        "cty": "",
        "ctl": "",
        "sctl": "",
        "tsg": "0",
        "st": "",
        "et": "",
        "order": "1",
        "pageSize": "30",
        "currPage": "1",
        "tn": "",
    }
    headers = {
        "Referer": f"https://search.damai.cn/search.htm?keyword={quote(keyword)}",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        r = session.get(url, params=params, headers=headers, timeout=12)
        snippet = r.text[:250].replace("\n", " ")
        if debug:
            log(f"  [大麦 DEBUG] status={r.status_code} body[:250]={snippet}")

        # 命中风控页面的典型特征
        if "punish" in r.text or "x5secdata" in r.text or "_____tmd_____" in r.text:
            log("  [大麦] 命中风控 (x5secdata/punish), 已重置会话, 下轮重试", "WARN")
            # 下轮强制重新热身
            global _DAMAI_LAST_WARMUP
            _DAMAI_LAST_WARMUP = 0
            return []

        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("result") or data.get("data", {}).get("result") or []
        result = []
        for it in items:
            pid = str(it.get("id") or "")
            if not pid:
                continue
            result.append({
                "source": "damai",
                "id": pid,
                "keyword": keyword,
                "title": (it.get("nameNoHtml") or it.get("name") or "").strip(),
                "city": it.get("cityName") or "",
                "venue": it.get("venueName") or "",
                "date": it.get("showTime") or "",
                "price": it.get("priceLow") or 0,
                "url": it.get("itemUrl") or f"https://detail.damai.cn/item.htm?id={pid}",
            })
        return result
    except Exception as e:
        if debug:
            log(f"  [大麦 DEBUG] 异常: {e}")
        return []


# ============================================================
# 数据源二·五:桔子密码(juzimima.com) - 结构化票务聚合
# ----------------------------------------------------------
# 这是一个静态渲染的票务聚合站,无反爬。
# 列表页: /event/7-0-5-0-1-1.html (演唱会最新, 最后的 1 是页码)
# 详情页: /event/{id}/ (含时间/场馆/票价/在售状态)
# 列表正则匹配格式:
#   [2026 周杰伦 上海 演唱会]
#   (URL: https://m.juzimima.com/event/XXXXX/)
# ============================================================
import re as _re_jzm

_JZM_BASE = "https://m.juzimima.com"


def fetch_juzimima(keyword, debug=False):
    """
    抓 juzimima 的演唱会最新列表,按关键词过滤命中的演出。
    用标准 requests 就够, 无需特殊反爬处理。
    """
    all_items = []
    # 扫前 10 页 "最新" 列表 (单页 9 条, 共 ~90 条, 覆盖大部分近期演出)
    for page in range(1, 11):
        url = f"{_JZM_BASE}/event/7-0-5-0-1-{page}.html"
        try:
            r = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
                                      " AppleWebKit/605.1.15"},
                timeout=15,
            )
            if r.status_code != 200:
                if debug:
                    log(f"  [juzimima DEBUG] page{page} status={r.status_code}")
                break
            r.encoding = "utf-8"
            html = r.text
        except Exception as e:
            if debug:
                log(f"  [juzimima DEBUG] page{page} 异常: {e}")
            break

        # 真实 HTML 结构 (从实际响应抓出来):
        #   <a href="/event/40838/">
        #     <img alt="2026陈慧娴成都演唱会" ...>
        #     <div class="Rig">
        #       <p class="tit">2026陈慧娴成都演唱会</p>
        #       <p class="cont">2026-06-13 19:00</p>
        #       <p class="cont">五粮液文化体育中心综合体育馆</p>
        #       <p class="price"><span>￥480</span>起</p>
        #     </div>
        #   </a>
        page_items = []
        # 先抓出所有 <a href="/event/NNN/"> ... </a> 整块
        a_re = _re_jzm.compile(
            r'<a\s+href="/event/(?P<id>\d+)/?"\s*>(?P<inner>.*?)</a>',
            _re_jzm.S,
        )
        for m in a_re.finditer(html):
            eid = m.group("id")
            inner = m.group("inner")

            tit_m = _re_jzm.search(r'<p\s+class="tit">([^<]+)</p>', inner)
            cont_m = _re_jzm.findall(r'<p\s+class="cont">([^<]+)</p>', inner)
            price_m = _re_jzm.search(r'<span>￥(\d+)</span>', inner)

            if not tit_m:
                continue
            title = tit_m.group(1).strip()
            date_str = cont_m[0].strip() if len(cont_m) >= 1 else ""
            venue = cont_m[1].strip() if len(cont_m) >= 2 else ""
            price = int(price_m.group(1)) if price_m else 0

            page_items.append({
                "id": f"juzimima-{eid}",
                "source": "juzimima",
                "title": title,
                "date": date_str,
                "venue": venue,
                "price": price,
                "url": f"{_JZM_BASE}/event/{eid}/",
            })
        if debug:
            log(f"  [juzimima DEBUG] page{page} 解析出 {len(page_items)} 条")
        if not page_items:
            break  # 没有数据了, 停止翻页
        all_items.extend(page_items)

    # 按 keyword 过滤 (在标题里查找)
    filtered = []
    for it in all_items:
        if keyword in it["title"]:
            it["keyword"] = keyword
            # 从标题粗略提取城市
            title_clean = it["title"].replace("2026", "").replace("2025", "").strip()
            idx = title_clean.find("演唱会")
            if idx > 0:
                artist_and_city = title_clean[:idx].strip()
                if artist_and_city.startswith(keyword):
                    it["city"] = artist_and_city[len(keyword):].strip()
                else:
                    it["city"] = ""
            else:
                it["city"] = ""

            # 对匹配项, 进详情页抓"演出状态/详细票价/地址"等关键字段
            detail = _fetch_juzimima_detail(it["id"].replace("juzimima-", ""), debug=debug)
            if detail:
                it.update(detail)
            filtered.append(it)

    log(f"  [juzimima] 关键词 '{keyword}': 全站扫 {len(all_items)} 条 -> 匹配 {len(filtered)} 条")
    return filtered


def _fetch_juzimima_detail(event_id, debug=False):
    """访问详情页, 提取 状态/票面价格区间/详细地址 等"""
    url = f"{_JZM_BASE}/event/{event_id}/"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
                                  " AppleWebKit/605.1.15"},
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        # 强制 UTF-8 解码 (juzimima 头声明是 utf-8 但 requests 可能误判为 ISO-8859-1)
        r.encoding = "utf-8"
        html = r.text
    except Exception as e:
        if debug:
            log(f"  [juzimima 详情 DEBUG] event={event_id} 异常: {e}")
        return {}

    detail = {}
    # 演出全名 (h1 标签)
    full_name_m = _re_jzm.search(r'<h1[^>]*>([^<]+)</h1>', html)
    if full_name_m:
        detail["full_name"] = full_name_m.group(1).strip()

    # 演出状态: 通常在文本里出现 "演出状态：xxx"
    status_m = _re_jzm.search(r'演出状态[:：]\s*([^\s<]+)', html)
    if status_m:
        detail["status"] = status_m.group(1).strip()

    # 票面价格: "票面价格：￥580~2380" 或 "￥XXX起"
    pricelist_m = _re_jzm.search(r'票面价格[:：]\s*￥([\d,~\-\.]+)', html)
    if pricelist_m:
        detail["price_range"] = pricelist_m.group(1).strip()

    # 完整地址: 通常在 brand 链接附近, 格式 "省市XX区XX路XXX号"
    addr_m = _re_jzm.search(r'>(\S+省\S+市\S+?号)<', html)
    if addr_m:
        detail["address"] = addr_m.group(1).strip()
    else:
        # 兜底: 找任何包含"省/市"且看起来像地址的行
        for cand in _re_jzm.findall(r'>([^<>\n]{6,80})<', html):
            if ("省" in cand and "市" in cand) or "区" in cand and "路" in cand:
                if "号" in cand or "街道" in cand:
                    detail["address"] = cand.strip()
                    break
    return detail


# ============================================================
# 数据源二·六:新浪娱乐艺人聚合页 (神级数据源)
# ----------------------------------------------------------
# URL: https://tags.sina.com.cn/star_{拼音}
# 特点:
#   - 纯静态 HTML, 无反爬
#   - 实时聚合艺人所有相关新闻 + 微博博文 (几百条)
#   - 天然按艺人聚合, 不用在海量热搜里过滤
#   - 明星官方发布/媒体报道/粉丝讨论全覆盖
#
# 用法: 在 config.json 里添加 sina_artists 映射 { "王一博": "wangyibo" }
# ============================================================

# 艺人中文名 -> 新浪聚合页拼音
# 可在 config.json 里的 sina_artists 字段覆盖/扩展
_SINA_ARTIST_MAP_DEFAULT = {
    "王一博": "wangyibo",
    "蔡徐坤": "caixukun",
    "周杰伦": "zhoujielun",
    "肖战": "xiaozhan",
    "易烊千玺": "yiyangqianxi",
    "张艺兴": "zhangyixing",
    "鹿晗": "luhan",
    "邓紫棋": "dengziqi",
    "薛之谦": "xuezhiqian",
    "林俊杰": "linjunjie",
    "五月天": "wuyuetian",
    "李荣浩": "lironghao",
    "告五人": "gaowuren",
    "万能青年旅店": "wannengqingnianlvdian",
}

# 只保留含这些关键词的新闻 (演出相关)
_SINA_SHOW_HINTS = [
    "演唱会", "巡演", "开票", "加场", "返场", "开抢",
    "音乐节", "livehouse", "Live House", "live house",
    "巡回", "主题曲", "MV", "新专辑", "新歌", "发布",
    "签售", "见面会", "歌友会",
]


def fetch_sina_star(keyword, debug=False, extra_artist_map=None):
    """
    抓新浪娱乐艺人聚合页, 过滤出含演出关键词的新闻。
    参数:
      keyword: 艺人中文名, 会查找 artist_map 得到拼音
      extra_artist_map: 外部传入的 "艺人→拼音" 映射, 覆盖默认
    """
    artist_map = dict(_SINA_ARTIST_MAP_DEFAULT)
    if extra_artist_map:
        artist_map.update(extra_artist_map)

    pinyin = artist_map.get(keyword)
    if not pinyin:
        log(f"  [新浪] 艺人 '{keyword}' 未配置拼音映射, 请在 config.json 的 sina_artists 里添加",
            "WARN")
        return []

    url = f"https://tags.sina.com.cn/star_{pinyin}"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                             " AppleWebKit/537.36 (KHTML, like Gecko)"
                             " Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=15,
        )
        if r.status_code != 200:
            if debug:
                log(f"  [新浪 DEBUG] status={r.status_code}")
            return []
        r.encoding = "utf-8"
        html = r.text
    except Exception as e:
        if debug:
            log(f"  [新浪 DEBUG] 异常: {e}")
        return []

    # 抓所有 <a href="...k.sina.com.cn/article_...">{title}</a>
    # 文章 URL 模式: https://k.sina.com.cn/article_{id}.html (或 /article_{hash}.html)
    # 以及 https://www.sina.cn/news/detail/{id}.html
    import re as _re_sina
    a_patterns = [
        _re_sina.compile(r'<a[^>]+href="(https?://k\.sina\.com\.cn/article_[^"]+\.html[^"]*)"[^>]*>([^<]+)</a>'),
        _re_sina.compile(r'<a[^>]+href="(https?://(?:www|news|finance|video)\.sina\.(?:com\.cn|cn)/[^"]*)"[^>]*>([^<]+)</a>'),
    ]

    seen_urls = set()
    all_items = []
    for pat in a_patterns:
        for m in pat.finditer(html):
            link = m.group(1)
            title = m.group(2).strip()
            if not title or len(title) < 4:
                continue
            if link in seen_urls:
                continue
            seen_urls.add(link)
            all_items.append({"url": link, "title": title})

    # 过滤含演出关键词的
    filtered = []
    for it in all_items:
        if any(h in it["title"] for h in _SINA_SHOW_HINTS):
            # 去重: 用 URL 作为 id
            import hashlib
            uid = hashlib.md5(it["url"].encode("utf-8")).hexdigest()[:12]
            filtered.append({
                "id": f"sina-{uid}",
                "source": "sina",
                "keyword": keyword,
                "title": it["title"],
                "url": it["url"],
                # 这些字段新浪聚合页没有, 留空
                "date": "",
                "venue": "",
                "city": "",
                "price": 0,
            })

    log(f"  [新浪] 艺人 '{keyword}' (star_{pinyin}): 页面 {len(all_items)} 条"
        f" -> 含演出关键词 {len(filtered)} 条")
    return filtered


# ============================================================
# 数据源三:豆瓣同城活动 (避开阿里系,最合规路径)
# ============================================================
_DOUBAN_SESSION = None
_DOUBAN_WARMED = False


def _get_douban_session():
    global _DOUBAN_SESSION, _DOUBAN_WARMED
    if _DOUBAN_SESSION is None:
        if HAS_CFFI:
            _DOUBAN_SESSION = CffiSession(impersonate="chrome124")
        else:
            _DOUBAN_SESSION = requests.Session()
    if not _DOUBAN_WARMED:
        try:
            # 预热: 访问移动端首页让豆瓣下发 bid cookie
            _DOUBAN_SESSION.get(
                "https://m.douban.com/",
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
                                      " AppleWebKit/605.1.15"},
                timeout=10,
            )
            _DOUBAN_WARMED = True
        except Exception:
            pass
    return _DOUBAN_SESSION


def fetch_douban(keyword, debug=False):
    """
    豆瓣同城搜索活动。使用移动端 rexxar API,返回 JSON。
    覆盖: 音乐会/演唱会/音乐节/话剧/展览 等全类型同城活动。
    """
    session = _get_douban_session()
    url = "https://m.douban.com/rexxar/api/v2/search"
    params = {
        "q": keyword,
        "type": "event",
        "start": "0",
        "count": "20",
    }
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
        "Referer": "https://m.douban.com/search/?query=" + quote(keyword) + "&type=event",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        r = session.get(url, params=params, headers=headers, timeout=12)
        snippet = r.text[:250].replace("\n", " ")
        if debug:
            log(f"  [豆瓣 DEBUG] status={r.status_code} body[:250]={snippet}")
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("items") or []
        result = []
        for wrapper in items:
            # rexxar 有时返回 {"target_type":..., "target":{...}} 的包装
            target = wrapper.get("target") or wrapper
            pid = str(target.get("id") or "")
            if not pid:
                continue
            title = target.get("title") or target.get("name") or ""
            if not title:
                continue
            # 尽量拿完整的时间/地点
            city = ""
            venue = ""
            addr = target.get("address") or ""
            if isinstance(addr, str):
                venue = addr
            start_time = target.get("start_time") or target.get("begin_time") or ""
            host = target.get("host") or {}
            host_name = host.get("name") if isinstance(host, dict) else ""
            result.append({
                "source": "douban",
                "id": f"douban-{pid}",
                "keyword": keyword,
                "title": title.strip(),
                "city": city,
                "venue": venue or host_name or "",
                "date": start_time,
                "price": 0,
                "url": target.get("url") or f"https://www.douban.com/event/{pid}/",
            })
        return result
    except Exception as e:
        if debug:
            log(f"  [豆瓣 DEBUG] 异常: {e}")
        return []


# ============================================================
# 数据源四:微博关键词搜索 (过滤演出相关博文)
# ============================================================
_WEIBO_SESSION = None
_WEIBO_WARMED = False
# 博文里出现这些词才视为"演出上新"类信息,否则会有大量噪音
_WEIBO_SHOW_HINTS = [
    "巡演", "演唱会", "音乐节", "LIVE", "live", "Live",
    "开票", "加场", "巡回", "演出", "售票", "开售", "票务",
]


def _get_weibo_session():
    global _WEIBO_SESSION, _WEIBO_WARMED
    if _WEIBO_SESSION is None:
        if HAS_CFFI:
            _WEIBO_SESSION = CffiSession(impersonate="chrome124")
        else:
            _WEIBO_SESSION = requests.Session()
    if not _WEIBO_WARMED:
        try:
            _WEIBO_SESSION.get(
                "https://m.weibo.cn/",
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
                                      " AppleWebKit/605.1.15"},
                timeout=10,
            )
            _WEIBO_WARMED = True
        except Exception:
            pass
    return _WEIBO_SESSION


def fetch_weibo(keyword, debug=False):
    """
    用 m.weibo.cn 无登录接口搜索关键词相关博文,
    只保留含"巡演/演唱会/开票"等上新类词汇的博文。
    """
    session = _get_weibo_session()
    url = "https://m.weibo.cn/api/container/getIndex"
    params = {
        "containerid": f"100103type=1&q={keyword}",
        "page_type": "searchall",
    }
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
        "Referer": f"https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D{quote(keyword)}",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        r = session.get(url, params=params, headers=headers, timeout=12)
        snippet = r.text[:250].replace("\n", " ")
        if debug:
            log(f"  [微博 DEBUG] status={r.status_code} body[:250]={snippet}")
        if r.status_code != 200:
            return []
        data = r.json()
        cards = data.get("data", {}).get("cards", []) or []
        result = []
        for card in cards:
            # 微博搜索结果分层比较深,card_group 里才是一条条博文
            groups = card.get("card_group") or [card]
            for g in groups:
                mblog = g.get("mblog")
                if not mblog:
                    continue
                text = (mblog.get("text") or "") + " " + (mblog.get("raw_text") or "")
                # 去掉 html 标签简单处理
                text_plain = text.replace("<br />", " ")
                if not any(h in text_plain for h in _WEIBO_SHOW_HINTS):
                    continue
                pid = str(mblog.get("id") or mblog.get("mid") or "")
                if not pid:
                    continue
                user = mblog.get("user") or {}
                # 截取前 80 字符作为标题
                title_text = text_plain
                # 粗略去掉 HTML 标签
                import re as _re
                title_text = _re.sub(r"<[^>]+>", "", title_text).strip()
                if len(title_text) > 80:
                    title_text = title_text[:80] + "..."
                result.append({
                    "source": "weibo",
                    "id": f"weibo-{pid}",
                    "keyword": keyword,
                    "title": title_text,
                    "city": "",
                    "venue": user.get("screen_name") or "",
                    "date": mblog.get("created_at") or "",
                    "price": 0,
                    "url": f"https://m.weibo.cn/status/{pid}",
                })
        return result
    except Exception as e:
        if debug:
            log(f"  [微博 DEBUG] 异常: {e}")
        return []


# ============================================================
# 数据源五:RSSHub 代理层 (无需登录,最合规)
# ============================================================
# RSSHub 已经处理了微博/豆瓣的反爬和登录态,我们只需拉 JSON。
# 默认用公共实例,生产环境建议自建(Cloudflare Workers 免费部署)。
_RSSHUB_BASE = "https://rsshub.app"
_RSSHUB_SHOW_HINTS = _WEIBO_SHOW_HINTS  # 复用微博的关键词过滤表


def _rsshub_get_json(path, debug=False):
    """
    统一请求 RSSHub 的 JSON Feed,在失败时尝试几个公共镜像。
    """
    mirrors = [
        _RSSHUB_BASE,
        "https://rsshub.rssforever.com",
        "https://rss.fatpandac.com",
    ]
    headers = {"User-Agent": "Mozilla/5.0 artist-show-monitor/1.0"}
    last_err = None
    for base in mirrors:
        url = f"{base}{path}"
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if debug:
                log(f"  [RSSHub DEBUG] {url} -> {r.status_code} "
                    f"body[:150]={r.text[:150].replace(chr(10), ' ')}")
            if r.status_code == 200 and r.text.strip().startswith("{"):
                return r.json(), base
        except Exception as e:
            last_err = e
            if debug:
                log(f"  [RSSHub DEBUG] {url} 异常: {e}")
            continue
    if debug and last_err:
        log(f"  [RSSHub DEBUG] 所有镜像均失败, 最后错误: {last_err}")
    return None, None


def fetch_rsshub_weibo(keyword, debug=False):
    """
    通过 RSSHub 的微博关键词路由搜索,过滤出含"巡演/演唱会"等演出词的博文。
    路径: /weibo/keyword/{keyword}.json
    """
    # RSSHub 的路径里 keyword 要 URL 编码
    path = f"/weibo/keyword/{quote(keyword)}.json"
    data, used = _rsshub_get_json(path, debug=debug)
    if not data:
        return []
    items = data.get("items") or []
    result = []
    for it in items:
        content = (it.get("content_html") or "") + " " + (it.get("title") or "")
        # 粗略去 HTML
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", content)
        if not any(h in plain for h in _RSSHUB_SHOW_HINTS):
            continue
        pid = it.get("id") or it.get("url") or ""
        if not pid:
            continue
        title_text = (it.get("title") or plain[:80]).strip()
        if len(title_text) > 100:
            title_text = title_text[:100] + "..."
        result.append({
            "source": "weibo(rsshub)",
            "id": f"rsshub-weibo-{pid}",
            "keyword": keyword,
            "title": title_text,
            "city": "",
            "venue": it.get("authors", [{}])[0].get("name", "") if it.get("authors") else "",
            "date": it.get("date_published") or "",
            "price": 0,
            "url": it.get("url") or "",
        })
    if debug:
        log(f"  [RSSHub/微博] '{keyword}' 原始 {len(items)} 条 -> 过滤后 {len(result)} 条演出相关")
    return result


def fetch_rsshub_douban(keyword, debug=False):
    """
    豆瓣没有直接的关键词活动搜索,改为监控"全国同城活动"列表,
    在结果里做关键词匹配。
    路径: /douban/other/event/all.json (按城市可改 /douban/other/event/108288 等)
    """
    path = "/douban/other/event/all.json"
    data, _ = _rsshub_get_json(path, debug=debug)
    if not data:
        return []
    items = data.get("items") or []
    result = []
    for it in items:
        title = it.get("title") or ""
        content = it.get("content_html") or ""
        full = title + " " + content
        if keyword not in full:
            continue
        pid = it.get("id") or it.get("url") or title
        result.append({
            "source": "douban(rsshub)",
            "id": f"rsshub-douban-{pid}",
            "keyword": keyword,
            "title": title.strip(),
            "city": "",
            "venue": "",
            "date": it.get("date_published") or "",
            "price": 0,
            "url": it.get("url") or "",
        })
    if debug:
        log(f"  [RSSHub/豆瓣] '{keyword}' 原始 {len(items)} 条 -> 过滤后 {len(result)} 条命中")
    return result


# ============================================================
# 数据源六:demo 模式 (内置假数据, 用于验证链路)
# ============================================================
# 首轮返回 3 条作为基线, 之后每轮基于当前时间注入 1-2 条"新演出",
# 用来验证: 数据源一旦通了, 整个 diff + 打印链路是否正确
_DEMO_BASE = [
    {"id": "demo-1001", "title": "告五人【带你飞】2026 巡演 北京站",
     "city": "北京", "venue": "M 空间", "date": "2026-06-08 20:00", "price": 380},
    {"id": "demo-1002", "title": "极品贵公子 拯旧 2012 巡演 上海站",
     "city": "上海", "venue": "MAO Livehouse", "date": "2026-05-20 20:00", "price": 188},
    {"id": "demo-1003", "title": "万能青年旅店 2026 广州站",
     "city": "广州", "venue": "中山纪念堂", "date": "2026-07-12 19:30", "price": 680},
]


def fetch_demo(keyword, debug=False):
    """演示数据源 - 每次调用会注入一条基于当前秒数的'新演出',模拟上新事件"""
    # 稳定的 3 条基础数据
    base = [dict(it, source="demo", keyword=keyword,
                 url=f"https://example.com/{it['id']}") for it in _DEMO_BASE
            if keyword in it["title"] or keyword == "*"]

    # 每 5 秒一个新 ID, 方便连续两次 --once 就能看到"新演出被识别"
    fresh_id = int(time.time() // 5)
    fresh = {
        "id": f"demo-fresh-{fresh_id}",
        "source": "demo",
        "keyword": keyword,
        "title": f"[DEMO 新上] {keyword} 巡演 测试站 #{fresh_id}",
        "city": "测试城市",
        "venue": "测试场馆",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "price": 299,
        "url": f"https://example.com/demo-{fresh_id}",
    }
    if debug:
        log(f"  [demo] '{keyword}' 返回 {len(base)} 条基础 + 1 条每分钟新数据")
    return base + [fresh]


# ============================================================
# 抓取分发
# ============================================================
PLATFORM_FUNCS = {
    "showstart": fetch_showstart,
    "damai": fetch_damai,
    "juzimima": fetch_juzimima,
    "sina": fetch_sina_star,
    "douban": fetch_douban,
    "weibo": fetch_weibo,
    "rsshub_weibo": fetch_rsshub_weibo,
    "rsshub_douban": fetch_rsshub_douban,
    "demo": fetch_demo,
}


def fetch_all(keyword, platforms, debug=False):
    all_items = []
    for name, enabled in platforms.items():
        if not enabled:
            continue
        func = PLATFORM_FUNCS.get(name)
        if not func:
            continue
        items = func(keyword, debug=debug)
        log(f"  [{name}] 关键词 '{keyword}': {len(items)} 条")
        all_items.extend(items)
    return all_items


# ============================================================
# 输出
# ============================================================
# ============================================================
# 推送 (PushPlus -> 微信; 后续可扩展钉钉/Server酱/Bark)
# ============================================================
def _build_md_for_push(items):
    """把多条 items 渲染成一段 Markdown, 标题已含安全关键字 '提醒'"""
    grouped = {}
    for it in items:
        kw = it.get("keyword", "其他")
        grouped.setdefault(kw, []).append(it)

    lines = []
    for kw, kw_items in grouped.items():
        lines.append(f"## **{kw}** ({len(kw_items)} 条)")
        lines.append("")
        for it in kw_items:
            full = it.get("full_name") or it.get("title", "")
            lines.append(f"### {full}")
            meta = []
            if it.get("status"):
                meta.append(f"在售: **{it['status']}**")
            if it.get("city"):
                meta.append(f"城市: {it['city']}")
            if it.get("venue"):
                meta.append(f"场馆: {it['venue']}")
            if it.get("date"):
                meta.append(f"时间: {it['date']}")
            if it.get("price_range"):
                meta.append(f"票价: ¥{it['price_range']}")
            elif it.get("price"):
                meta.append(f"起价: ¥{it['price']}")
            if meta:
                lines.append("  \n".join(meta))  # markdown 强制换行用两个空格+\n
            if it.get("url"):
                lines.append(f"\n[> 打开详情]({it['url']})")
            lines.append("")
            lines.append("---")
            lines.append("")
    return "\n".join(lines)


def send_pushplus(items, debug=False):
    """
    通过 PushPlus 推送到微信。
    需要环境变量 PUSHPLUS_TOKEN, 没配就静默跳过。
    """
    if not items:
        return
    import os
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        if debug:
            log("[PushPlus] 未配置 PUSHPLUS_TOKEN, 跳过推送")
        return

    title = f"演出提醒 · {len(items)} 条新动态"
    content = _build_md_for_push(items)

    # PushPlus 单条 content 上限 64KB, 我们这点字数不会超
    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": token,
                "title": title,
                "content": content,
                "template": "markdown",
            },
            timeout=15,
        )
        if r.status_code == 200:
            try:
                resp = r.json()
                if resp.get("code") == 200:
                    log(f"[PushPlus] 已推送 {len(items)} 条")
                else:
                    log(f"[PushPlus] 推送失败 code={resp.get('code')} msg={resp.get('msg')}",
                        "ERROR")
            except Exception:
                log(f"[PushPlus] 响应解析失败: {r.text[:200]}", "ERROR")
        else:
            log(f"[PushPlus] HTTP {r.status_code}: {r.text[:200]}", "ERROR")
    except Exception as e:
        log(f"[PushPlus] 推送异常: {e}", "ERROR")


def notify_new_items(items, debug=False):
    """统一推送入口, 后续要加钉钉/邮箱/Bark 在这里加分支"""
    if not items:
        return
    send_pushplus(items, debug=debug)


def print_show(item):
    log("-" * 60, tag="NEW ")
    log(f"  [发现新演出] 来源: {item.get('source', '')}", tag="NEW ")
    log(f"  关键词  : {item.get('keyword', '')}", tag="NEW ")
    title = item.get("full_name") or item.get("title", "")
    log(f"  标题    : {title}", tag="NEW ")
    if item.get("status"):
        log(f"  在售状态: {item['status']}", tag="NEW ")
    if item.get("city"):
        log(f"  城市    : {item['city']}", tag="NEW ")
    log(f"  场馆    : {item.get('venue', '')}", tag="NEW ")
    if item.get("address"):
        log(f"  详细地址: {item['address']}", tag="NEW ")
    log(f"  时间    : {item.get('date', '')}", tag="NEW ")
    if item.get("price_range"):
        log(f"  票价区间: ¥{item['price_range']}", tag="NEW ")
    elif item.get("price"):
        log(f"  起价    : ¥{item['price']}", tag="NEW ")
    log(f"  链接    : {item.get('url', '')}", tag="NEW ")
    log("-" * 60, tag="NEW ")


# ============================================================
# 主循环
# ============================================================
def run_once(cfg, seen, first_run=False):
    debug = cfg.get("debug", False)
    preview_n = cfg.get("first_run_preview", 0)

    round_new = []
    round_total = 0
    for kw in cfg["keywords"]:
        items = fetch_all(kw, cfg["platforms"], debug=debug)
        round_total += len(items)
        for it in items:
            key = f"{it['source']}:{it['id']}"
            if key in seen:
                continue
            seen.add(key)
            round_new.append(it)

    if first_run and round_new:
        # 首轮:打印前 N 条,让用户立刻看到"链路通了"的效果;
        # 剩下的只是加入基线,不刷屏。
        preview = round_new[:preview_n]
        rest = len(round_new) - len(preview)
        log(f"首轮建立基线: 共抓到 {round_total} 条演出, 预览前 {len(preview)} 条:")
        for it in preview:
            print_show(it)
        if rest > 0:
            log(f"其余 {rest} 条已加入基线(不打印),下次起只报新增。")
        return []

    if round_new:
        log(f"本轮新增 {len(round_new)} 条演出")
        for it in round_new:
            print_show(it)
    else:
        log(f"本轮无新增 (共查询到 {round_total} 条,均已见过)")
    return round_new


# ============================================================
# CI 模式:写 Markdown 报告供 GitHub Actions 提交
# ============================================================
LATEST_MD = ROOT / "latest.md"


def _format_item_md(it):
    lines = [f"### {it.get('full_name') or it.get('title', '')}"]
    lines.append("")
    src = it.get("source", "")
    kw = it.get("keyword", "")
    lines.append(f"- 艺人/关键词: **{kw}**")
    lines.append(f"- 来源: `{src}`")
    if it.get("status"):
        lines.append(f"- 在售状态: **{it['status']}**")
    if it.get("city"):
        lines.append(f"- 城市: {it['city']}")
    if it.get("venue"):
        lines.append(f"- 场馆: {it['venue']}")
    if it.get("address"):
        lines.append(f"- 详细地址: {it['address']}")
    if it.get("date"):
        lines.append(f"- 时间: {it['date']}")
    if it.get("price_range"):
        lines.append(f"- 票价区间: ¥{it['price_range']}")
    elif it.get("price"):
        lines.append(f"- 起价: ¥{it['price']}")
    if it.get("url"):
        lines.append(f"- 链接: <{it['url']}>")
    lines.append("")
    return "\n".join(lines)


def write_latest_md(new_items, first_run, round_total):
    """CI 模式每轮运行后更新 latest.md 供 GitHub 仓库展示"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        f"# 艺人演出上新监控 — 最新报告",
        "",
        f"- 最近更新: **{now}**",
        f"- 本轮扫到 {round_total} 条, 新增 **{len(new_items)}** 条",
        "",
        "---",
        "",
    ]

    # 累加模式:保留旧 latest.md 的"历史新发现", 新发现追加到顶部
    old_history = ""
    if LATEST_MD.exists() and not first_run:
        try:
            old_text = LATEST_MD.read_text(encoding="utf-8")
            # 保留从 "## 历史发现" 之后的内容
            idx = old_text.find("## 历史发现")
            if idx >= 0:
                old_history = old_text[idx:]
        except Exception:
            pass

    body = []
    if first_run:
        body.append("## 基线数据 (首次运行)")
        body.append("")
        body.append(f"首次运行, 已把当前 {round_total} 条演出全部作为基线入库。")
        body.append("从第 2 轮起,只有真正新增的演出才会出现在下方。")
        body.append("")
    elif new_items:
        body.append(f"## 本轮新发现 ({len(new_items)} 条)")
        body.append("")
        for it in new_items:
            body.append(_format_item_md(it))
        body.append("")
    else:
        body.append("## 本轮无新增")
        body.append("")
        body.append("数据库里所有演出/动态都已见过,暂无新增。")
        body.append("")

    # 把当轮新发现同时累加到"历史发现"
    if new_items and not first_run:
        history_block = ["", "---", "", "## 历史发现", ""]
        history_block.append(f"### {now}  ({len(new_items)} 条)")
        history_block.append("")
        for it in new_items:
            history_block.append(f"- **{it.get('keyword','')}** | "
                                 f"{it.get('full_name') or it.get('title','')} "
                                 f"<{it.get('url','')}>")
        history_block.append("")
        combined_history = "\n".join(history_block)
        # 把新 history 块插入旧 history 之前
        if old_history:
            # 去掉 "## 历史发现" 重复头
            old_history_body = old_history.replace("## 历史发现", "", 1).lstrip("\n")
            full_history = combined_history + "\n" + old_history_body
        else:
            full_history = combined_history
    else:
        full_history = old_history if old_history else ""

    full_content = "\n".join(header + body) + full_history
    LATEST_MD.write_text(full_content, encoding="utf-8")
    log(f"CI 报告已写入: {LATEST_MD}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只跑一轮退出")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    parser.add_argument("--reset", action="store_true", help="清空已见记录")
    parser.add_argument("--interval", type=int, help="覆盖配置里的轮询间隔(秒)")
    parser.add_argument("--demo", action="store_true",
                        help="开启 demo 模式:用内置假数据验证链路")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式:跑完一轮后把新发现写到 latest.md, 供 GitHub Actions commit")
    parser.add_argument("--test-notify", action="store_true",
                        help="只发一条测试推送, 不抓数据 (用于验证 PushPlus 链路是否打通)")
    args = parser.parse_args()

    # --test-notify: 直接发一条假消息测试推送链路, 立刻退出
    if args.test_notify:
        log("test-notify 模式: 发送测试推送验证链路 ...")
        fake = [{
            "keyword": "测试",
            "id": "self-test-001",
            "source": "self-test",
            "title": "演出监控部署成功 · 推送链路验证 · 提醒",
            "status": "测试通过",
            "venue": "GitHub Actions Cloud",
            "city": "云端",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "price_range": "0-0",
            "url": "https://github.com/guangli395/artist-show-monitor",
        }]
        notify_new_items(fake, debug=True)
        log("test-notify 完成, 检查你的微信公众号 'PushPlus推送加'")
        return

    cfg = load_config()
    if args.debug:
        cfg["debug"] = True
    if args.interval:
        cfg["interval_seconds"] = args.interval
    if args.demo:
        # demo 模式: 关闭其它平台, 只开 demo
        cfg["platforms"] = {k: False for k in cfg["platforms"]}
        cfg["platforms"]["demo"] = True
        log("已开启 --demo 模式, 使用内置数据源验证完整链路")

    if args.reset and SEEN_FILE.exists():
        SEEN_FILE.unlink()
        log("已清空 seen.json")

    banner("艺人演出上新监控 - 本地验证版")
    log(f"监控关键词 : {cfg['keywords']}")
    log(f"启用平台   : {[k for k, v in cfg['platforms'].items() if v]}")
    log(f"轮询间隔   : {cfg['interval_seconds']} 秒")
    log(f"调试模式   : {cfg['debug']}")
    log(f"日志文件   : {LOG_FILE}")
    log(f"状态文件   : {SEEN_FILE}")
    log("按 Ctrl+C 退出\n")

    seen = load_seen()
    first_run = len(seen) == 0
    if first_run:
        log("首次运行:首轮会抓一批并建立基线(只预览前几条),从第二轮起才算新增。")

    round_num = 0
    try:
        while True:
            round_num += 1
            banner(f"第 {round_num} 轮检查  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            round_new = []
            round_total_approx = 0
            try:
                is_first = first_run and round_num == 1
                round_new = run_once(cfg, seen, first_run=is_first) or []
                round_total_approx = len(seen)
            except Exception as e:
                log(f"本轮异常: {e}", "ERROR")
            save_seen(seen)

            # CI 模式: 每轮跑完写一份 Markdown 报告给 GitHub Actions commit
            if args.ci:
                try:
                    write_latest_md(
                        round_new,
                        first_run=(first_run and round_num == 1),
                        round_total=round_total_approx,
                    )
                except Exception as e:
                    log(f"写 latest.md 失败: {e}", "ERROR")

            # 推送新发现到微信(PushPlus)/未来扩展更多通道
            # 注意: run_once 在首轮已经返回 [], 所以 round_new 天然不含基线
            if round_new:
                try:
                    notify_new_items(round_new, debug=cfg.get("debug", False))
                except Exception as e:
                    log(f"推送异常: {e}", "ERROR")

            if args.once:
                log("--once 指定,退出。")
                break

            log(f"下一轮 {cfg['interval_seconds']} 秒后...\n")
            time.sleep(cfg["interval_seconds"])
    except KeyboardInterrupt:
        log("\n用户中断,保存状态并退出。")
        save_seen(seen)


if __name__ == "__main__":
    main()
