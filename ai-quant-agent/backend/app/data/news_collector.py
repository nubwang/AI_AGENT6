"""政策采集器（news_collector）

对齐 plans/03-Agent系统.md §16.2 权威采集信源清单：
  ① 顶层政策/红头文件：中国政府网、发改委、央行、工信部、财政部（RSS + 专栏爬虫）
  ② 官媒核心新闻：新闻联播文字稿（央视网）、新华网、人民网
  ③ 财经产业新闻：财联社电报、证券时报、上海证券报
  ④ 行业监管政策：证监会、能源局、科技部等部委

实现：
  - feedparser 拉取 RSS 条目（部分官方站无公开 RSS 时跳过该源，不阻断）
  - BeautifulSoup4 提取正文
  - 标题哈希 + 正文前 200 字哈希双重去重
  - SQLite 存储（结构化元数据：标题/时间/发布单位/来源分类/正文/摘要）
  - 支持从 config.POLICY_RSS_URLS 扩展信源

产出 SQLite（backend/data/policy_kb.sqlite, 表 policy_docs）供 policy_kb.py 建 RAG 索引。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time

import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from app.core.logger import logger

POLICY_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "policy_kb.sqlite",
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AI-Quant-Agent/0.1"
REQUEST_TIMEOUT = 15

# 权威信源（尽力可用的官方/权威 RSS；不可用的源采集时跳过，不阻断）
# 结构：name -> {category, rss, url(正文页回退)}
_SOURCES: list[dict] = [
    {"name": "财联社电报", "category": "财经产业", "rss": "https://www.cls.cn/nodeapi/telegraphList",
     "url": "https://www.cls.cn/telegraph", "rss_type": "json"},
    {"name": "证券时报", "category": "财经产业", "rss": "http://www.stcn.com/rss/article.xml",
     "url": "http://www.stcn.com/", "rss_type": "rss"},
    {"name": "新华网", "category": "官媒核心", "rss": "http://www.news.cn/whxw/xwrd/rss.htm",
     "url": "http://www.news.cn/", "rss_type": "html"},
    {"name": "人民网", "category": "官媒核心", "rss": "http://www.people.com.cn/rss/politics.xml",
     "url": "http://politics.people.com.cn/", "rss_type": "rss"},
    {"name": "中国政府网", "category": "顶层政策", "rss": "https://www.gov.cn/zhengce/zuixin/",
     "url": "https://www.gov.cn/zhengce/zuixin/", "rss_type": "html"},
    {"name": "中国人民银行", "category": "顶层政策", "rss": "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
     "url": "http://www.pbc.gov.cn/", "rss_type": "html"},
    {"name": "工信部", "category": "顶层政策", "rss": "https://www.miit.gov.cn/jgsj/",
     "url": "https://www.miit.gov.cn/", "rss_type": "html"},
    {"name": "证监会", "category": "行业监管", "rss": "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml",
     "url": "http://www.csrc.gov.cn/", "rss_type": "html"},
]


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(POLICY_DB), exist_ok=True)
    # WAL + busy_timeout：采集/读取并发不锁库（避免 "database is locked"）
    c = sqlite3.connect(POLICY_DB, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
    except Exception:  # noqa: BLE001
        pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS policy_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, url TEXT, source TEXT, category TEXT,
            publish_time TEXT, publisher TEXT, content TEXT, summary TEXT,
            hash TEXT UNIQUE, created_at TEXT
        )
    """)
    c.commit()
    return c


def _fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"抓取失败 {url}: {exc}")
    return None


