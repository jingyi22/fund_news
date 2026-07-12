# A股财经资讯聚合

自动抓取 A 股相关新闻，通过 GitHub Actions 每 30 分钟更新，GitHub Pages 静态托管，无服务器成本。

**在线访问：** https://jingyi22.github.io/fund_news/

---

## 功能特性

- **多数据源**：新华社、财联社、证券时报、路透中文、东方财富快讯
- **自动分类**：按商业航天、半导体、AI/科技、新能源等 10 大板块分类
- **情感标注**：🟢 利好 / 🔴 利空 / ⚪ 中性（关键词匹配）
- **重要程度**：🔥 突发 / ⭐ 重要 / 📄 一般
- **板块筛选 + 关键词搜索**
- **移动端适配**，手机随时可查

---

## 部署步骤

### 1. 创建仓库并启用 GitHub Pages

1. 进入仓库 → **Settings** → **Pages**
2. Source 选 **Deploy from a branch**
3. Branch 选 `main`，目录选 `/docs`
4. 点击 **Save**

### 2. 首次本地运行（可选，生成初始数据）

```bash
pip install feedparser requests akshare
python scripts/fetch_news.py
```

生成的 `data/news.json` 和 `docs/index.html` 提交到仓库后，Pages 立即可访问。

### 3. Push 到仓库

```bash
git init
git remote add origin https://github.com/jingyi22/fund_news.git
git add .
git commit -m "init: A股新闻聚合"
git branch -M main
git push -u origin main
```

### 4. 启用 Actions 自动化

Push 完成后，进入仓库 → **Actions** 标签页，确认 workflow 已启用（绿色）。

之后每个交易日 09:00–17:30，每 30 分钟自动抓取并更新页面。

---

## 目录结构

```
stock-news-app/
├── .github/workflows/update.yml   # Actions 定时任务
├── scripts/fetch_news.py          # 爬虫脚本
├── data/news.json                 # 新闻数据（自动更新）
├── docs/index.html                # 静态页面（自动生成）
└── README.md
```

---

## 手动触发更新

进入仓库 → **Actions** → **Update News** → **Run workflow**

---

## 新增数据源

在 `scripts/fetch_news.py` 的 `RSS_SOURCES` 列表中添加：

```python
{
    "name": "来源名称",
    "url": "https://example.com/rss.xml",
    "timeout": 10,
},
```

---

## License

MIT
