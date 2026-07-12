"""
A股新闻聚合爬虫
数据源：RSS（新华社/财联社/证券时报/路透中文）+ AKShare 东方财富快讯
输出：data/news.json + docs/index.html
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests

# ── 可选：AKShare（失败时跳过）──────────────────────────────
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

# ── 项目根目录（脚本在 scripts/ 下，根目录向上一级）──────────
ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "news.json"
HTML_FILE = ROOT / "docs" / "index.html"

# ── 板块关键词（顺序即优先级，匹配第一个命中的板块）──────────
SECTOR_KEYWORDS: dict[str, list[str]] = {
    "商业航天/军工": ["航天", "航空", "火箭", "卫星", "军工", "国防", "长征号", "载人"],
    "半导体/芯片":  ["半导体", "芯片", "集成电路", "晶圆", "封装测试", "算力", "GPU", "NVIDIA"],
    "AI/科技":     ["人工智能", "AI", "大模型", "GPT", "科技", "数字经济", "云计算", "智算"],
    "新能源":      ["新能源", "储能", "光伏", "风电", "氢能", "碳中和", "锂电", "充电桩"],
    "医药/生物":   ["医药", "医疗", "生物", "创新药", "基因", "疫苗", "器械", "CXO"],
    "消费":        ["消费", "白酒", "食品", "零售", "餐饮", "茅台", "五粮液", "品牌"],
    "金融":        ["银行", "证券", "保险", "基金", "利率", "美联储", "降息", "加息", "债券"],
    "有色/资源":   ["黄金", "铜", "原油", "煤炭", "资源", "锂矿", "稀土", "铁矿"],
    "宏观/政策":   ["政策", "发改委", "央行", "宏观", "GDP", "CPI", "PMI", "人民币", "财政"],
    "国际市场":    ["美股", "港股", "纳指", "标普", "道琼斯", "地缘", "关税", "特朗普", "制裁"],
}

# ── 情感关键词 ─────────────────────────────────────────────
POSITIVE_WORDS = ["暴涨", "大涨", "利好", "突破", "创新高", "增长", "扩产", "获批", "签约",
                  "上涨", "新高", "超预期", "提速", "放量", "涨停", "启动", "落地", "获得"]
NEGATIVE_WORDS = ["暴跌", "大跌", "利空", "下调", "亏损", "减持", "退市", "制裁", "降级",
                  "下跌", "跌停", "亏损", "风险", "警告", "撤资", "下滑", "腰斩", "暴雷"]
BREAKING_WORDS = ["突发", "紧急", "重磅", "速报", "刚刚", "快讯", "突破", "震惊"]
IMPORTANT_WORDS = ["重要", "公告", "发布", "批准", "决定", "通知", "声明", "会议"]

# ── RSS 数据源列表 ────────────────────────────────────────
RSS_SOURCES = [
    {
        "name": "新华社财经",
        "url": "http://www.xinhuanet.com/finance/news_finance.xml",
        "timeout": 10,
    },
    {
        "name": "证券时报",
        "url": "https://www.stcn.com/rss/news.xml",
        "timeout": 10,
    },
    {
        "name": "财联社",
        "url": "https://www.cls.cn/rss",
        "timeout": 10,
    },
    {
        "name": "路透中文财经",
        "url": "https://feeds.reuters.com/reuters/CNbusinessNews",
        "timeout": 10,
    },
    {
        "name": "第一财经",
        "url": "https://www.yicai.com/rss/news.xml",
        "timeout": 10,
    },
]

# ── 辅助函数 ──────────────────────────────────────────────

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


def _normalize_time(t) -> str:
    """把各种时间格式统一为 ISO8601 字符串（北京时间）"""
    tz_bj = timezone(timedelta(hours=8))
    if t is None:
        return datetime.now(tz_bj).isoformat(timespec="minutes")
    if isinstance(t, str):
        return t
    # feedparser time_struct
    try:
        import time as _time
        ts = _time.mktime(t)
        return datetime.fromtimestamp(ts, tz=tz_bj).isoformat(timespec="minutes")
    except Exception:
        return datetime.now(tz_bj).isoformat(timespec="minutes")


def _make_item(title: str, summary: str, url: str, source: str, pub_time) -> dict:
    title = (title or "").strip()
    summary = (summary or "").strip()[:200]
    text = title + summary
    return {
        "id": _hash(title),
        "title": title,
        "summary": summary,
        "url": url or "",
        "source": source,
        "pub_time": _normalize_time(pub_time),
        "sector": _tag_sector(text),
        "sentiment": _tag_sentiment(title),
        "importance": _tag_importance(title),
    }


# ── 数据抓取 ───────────────────────────────────────────────

def fetch_rss(source: dict) -> list[dict]:
    items = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StockNewsBot/1.0)"}
        resp = requests.get(source["url"], timeout=source["timeout"], headers=headers)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:30]:
            title = getattr(entry, "title", "")
            if not title:
                continue
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            # 去掉摘要里的 HTML 标签
            import re
            summary = re.sub(r"<[^>]+>", "", summary)
            link = getattr(entry, "link", "")
            pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            items.append(_make_item(title, summary, link, source["name"], pub))
        print(f"  RSS [{source['name']}]: {len(items)} 条")
    except Exception as e:
        print(f"  RSS [{source['name']}] 失败: {e}", file=sys.stderr)
    return items


def fetch_akshare() -> list[dict]:
    if not HAS_AKSHARE:
        print("  AKShare 未安装，跳过", file=sys.stderr)
        return []
    items = []
    try:
        df = ak.stock_news_em(symbol="000001")  # 取综合财经新闻
        for _, row in df.head(50).iterrows():
            title = str(row.get("新闻标题", "") or row.get("title", ""))
            if not title:
                continue
            summary = str(row.get("新闻内容", "") or row.get("content", ""))[:200]
            url = str(row.get("新闻链接", "") or row.get("url", ""))
            source = str(row.get("文章来源", "") or "东方财富")
            pub = str(row.get("发布时间", "") or row.get("pub_time", ""))
            items.append(_make_item(title, summary, url, source, pub))
        print(f"  AKShare 东方财富: {len(items)} 条")
    except Exception as e:
        print(f"  AKShare 失败: {e}", file=sys.stderr)
    return items


def fetch_all() -> list[dict]:
    all_items: list[dict] = []
    seen: set[str] = set()

    print("抓取 RSS 数据源...")
    for src in RSS_SOURCES:
        for item in fetch_rss(src):
            if item["id"] not in seen:
                seen.add(item["id"])
                all_items.append(item)

    print("抓取 AKShare 东方财富...")
    for item in fetch_akshare():
        if item["id"] not in seen:
            seen.add(item["id"])
            all_items.append(item)

    # 按发布时间倒序，取最新 200 条
    all_items.sort(key=lambda x: x["pub_time"], reverse=True)
    return all_items[:200]


# ── 输出 news.json ─────────────────────────────────────────

def save_json(items: list[dict]):
    tz_bj = timezone(timedelta(hours=8))
    payload = {
        "last_updated": datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M 北京时间"),
        "total": len(items),
        "items": items,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {len(items)} 条新闻 → {DATA_FILE}")
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
  .sentiment-bar {{
    width: 4px; min-height: 100%; border-radius: 2px; flex-shrink: 0;
  }}
  .bar-positive {{ background: #16a34a; }}
  .bar-negative {{ background: #dc2626; }}
  .bar-neutral  {{ background: #9ca3af; }}
  .news-card {{ display: flex; gap: 10px; align-items: stretch; }}
  @media (max-width: 640px) {{ .grid-cols-2 {{ grid-template-columns: 1fr !important; }} }}
</style>
</head>
<body class="bg-gray-50 min-h-screen">

<!-- 顶部导航 -->
<header class="bg-white shadow-sm sticky top-0 z-10">
  <div class="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2">
      <span class="text-xl font-bold text-blue-700">📈 A股财经资讯</span>
    </div>
    <div class="text-xs text-gray-400" id="update-time">加载中...</div>
  </div>
</header>

<!-- 筛选栏 -->
<div class="bg-white border-b sticky top-14 z-10">
  <div class="max-w-5xl mx-auto px-4 py-2">
    <div class="flex gap-2 overflow-x-auto pb-1 scrollbar-hide" id="sector-filter">
      {sector_options}
    </div>
  </div>
</div>

<!-- 搜索框 -->
<div class="max-w-5xl mx-auto px-4 pt-4 pb-2">
  <input id="search-box" type="text" placeholder="🔍 搜索关键词（标题/来源/板块）..."
    class="w-full px-4 py-2 rounded-lg border border-gray-300 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
</div>

<!-- 统计栏 -->
<div class="max-w-5xl mx-auto px-4 pb-2">
  <div class="text-xs text-gray-400" id="stats-bar">共 0 条</div>
</div>

<!-- 新闻列表 -->
<main class="max-w-5xl mx-auto px-4 pb-10">
  <div id="news-list" class="grid gap-3 grid-cols-1 md:grid-cols-2"></div>
  <div id="empty-msg" class="text-center py-16 text-gray-400 hidden">没有找到相关新闻</div>
</main>

<script>
const RAW_DATA = {news_json_str};

const SENTIMENT_LABEL = {{ positive: "🟢 利好", negative: "🔴 利空", neutral: "⚪ 中性" }};
const IMPORTANCE_LABEL = {{ breaking: "🔥 突发", important: "⭐ 重要", normal: "📄 一般" }};
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

let activeSector = "全部";
let searchKw = "";

function formatTime(iso) {{
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = n => String(n).padStart(2,"0");
  return `${{d.getMonth()+1}}/${{pad(d.getDate())}} ${{pad(d.getHours())}}:${{pad(d.getMinutes())}}`;
}}

function truncate(s, n) {{
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}}

function renderCard(item) {{
  const barClass = item.sentiment === "positive" ? "bar-positive"
    : item.sentiment === "negative" ? "bar-negative" : "bar-neutral";
  const sectorColor = SECTOR_COLORS[item.sector] || "bg-slate-100 text-slate-500";
  const titleEl = item.url
    ? `<a href="${{item.url}}" target="_blank" rel="noopener" class="font-medium text-gray-900 hover:text-blue-600 leading-snug">${{item.title}}</a>`
    : `<span class="font-medium text-gray-900 leading-snug">${{item.title}}</span>`;
  return `
  <div class="bg-white rounded-xl shadow-sm p-4 news-card hover:shadow-md transition-shadow">
    <div class="sentiment-bar ${{barClass}}"></div>
    <div class="flex-1 min-w-0">
      <div class="mb-1">${{titleEl}}</div>
      ${{item.summary ? `<p class="text-xs text-gray-500 mb-2 leading-relaxed">${{truncate(item.summary, 80)}}</p>` : ""}}
      <div class="flex flex-wrap items-center gap-1.5 text-xs text-gray-400">
        <span>${{item.source}}</span>
        <span>·</span>
        <span>${{formatTime(item.pub_time)}}</span>
        <span class="ml-auto"></span>
        <span class="px-1.5 py-0.5 rounded-full text-xs ${{sectorColor}}">${{item.sector}}</span>
        <span>${{IMPORTANCE_LABEL[item.importance] || ""}}</span>
        <span>${{SENTIMENT_LABEL[item.sentiment] || ""}}</span>
      </div>
    </div>
  </div>`;
}}

function applyFilters() {{
  const kw = searchKw.toLowerCase();
  const filtered = RAW_DATA.items.filter(item => {{
    const matchSector = activeSector === "全部" || item.sector === activeSector;
    const matchKw = !kw ||
      (item.title||"").toLowerCase().includes(kw) ||
      (item.summary||"").toLowerCase().includes(kw) ||
      (item.source||"").toLowerCase().includes(kw) ||
      (item.sector||"").toLowerCase().includes(kw);
    return matchSector && matchKw;
  }});
  const list = document.getElementById("news-list");
  const empty = document.getElementById("empty-msg");
  list.innerHTML = filtered.map(renderCard).join("");
  empty.classList.toggle("hidden", filtered.length > 0);
  document.getElementById("stats-bar").textContent =
    `共 ${{filtered.length}} 条` + (activeSector !== "全部" ? ` · ${{activeSector}}` : "") +
    (kw ? ` · 搜索: "${{searchKw}}"` : "");
}}

// 初始化
document.getElementById("update-time").textContent = "最后更新：" + (RAW_DATA.last_updated || "未知");

document.getElementById("sector-filter").addEventListener("click", e => {{
  const btn = e.target.closest(".sector-btn");
  if (!btn) return;
  document.querySelectorAll(".sector-btn").forEach(b => b.classList.remove("active"));
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


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== A股新闻聚合爬虫启动 ===")
    items = fetch_all()
    print(f"\n共抓取 {len(items)} 条（去重后）")
    payload = save_json(items)
    render_html(payload)
    print("=== 完成 ===")