def _extract_text(html: str) -> str:
    """抽取正文纯文本（政策正文较长，取前 6000 字避免截断影响 RAG 分块）。"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text)[:15000]
    except Exception:  # noqa: BLE001
        return ""


def _entry_items(source: dict) -> list[dict]:
    """从某信源拉取条目（RSS/JSON/HTML 尽力解析）。"""
    src_url = source.get("rss", "")
    rss_type = source.get("rss_type", "rss")
    html = _fetch(src_url)
    if not html:
        return []
    items: list[dict] = []
    if rss_type == "json":   # 财联社电报 API 类
        try:
            data = json.loads(html)
            arr = data.get("data", {}).get("roll_data", []) if isinstance(data, dict) else []
            for it in arr[:30]:
                title = str(it.get("title", "") or "")
                content = str(it.get("content", "") or "")
                items.append({
                    "title": title or content[:40], "url": source.get("url", src_url),
                    "publish_time": str(it.get("ctime", "") or ""), "content": content,
                })
        except Exception:  # noqa: BLE001
            pass
    elif rss_type == "rss":  # 标准 RSS
        try:
            feed = feedparser.parse(html)
            for e in feed.entries[:30]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or source.get("url", "")
                pub = getattr(e, "published", "") or getattr(e, "updated", "") or ""
                items.append({
                    "title": str(title), "url": str(link), "publish_time": str(pub),
                    "content": str(getattr(e, "summary", "") or "")[:800],
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"RSS 解析失败 {src_url}: {exc}")
    else:  # html 专栏页：抓标题链接列表
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True)[:30]:
            title = a.get_text(strip=True)
            href = str(a["href"])
            if len(title) < 8 or not re.search(r"(政策|通知|意见|规划|监管|改革|支持|推进)", title):
                continue
            url = href if href.startswith("http") else urljoin(src_url, href)
            items.append({
                "title": title, "url": url, "publish_time": "", "content": "",
            })
    return items


def _digest(entry: dict) -> str:
    """标题哈希 + 正文前 200 字哈希（双重去重）。"""
    title_h = hashlib.md5((entry.get("title", "") or "").encode()).hexdigest()[:16]
    body_h = hashlib.md5((entry.get("content", "") or "")[:200].encode()).hexdigest()[:16]
    return f"{title_h}_{body_h}"


def collect(max_sources: int | None = None, max_docs: int = 30) -> dict:
    """从权威信源采集政策/新闻入库（去重，正文缺失时尝试正文页提取）。"""
    sources = _SOURCES
    if max_sources:
        sources = sources[:max_sources]
    db = _conn()
    inserted, skipped, failed = 0, 0, 0
    for src in sources:
        try:
            items = _entry_items(src)
            if not items:
                logger.info(f"[news_collector] {src['name']} 无条目（源不可用/格式变化），跳过")
                failed += 1
                continue
            # 正文页抓取上限（防 html 源串行抓取过慢）
            body_fetch_limit = 8
            src_inserted = 0
            for idx, e in enumerate(items[:max_docs]):
                digest = _digest(e)
                if db.execute("SELECT 1 FROM policy_docs WHERE hash=:h", {"h": digest}).fetchone():
                    skipped += 1
                    continue
                content = e.get("content", "") or ""
                if len(content) < 50 and e.get("url") and idx < body_fetch_limit:
                    html = _fetch(e["url"])
                    if html:
                        content = _extract_text(html)
                if len(content) < 50:
                    # C：无正文条目（html 源仅标题）不入库，避免污染 RAG 语料
                    skipped += 1
                    continue
                db.execute(
                    "INSERT INTO policy_docs (title,url,source,category,publish_time,publisher,content,summary,hash,created_at) "
                    "VALUES (:t,:u,:s,:c,:p,:pub,:co,:su,:h,:ca)",
                    {"t": e.get("title", "")[:200], "u": e.get("url", ""), "s": src["name"],
                     "c": src["category"], "p": e.get("publish_time", ""),
                     "pub": src["name"], "co": content[:12000], "su": content[:200],
                     "h": digest, "ca": time.strftime("%Y-%m-%d %H:%M:%S")},
                )
                inserted += 1
                src_inserted += 1
            db.commit()
            logger.info(f"[news_collector] {src['name']} 入库 {src_inserted} 条")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[news_collector] {src['name']} 采集异常: {exc}")
            failed += 1
    total = db.execute("SELECT COUNT(*) FROM policy_docs").fetchone()[0]
    db.close()
    # 官方 RSS 多不可用（现实）：当天无新增时用 Tavily 兜底抓政策文本入库（§18.9 ① MVP）
    if inserted == 0:
        try:
            added = fetch_by_tavily()
            if added:
                db = _conn()
                total = db.execute("SELECT COUNT(*) FROM policy_docs").fetchone()[0]
                db.close()
                inserted = added
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[news_collector] Tavily 兜底抓取失败: {exc}")
    logger.info(f"[news_collector] 采集完成: 新增 {inserted}，重复 {skipped}，失败源 {failed}，累计 {total}")
    return {"inserted": inserted, "skipped": skipped, "failed": failed, "total": total}


# Tavily 兜底多 query（覆盖顶层/科技/财政/监管等，避免单 query 结果雷同）
DEFAULT_TAVILY_QUERIES = [
    "国务院 政策 通知 支持 产业",
    "发改委 产业 政策 规划 通知 意见",
    "工信部 科技 半导体 人工智能 机器人 政策",
    "财政部 央行 财政 货币 政策 支持 减税",
    "证监会 能源局 行业 监管 政策 意见 支持",
]


def fetch_by_tavily(queries: list[str] | None = None, category: str = "顶层政策",
                    max_results: int = 5) -> int:
    """官方 RSS 不可用时，用 Tavily 多 query 搜索权威政策/新闻文本入库（权威口径兜底）。"""
    from app.agents.llm_client import tavily_search
    queries = queries or DEFAULT_TAVILY_QUERIES
    db = _conn()
    n = 0
    for q in queries:
        try:
            news = tavily_search(q, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[news_collector] Tavily 查询失败 {q}: {exc}")
            continue
        for item in news or []:
            title = str(item.get("title", ""))[:200]
            content = str(item.get("content", "") or "")[:12000]
            if not title or len(content) < 30:
                continue
            digest = _digest({"title": title, "content": content})
            if db.execute("SELECT 1 FROM policy_docs WHERE hash=:h", {"h": digest}).fetchone():
                continue
            db.execute(
                "INSERT INTO policy_docs (title,url,source,category,publish_time,publisher,content,summary,hash,created_at) "
                "VALUES (:t,:u,:s,:c,:p,:pub,:co,:su,:h,:ca)",
                {"t": title, "u": str(item.get("url", "")), "s": "Tavily",
                 "c": category, "p": str(item.get("publish_date", "") or ""),
                 "pub": "Tavily权威搜索", "co": content, "su": content[:200],
                 "h": digest, "ca": time.strftime("%Y-%m-%d %H:%M:%S")},
            )
            n += 1
        db.commit()
    db.close()
    logger.info(f"[news_collector] Tavily 兜底抓取入库 {n} 条（{category}）")
    return n


def _parse_pub_dt(s) -> int:
    """publish_time → YYYYMMDD 数字（用于过期清理）。解析失败返回 0。"""
    if not s:
        return 0
    m = re.search(r"(20\d{2})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", str(s))
    if m:
        return int(f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}")
    return 0


def prune_old(keep_years: int = 5) -> int:
    """清理超过 keep_years 年的旧政策（按 publish_time 解析；无时间的不删）。返回删除数。"""
    db = _conn()
    try:
        rows = db.execute("SELECT id, publish_time FROM policy_docs").fetchall()
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - keep_years * 365 * 24 * 3600))
        rm = [rid for rid, pt in rows if (dt := _parse_pub_dt(pt)) and dt < int(cutoff)]
        if rm:
            db.executemany("DELETE FROM policy_docs WHERE id=?", [(i,) for i in rm])
            db.commit()
            logger.info(f"[news_collector] 清理 {len(rm)} 条超过 {keep_years} 年的旧政策")
        return len(rm)
    finally:
        db.close()


def list_docs(limit: int = 20) -> list[dict]:
    """列出库中政策文档（调试/检索用）。"""
    db = _conn()
    rows = db.execute(
        "SELECT title,source,category,publish_time,content FROM policy_docs ORDER BY id DESC LIMIT :n",
        {"n": limit},
    ).fetchall()
    db.close()
    return [{"title": r[0], "source": r[1], "category": r[2], "publish_time": r[3], "content": r[4]} for r in rows]


__all__ = ["collect", "fetch_by_tavily", "prune_old", "list_docs", "POLICY_DB"]
