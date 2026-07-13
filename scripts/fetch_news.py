"""
A股新闻聚合爬虫
数据源：
  - 新浪财经滚动新闻 API（实时）
  - AKShare 东方财富快讯
  - RSS（财联社/证券时报/第一财经，失败自动跳过）
输出：data/news.json + docs/index.html
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "news.json"
HTML_FILE = ROOT / "docs" / "index.html"
TZ_BJ = timezone(timedelta(hours=8))

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "商业航天/军工": ["航天", "航空", "火箭", "卫星", "军工", "国防", "长征号", "载人", "飞船"],
    "半导体/芯片":  ["半导体", "芯片", "集成电路", "晶圆", "封装测试", "算力", "GPU", "NVIDIA", "英伟达", "光刻"],
    "AI/科技":     ["人工智能", "AI", "大模型", "GPT", "科技", "数字经济", "云计算", "智算", "机器人"],
    "新能源":      ["新能源", "储能", "光伏", "风电", "氢能", "碳中和", "锂电", "充电桩", "电池"],
    "医药/生物":   ["医药", "医疗", "生物", "创新药", "基因", "疫苗", "器械", "CXO", "制药"],
    "消费":        ["消费", "白酒", "食品", "零售", "餐饮", "茅台", "五粮液", "品牌", "电商"],
    "金融":        ["银行", "证券", "保险", "基金", "利率", "美联储", "降息", "加息", "债券", "股市"],
    "有色/资源":   ["黄金", "铜", "原油", "煤炭", "资源", "锂矿", "稀土", "铁矿", "石油"],
    "宏观/政策":   ["政策", "发改委", "央行", "宏观", "GDP", "CPI", "PMI", "人民币", "财政", "货币"],
    "国际市场":    ["美股", "港股", "纳指", "标普", "道琼斯", "地缘", "关税", "特朗普", "制裁", "欧股"],
}

POSITIVE_WORDS = ["暴涨", "大涨", "利好", "突破", "创新高", "增长", "扩产", "获批", "签约",
                  "上涨", "新高", "超预期", "提速", "放量", "涨停", "启动", "落地", "获得",
                  "反弹", "走强", "跑赢", "超额", "盈利", "丰收"]
NEGATIVE_WORDS = ["暴跌", "大跌", "利空", "下调", "亏损", "减持", "退市", "制裁", "降级",
                  "下跌", "跌停", "风险", "警告", "撤资", "下滑", "腰斩", "暴雷", "违约",
                  "亏损", "崩盘", "熔断", "跌破", "做空"]
BREAKING_WORDS = ["突发", "紧急", "重磅", "速报", "刚刚", "快讯", "重大", "紧急"]
IMPORTANT_WORDS = ["重要", "公告", "发布", "批准", "决定", "通知", "声明", "会议", "政策"]

RSS_SOURCES = [
    {"name": "财联社", "url": "https://www.cls.cn/rss", "timeout": 10},
    {"name": "证券时报", "url": "https://www.stcn.com/rss/news.xml", "timeout": 10},
    {"name": "第一财经", "url": "https://www.yicai.com/rss/news.xml", "timeout": 10},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _hash(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()[:12]


def _tag_sector(text: str) -> str:
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return sector
    return "综合/其他"


def _tag_sentiment(title: str) -> str:
    for w in POSITIVE_WORDS:
        if w in title:
            return "positive"
    for w in NEGATIVE_WORDS:
        if w in title:
            return "negative"
    return "neutral"


def _tag_importance(title: str) -> str:
    for w in BREAKING_WORDS:
        if w in title:
            return "breaking"
    for w in IMPORTANT_WORDS:
        if w in title:
            return "important"
    return "normal"


def _ts_to_iso(ts) -> str:
    """Unix 时间戳或字符串转北京时间 ISO 字符串"""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(int(ts), tz=TZ_BJ).isoformat(timespec="minutes")
        if isinstance(ts, str) and ts.strip().isdigit():
            return datetime.fromtimestamp(int(ts), tz=TZ_BJ).isoformat(timespec="minutes")
    except Exception:
        pass
    return datetime.now(TZ_BJ).isoformat(timespec="minutes")


def _feedparser_time_to_iso(t) -> str:
    try:
        import time as _time
        ts = _time.mktime(t)
        return datetime.fromtimestamp(ts, tz=TZ_BJ).isoformat(timespec="minutes")
    except Exception:
        return datetime.now(TZ_BJ).isoformat(timespec="minutes")


def _make_item(title, summary, url, source, pub_time_iso) -> dict:
    title = (title or "").strip()
    summary = re.sub(r"<[^>]+>", "", (summary or "")).strip()[:200]
    text = title + summary
    return {
        "id": _hash(title),
        "title": title,
        "summary": summary,
        "url": url or "",
        "source": source,
        "pub_time": pub_time_iso,
        "sector": _tag_sector(text),
        "sentiment": _tag_sentiment(title),
        "importance": _tag_importance(title),
    }


# ── 新浪财经滚动新闻（主力数据源）────────────────────────────
def fetch_sina() -> list[dict]:
    items = []
    # lid=2509 财经综合，lid=2516 A股，lid=2514 港美股
    for lid, label in [("2509", "新浪财经"), ("2516", "新浪A股")]:
        try:
            url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid={lid}&k=&num=50&page=1&r=0.3"
            r = requests.get(url, timeout=10, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
            entries = data.get("result", {}).get("data", [])
            for e in entries:
                title = e.get("title", "")
                if not title:
                    continue
                summary = e.get("intro", "") or e.get("content", "")
                link = e.get("url", "") or e.get("link", "")
                ctime = e.get("ctime", "") or e.get("mtime", "")
                items.append(_make_item(title, summary, link, label, _ts_to_iso(ctime)))
            print(f"  新浪[{label}]: {len(entries)} 条")
        except Exception as e:
            print(f"  新浪[{label}] 失败: {e}", file=sys.stderr)
    return items


# ── AKShare 东方财富快讯 ───────────────────────────────────
def fetch_akshare() -> list[dict]:
    if not HAS_AKSHARE:
        print("  AKShare 未安装，跳过", file=sys.stderr)
        return []
    items = []
    try:
        df = ak.stock_news_em(symbol="000001")
        col_map = {
            "新闻标题": "title", "title": "title",
            "新闻内容": "summary", "content": "summary",
            "新闻链接": "url", "url": "url",
            "文章来源": "source",
            "发布时间": "pub_time",
        }
        for _, row in df.head(50).iterrows():
            title = str(row.get("新闻标题", row.get("title", "")) or "").strip()
            if not title:
                continue
            summary = str(row.get("新闻内容", row.get("content", "")) or "")[:200]
            link = str(row.get("新闻链接", row.get("url", "")) or "")
            source = str(row.get("文章来源", "东方财富") or "东方财富")
            pt = str(row.get("发布时间", "") or "")
            items.append(_make_item(title, summary, link, source, _ts_to_iso(pt) if pt.isdigit() else pt or datetime.now(TZ_BJ).isoformat(timespec="minutes")))
        print(f"  AKShare 东方财富: {len(items)} 条")
    except Exception as e:
        print(f"  AKShare 失败: {e}", file=sys.stderr)
    return items


# ── RSS 数据源（失败跳过）────────────────────────────────────
def fetch_rss_source(source: dict) -> list[dict]:
    if not HAS_FEEDPARSER:
        return []
    items = []
    try:
        r = requests.get(source["url"], timeout=source["timeout"], headers=HEADERS)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        for entry in feed.entries[:30]:
            title = getattr(entry, "title", "")
            if not title:
                continue
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            link = getattr(entry, "link", "")
            pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            pt = _feedparser_time_to_iso(pub) if pub else datetime.now(TZ_BJ).isoformat(timespec="minutes")
            items.append(_make_item(title, summary, link, source["name"], pt))
        print(f"  RSS [{source['name']}]: {len(items)} 条")
    except Exception as e:
        print(f"  RSS [{source['name']}] 失败: {e}", file=sys.stderr)
    return items


# ── 汇总抓取 ──────────────────────────────────────────────
def fetch_all() -> list[dict]:
    all_items: list[dict] = []
    seen: set[str] = set()

    def add(items):
        for item in items:
            if item["id"] not in seen and item["title"]:
                seen.add(item["id"])
                all_items.append(item)

    print("抓取新浪财经...")
    add(fetch_sina())

    print("抓取 AKShare 东方财富...")
    add(fetch_akshare())

    print("抓取 RSS 数据源...")
    for src in RSS_SOURCES:
        add(fetch_rss_source(src))

    all_items.sort(key=lambda x: x["pub_time"], reverse=True)
    return all_items[:200]


# ── 输出 news.json ─────────────────────────────────────────
def save_json(items: list[dict]) -> dict:
    payload = {
        "last_updated": datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M 北京时间"),
        "total": len(items),
        "items": items,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {len(items)} 条 → {DATA_FILE}")
    return payload


# ── 渲染 index.html ────────────────────────────────────────
def render_html(payload: dict):
    news_json_str = json.dumps(payload, ensure_ascii=False)
    sectors = ["全部"] + list(SECTOR_KEYWORDS.keys()) + ["综合/其他"]
    sector_options = "\n".join(
        '<button class="sector-btn {}" data-sector="{}">{}</button>'.format(
            "active" if s == "全部" else "", s, s
        )
        for s in sectors
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股财经资讯聚合</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif; }}
  .sector-btn {{
    padding: 4px 12px; border-radius: 999px; border: 1px solid #d1d5db;
    font-size: 13px; cursor: pointer; white-space: nowrap;
    background: #f9fafb; color: #374151; transition: all .15s;
  }}
  .sector-btn.active, .sector-btn:hover {{
    background: #1d4ed8; color: #fff; border-color: #1d4ed8;
  }}
  .sentiment-bar {{ width: 4px; min-height: 100%; border-radius: 2px; flex-shrink: 0; }}
  .bar-positive {{ background: #16a34a; }}
  .bar-negative {{ background: #dc2626; }}
  .bar-neutral  {{ background: #9ca3af; }}
  .news-card {{ display: flex; gap: 10px; align-items: stretch; }}
</style>
</head>
<body class="bg-gray-50 min-h-screen">

<header class="bg-white shadow-sm sticky top-0 z-10">
  <div class="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between">
    <span class="text-xl font-bold text-blue-700">📈 A股财经资讯</span>
    <div class="text-xs text-gray-400" id="update-time">加载中...</div>
  </div>
</header>

<div class="bg-white border-b sticky top-14 z-10">
  <div class="max-w-5xl mx-auto px-4 py-2">
    <div class="flex gap-2 overflow-x-auto pb-1" id="sector-filter">
      {sector_options}
    </div>
  </div>
</div>

<div class="max-w-5xl mx-auto px-4 pt-4 pb-2">
  <input id="search-box" type="text" placeholder="🔍 搜索关键词（标题/来源/板块）..."
    class="w-full px-4 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
</div>
<div class="max-w-5xl mx-auto px-4 pb-2">
  <div class="text-xs text-gray-400" id="stats-bar">共 0 条</div>
</div>

<main class="max-w-5xl mx-auto px-4 pb-10">
  <div id="news-list" class="grid gap-3 grid-cols-1 md:grid-cols-2"></div>
  <div id="empty-msg" class="text-center py-16 text-gray-400 hidden">没有找到相关新闻</div>
</main>

<script>
const RAW_DATA = {news_json_str};
const SENTIMENT_LABEL = {{positive:"🟢 利好",negative:"🔴 利空",neutral:"⚪ 中性"}};
const IMPORTANCE_LABEL = {{breaking:"🔥 突发",important:"⭐ 重要",normal:"📄 一般"}};
const SECTOR_COLORS = {{
  "商业航天/军工":"bg-purple-100 text-purple-700",
  "半导体/芯片":"bg-blue-100 text-blue-700",
  "AI/科技":"bg-indigo-100 text-indigo-700",
  "新能源":"bg-green-100 text-green-700",
  "医药/生物":"bg-pink-100 text-pink-700",
  "消费":"bg-orange-100 text-orange-700",
  "金融":"bg-yellow-100 text-yellow-700",
  "有色/资源":"bg-amber-100 text-amber-700",
  "宏观/政策":"bg-gray-100 text-gray-700",
  "国际市场":"bg-sky-100 text-sky-700",
  "综合/其他":"bg-slate-100 text-slate-500",
}};
let activeSector = "全部", searchKw = "";

function formatTime(iso) {{
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2,"0");
  return `${{d.getMonth()+1}}/${{p(d.getDate())}} ${{p(d.getHours())}}:${{p(d.getMinutes())}}`;
}}

function renderCard(item) {{
  const bar = item.sentiment==="positive"?"bar-positive":item.sentiment==="negative"?"bar-negative":"bar-neutral";
  const sc = SECTOR_COLORS[item.sector]||"bg-slate-100 text-slate-500";
  const titleEl = item.url
    ? `<a href="${{item.url}}" target="_blank" rel="noopener" class="font-medium text-gray-900 hover:text-blue-600 leading-snug">${{item.title}}</a>`
    : `<span class="font-medium text-gray-900 leading-snug">${{item.title}}</span>`;
  return `<div class="bg-white rounded-xl shadow-sm p-4 news-card hover:shadow-md transition-shadow">
    <div class="sentiment-bar ${{bar}}"></div>
    <div class="flex-1 min-w-0">
      <div class="mb-1">${{titleEl}}</div>
      ${{item.summary?`<p class="text-xs text-gray-500 mb-2 leading-relaxed">${{item.summary.slice(0,80)}}${{item.summary.length>80?"…":""}}</p>`:""}}
      <div class="flex flex-wrap items-center gap-1.5 text-xs text-gray-400">
        <span>${{item.source}}</span><span>·</span><span>${{formatTime(item.pub_time)}}</span>
        <span class="ml-auto"></span>
        <span class="px-1.5 py-0.5 rounded-full ${{sc}}">${{item.sector}}</span>
        <span>${{IMPORTANCE_LABEL[item.importance]||""}}</span>
        <span>${{SENTIMENT_LABEL[item.sentiment]||""}}</span>
      </div>
    </div>
  </div>`;
}}

function applyFilters() {{
  const kw = searchKw.toLowerCase();
  const filtered = RAW_DATA.items.filter(item => {{
    const ms = activeSector==="全部"||item.sector===activeSector;
    const mk = !kw||(item.title||"").toLowerCase().includes(kw)||(item.summary||"").toLowerCase().includes(kw)||(item.source||"").toLowerCase().includes(kw)||(item.sector||"").toLowerCase().includes(kw);
    return ms && mk;
  }});
  document.getElementById("news-list").innerHTML = filtered.map(renderCard).join("");
  document.getElementById("empty-msg").classList.toggle("hidden", filtered.length>0);
  document.getElementById("stats-bar").textContent =
    `共 ${{filtered.length}} 条`+(activeSector!=="全部"?` · ${{activeSector}}`:"")+(kw?` · 搜索:"${{searchKw}}")`:"");
}}

document.getElementById("update-time").textContent = "最后更新：" + (RAW_DATA.last_updated||"未知");
document.getElementById("sector-filter").addEventListener("click", e => {{
  const btn = e.target.closest(".sector-btn");
  if (!btn) return;
  document.querySelectorAll(".sector-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");
  activeSector = btn.dataset.sector;
  applyFilters();
}});
document.getElementById("search-box").addEventListener("input", e => {{
  searchKw = e.target.value.trim();
  applyFilters();
}});
applyFilters();
</script>
</body>
</html>"""

    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"已生成 HTML → {HTML_FILE}")


if __name__ == "__main__":
    print("=== A股新闻聚合爬虫启动 ===")
    items = fetch_all()
    print(f"\n共抓取 {len(items)} 条（去重后）")
    payload = save_json(items)
    render_html(payload)
    print("=== 完成 ===")
