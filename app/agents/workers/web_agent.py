"""
【模块说明】网络智能体（WebAgent）— 帮用户在互联网上找信息、抓内容

这个 Agent 具备访问互联网的能力，可以替用户从网上获取信息。

【三种工作模式】
  爬虫模式 — 给定一个网页地址（URL），自动抓取页面内容，按需提取指定信息
  嗅探模式 — 给定关键词，跨多个平台搜索并返回相关链接（支持 68 个平台）
  混合模式 — 先搜索找到目标链接，再深入抓取页面详细内容

【三级抓取降级方案】
  1. 普通 HTTP 请求（httpx）— 最快，适用大多数网站
  2. Cloudflare 绕过（cloudscraper）— 针对有 Cloudflare 防护的网站
  3. 浏览器渲染（Playwright）— 针对需要执行 JavaScript 才能加载内容的网站

网络智能体（WebAgent）— 整合网页爬虫与多平台信息嗅探。

模式一：爬虫模式 — 给定 URL，抓取页面内容并按需提取指定信息
模式二：嗅探模式 — 给定关键词，跨 68 个平台检索并返回相关 URL
模式三：混合模式 — 先嗅探定位目标 URL，再深度爬取内容

抓取层三级降级：httpx → cloudscraper（Cloudflare 绕过）→ Playwright（JS 渲染）

文件结构：
  ── 常量与辅助函数（HTTP 抓取 / HTML 解析）
  ── 平台注册表（6 类 68 个平台）
  ── 专用工具（VIS_EXCLUSIVE，仅本 Agent 可用）
      web_fetch / web_parse / web_batch_fetch
      web_search_urls / web_search_fetch
  ── WebAgent 类
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, ClassVar, Dict, List, Optional
from urllib.parse import quote, quote_plus, urljoin, urlparse

import httpx

from app.agents.base import BaseAgent
from app.agents.decorators import agent
from app.tools.base import BaseTool, EXEC_SERVER, VIS_EXCLUSIVE
from app.tools.decorators import tool

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 一、HTTP 抓取层（三级反爬降级）
# ══════════════════════════════════════════════════════════════════════════════

_AGENT_NAME = "web_agent"  # 与 @agent name 保持一致

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_BASE64_RE = re.compile(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', re.I)
_BLOCKED_PATTERNS = [
    "checking your browser", "enable javascript", "just a moment",
    "ddos-guard", "access denied", "robot or human", "are you a robot",
    "security check", "请完成验证", "滑动验证", "人机验证",
]

# 每个 user_id 维护独立的 Cookie 会话
_http_sessions: Dict[str, httpx.AsyncClient] = {}


def _browser_headers(ua: Optional[str] = None, referer: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": ua or random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


def _get_http_session(user_id: str) -> httpx.AsyncClient:
    if user_id not in _http_sessions:
        _http_sessions[user_id] = httpx.AsyncClient(
            http2=True, follow_redirects=True, timeout=30,
            headers=_browser_headers(),
        )
    return _http_sessions[user_id]


def _is_blocked(status: int, html: str) -> bool:
    if status in (403, 429, 503):
        return True
    lower = html.lower()
    return any(p in lower for p in _BLOCKED_PATTERNS)


def _clean_html(text: str, max_len: int = 200_000) -> str:
    text = _BASE64_RE.sub("[IMG]", text)
    if len(text) > max_len:
        text = text[:max_len] + f"\n[已截断，原始 {len(text)} 字符]"
    return text


# ── L1：httpx ─────────────────────────────────────────────────────────────────
async def _fetch_l1(url: str, timeout: int, user_id: str) -> tuple[int, str, str]:
    session = _get_http_session(user_id)
    parsed = urlparse(url)
    referer = f"https://www.google.com/search?q={parsed.netloc}"
    headers = _browser_headers(random.choice(_UA_POOL), referer)
    resp = await session.get(url, headers=headers, timeout=timeout)
    return resp.status_code, resp.text, resp.headers.get("content-type", "")


# ── L2：cloudscraper（Cloudflare WAF）────────────────────────────────────────
def _fetch_l2_sync(url: str, timeout: int) -> tuple[int, str, str]:
    import cloudscraper  # type: ignore
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    resp = scraper.get(url, timeout=timeout)
    return resp.status_code, resp.text, resp.headers.get("content-type", "")


# ── L3：Playwright（JS 渲染，Scrapling 不可用时的兜底）──────────────────────
async def _fetch_l3(url: str, timeout: int, wait_for: Optional[str]) -> tuple[int, str, str]:
    from playwright.async_api import async_playwright  # type: ignore
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=random.choice(_UA_POOL),
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        page = await ctx.new_page()
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=10_000)
            except Exception:
                pass
        html = await page.content()
        await browser.close()
    return 200, html, "text/html"


# ── 统一入口（三层降级：httpx → cloudscraper → Playwright）──────────────────
async def _fetch(
    url: str,
    timeout: int = 30,
    render_js: bool = False,
    wait_for: Optional[str] = None,
    user_id: str = "default",
    stealth: bool = False,
) -> Dict[str, Any]:
    """三层降级策略：
      L1: httpx         — 浏览器头 + Cookie 会话（最快）
      L2: cloudscraper  — Cloudflare WAF 绕过
      L3: Playwright    — 完整 JS 渲染

    stealth=True  → 直接从 L2 开始
    render_js=True → 直接从 L3 开始
    """
    errors: list[str] = []

    # render_js=True → 直接 L3
    if render_js:
        try:
            status, html, ct = await _fetch_l3(url, timeout, wait_for)
            return {"html": html, "status": status, "ct": ct, "strategy": "playwright", "error": None}
        except ImportError:
            return {"html": "", "status": 0, "ct": "", "strategy": "none",
                    "error": "playwright 未安装，请调用 cli_exec 执行：pip install playwright && python -m playwright install chromium，完成后重新调用工具"}
        except Exception as e:
            return {"html": "", "status": 0, "ct": "", "strategy": "none",
                    "error": f"L3 playwright 失败: {e}"}

    # stealth=True → 从 L2 开始（跳过 L1 轻量请求）
    if not stealth:
        # ── L1 ──────────────────────────────────────────────────────────────
        try:
            status, html, ct = await _fetch_l1(url, timeout, user_id)
            if not _is_blocked(status, html):
                return {"html": html, "status": status, "ct": ct,
                        "strategy": "httpx", "error": None}
            errors.append(f"L1 httpx: status={status} 反爬拦截")
        except Exception as e:
            errors.append(f"L1 httpx: {e}")

    # ── L2 ──────────────────────────────────────────────────────────────────
    try:
        status, html, ct = await asyncio.to_thread(_fetch_l2_sync, url, timeout)
        if not _is_blocked(status, html):
            return {"html": html, "status": status, "ct": ct,
                    "strategy": "cloudscraper", "error": None}
        errors.append(f"L2 cloudscraper: status={status} 仍被拦截")
    except ImportError:
        errors.append("cloudscraper 未安装，请调用 cli_exec 执行：pip install cloudscraper，完成后重新调用工具")
    except Exception as e:
        errors.append(f"L2 cloudscraper: {e}")

    # ── L3 ──────────────────────────────────────────────────────────────────
    try:
        status, html, ct = await _fetch_l3(url, timeout, wait_for)
        return {"html": html, "status": status, "ct": ct, "strategy": "playwright", "error": None}
    except ImportError:
        errors.append("playwright 未安装，请调用 cli_exec 执行：pip install playwright && python -m playwright install chromium，完成后重新调用工具")
    except Exception as e:
        errors.append(f"L3 playwright: {e}")

    return {"html": "", "status": 0, "ct": "", "strategy": "none",
            "error": "所有策略均失败: " + " | ".join(errors)}


# ══════════════════════════════════════════════════════════════════════════════
# 二、HTML 解析层（BeautifulSoup）
# ══════════════════════════════════════════════════════════════════════════════

def _parse_html(
    html: str,
    base_url: str = "",
    extract: List[str] = None,
    selector: Optional[str] = None,
    max_len: int = 50_000,
) -> Dict[str, Any]:
    from bs4 import BeautifulSoup  # type: ignore

    extract = extract or ["text"]
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "iframe", "svg", "head"]):
        tag.decompose()

    root = soup
    if selector:
        selected = soup.select(selector)
        if not selected:
            return {"error": f"选择器 '{selector}' 未匹配到任何元素"}
        from bs4 import BeautifulSoup as _BS
        root = _BS("".join(str(el) for el in selected), "lxml")

    want_all = "all" in extract
    result: Dict[str, Any] = {}

    if want_all or "text" in extract:
        lines, seen = [], set()
        for el in root.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                  "li", "td", "th", "article", "section", "blockquote", "pre", "code"]):
            t = el.get_text(separator=" ", strip=True)
            if t and len(t) > 5 and t not in seen:
                seen.add(t)
                lines.append(t)
        text = "\n".join(lines)
        if len(text) > max_len:
            text = text[:max_len] + f"\n[文本截断，原始 {len(text)} 字符]"
        result["text"] = text
        result["text_length"] = len(text)

    if want_all or "links" in extract:
        links, seen_hrefs = [], set()
        for a in root.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if base_url:
                href = urljoin(base_url, href)
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            links.append({"text": a.get_text(strip=True)[:200], "href": href})
        result["links"] = links[:500]
        result["links_count"] = len(links)

    if want_all or "tables" in extract:
        tables = []
        for table in root.find_all("table"):
            headers, rows = [], []
            thead = table.find("thead")
            if thead:
                headers = [th.get_text(strip=True) for th in thead.find_all(["th", "td"])]
            for tr in (table.find("tbody") or table).find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if not any(cells):
                    continue
                if not headers and not rows:
                    headers = cells
                    continue
                row = {headers[i] if i < len(headers) else f"col_{i}": v for i, v in enumerate(cells)}
                rows.append(row)
            if rows:
                tables.append(rows)
        result["tables"] = tables
        result["tables_count"] = len(tables)

    if want_all or "images" in extract:
        images = []
        for img in root.find_all("img"):
            src = img.get("src", "").strip()
            if not src or src.startswith("data:"):
                continue
            if base_url:
                src = urljoin(base_url, src)
            images.append({"src": src, "alt": img.get("alt", "")[:200]})
        result["images"] = images[:200]

    if want_all or "meta" in extract:
        orig = BeautifulSoup(html, "lxml")
        t = orig.find("title")
        d = orig.find("meta", attrs={"name": re.compile(r"description", re.I)})
        result["meta"] = {
            "title": t.get_text(strip=True) if t else "",
            "description": (d.get("content", "") if d else "").strip(),
        }

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 三、平台注册表（6 类 68 个平台）
# ══════════════════════════════════════════════════════════════════════════════

PLATFORMS: Dict[str, List[Dict[str, Any]]] = {
    "search_engines": [
        {"name": "DuckDuckGo HTML", "id": "duckduckgo", "lang": "multi", "enc": "plus", "js": False,
         "url": "https://html.duckduckgo.com/html/?q={q}", "note": "无需 API，最适合程序化抓取"},
        {"name": "Bing",            "id": "bing",        "lang": "multi", "enc": "plus", "js": False,
         "url": "https://www.bing.com/search?q={q}"},
        {"name": "Google",          "id": "google",      "lang": "multi", "enc": "plus", "js": True,
         "url": "https://www.google.com/search?q={q}", "note": "强反爬，建议仅获取 URL"},
        {"name": "百度",             "id": "baidu",       "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://www.baidu.com/s?wd={q}", "note": "中文首选"},
        {"name": "搜狗",             "id": "sogou",       "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://www.sogou.com/web?query={q}"},
        {"name": "360搜索",          "id": "so360",       "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://www.so.com/s?q={q}"},
        {"name": "Yandex",          "id": "yandex",      "lang": "multi", "enc": "plus", "js": False,
         "url": "https://yandex.com/search/?text={q}", "note": "俄语/多语言强"},
        {"name": "Ecosia",          "id": "ecosia",      "lang": "multi", "enc": "plus", "js": False,
         "url": "https://www.ecosia.org/search?q={q}"},
    ],
    "learning": [
        {"name": "哔哩哔哩",   "id": "bilibili",  "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://search.bilibili.com/all?keyword={q}", "note": "视频/专栏"},
        {"name": "CSDN",       "id": "csdn",      "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://so.csdn.net/so/search?q={q}&t=blog", "note": "中文技术博客"},
        {"name": "掘金",       "id": "juejin",    "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://juejin.cn/search?query={q}&type=0"},
        {"name": "简书",       "id": "jianshu",   "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://www.jianshu.com/search?q={q}"},
        {"name": "Coursera",   "id": "coursera",  "lang": "en", "enc": "pct",  "js": True,
         "url": "https://www.coursera.org/search?query={q}", "note": "在线课程"},
        {"name": "edX",        "id": "edx",       "lang": "en", "enc": "plus", "js": True,
         "url": "https://www.edx.org/search?q={q}", "note": "在线课程"},
        {"name": "Medium",     "id": "medium",    "lang": "en", "enc": "plus", "js": True,
         "url": "https://medium.com/search?q={q}"},
        {"name": "Dev.to",     "id": "devto",     "lang": "en", "enc": "plus", "js": False,
         "url": "https://dev.to/search?q={q}"},
        {"name": "GeeksforGeeks", "id": "gfg",    "lang": "en", "enc": "plus", "js": False,
         "url": "https://www.geeksforgeeks.org/search-results/?search={q}"},
        {"name": "MDN Web Docs",  "id": "mdn",    "lang": "multi", "enc": "plus", "js": False,
         "url": "https://developer.mozilla.org/zh-CN/search?q={q}", "note": "Web 标准文档"},
        {"name": "W3Schools",  "id": "w3schools", "lang": "en", "enc": "plus", "js": False,
         "url": "https://www.w3schools.com/search/search_result.php?search={q}"},
        {"name": "LeetCode",   "id": "leetcode",  "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://leetcode.cn/search/?q={q}", "note": "算法/竞赛"},
    ],
    "forums": [
        {"name": "知乎",              "id": "zhihu",    "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://www.zhihu.com/search?q={q}&type=content", "note": "中文问答/专栏"},
        {"name": "百度贴吧",          "id": "tieba",    "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://tieba.baidu.com/f/search/res?qw={q}&rn=30"},
        {"name": "Reddit",            "id": "reddit",   "lang": "en", "enc": "plus", "js": True,
         "url": "https://www.reddit.com/search/?q={q}&sort=relevance"},
        {"name": "Stack Overflow",    "id": "so",       "lang": "en", "enc": "plus", "js": False,
         "url": "https://stackoverflow.com/search?q={q}", "note": "编程问答首选"},
        {"name": "V2EX",              "id": "v2ex",     "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://www.v2ex.com/search?q={q}", "note": "技术极客社区"},
        {"name": "思否 SegmentFault", "id": "sf",       "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://segmentfault.com/search?q={q}", "note": "中文技术问答"},
        {"name": "Hacker News",       "id": "hn",       "lang": "en", "enc": "plus", "js": False,
         "url": "https://hn.algolia.com/api/v1/search?query={q}&hitsPerPage=10", "note": "JSON API，直接解析"},
        {"name": "豆瓣",              "id": "douban",   "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://www.douban.com/search?cat=1001&q={q}", "note": "书籍/影视/讨论"},
        {"name": "Stack Exchange",    "id": "se",       "lang": "en", "enc": "plus", "js": False,
         "url": "https://stackexchange.com/search?q={q}", "note": "全站搜索"},
        {"name": "电子工程世界",       "id": "eeworld",  "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://so.eeworld.com.cn/eeworld/s?q={q}", "note": "硬件/嵌入式"},
    ],
    "repositories": [
        {"name": "GitHub 仓库",    "id": "github",       "lang": "multi", "enc": "plus", "js": False,
         "url": "https://github.com/search?q={q}&type=repositories"},
        {"name": "GitHub 代码",    "id": "github_code",  "lang": "multi", "enc": "plus", "js": False,
         "url": "https://github.com/search?q={q}&type=code"},
        {"name": "GitLab",         "id": "gitlab",       "lang": "multi", "enc": "plus", "js": True,
         "url": "https://gitlab.com/search?search={q}&scope=projects"},
        {"name": "Gitee 码云",     "id": "gitee",        "lang": "zh",    "enc": "pct",  "js": True,
         "url": "https://gitee.com/explore/repos?lang=&q={q}", "note": "国内开源"},
        {"name": "PyPI",           "id": "pypi",         "lang": "multi", "enc": "plus", "js": False,
         "url": "https://pypi.org/search/?q={q}", "note": "Python 包"},
        {"name": "npm",            "id": "npm",          "lang": "multi", "enc": "plus", "js": True,
         "url": "https://www.npmjs.com/search?q={q}", "note": "JS/Node 包"},
        {"name": "Docker Hub",     "id": "docker",       "lang": "multi", "enc": "plus", "js": True,
         "url": "https://hub.docker.com/search?q={q}&type=image"},
        {"name": "Hugging Face",   "id": "hf",           "lang": "multi", "enc": "plus", "js": True,
         "url": "https://huggingface.co/models?search={q}", "note": "AI 模型/数据集"},
        {"name": "Maven Central",  "id": "maven",        "lang": "multi", "enc": "plus", "js": True,
         "url": "https://search.maven.org/search?q={q}", "note": "Java/JVM 包"},
        {"name": "Crates.io",      "id": "crates",       "lang": "multi", "enc": "plus", "js": True,
         "url": "https://crates.io/search?q={q}", "note": "Rust 包"},
        {"name": "Packagist",      "id": "packagist",    "lang": "multi", "enc": "plus", "js": True,
         "url": "https://packagist.org/?query={q}", "note": "PHP 包"},
        {"name": "Awesome Lists",  "id": "awesome",      "lang": "multi", "enc": "plus", "js": False,
         "url": "https://github.com/search?q=awesome+{q}&type=repositories", "note": "资源合集"},
    ],
    "knowledge": [
        {"name": "Wikipedia (英文)",    "id": "wiki_en",    "lang": "en",    "enc": "plus", "js": False,
         "url": "https://en.wikipedia.org/w/index.php?search={q}"},
        {"name": "维基百科（中文）",     "id": "wiki_zh",    "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://zh.wikipedia.org/w/index.php?search={q}"},
        {"name": "百度百科",             "id": "baike",      "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://baike.baidu.com/search/word?word={q}"},
        {"name": "arXiv",               "id": "arxiv",      "lang": "en",    "enc": "plus", "js": False,
         "url": "https://arxiv.org/search/?query={q}&searchtype=all", "note": "预印本/CS/物理/数学"},
        {"name": "Google Scholar",      "id": "scholar",    "lang": "multi", "enc": "plus", "js": False,
         "url": "https://scholar.google.com/scholar?q={q}", "note": "有反爬"},
        {"name": "PubMed",              "id": "pubmed",     "lang": "en",    "enc": "plus", "js": False,
         "url": "https://pubmed.ncbi.nlm.nih.gov/?term={q}", "note": "生物医学"},
        {"name": "Semantic Scholar",    "id": "s2",         "lang": "en",    "enc": "plus", "js": True,
         "url": "https://www.semanticscholar.org/search?q={q}&sort=Relevance"},
        {"name": "Internet Archive",    "id": "archive",    "lang": "multi", "enc": "plus", "js": False,
         "url": "https://archive.org/search?query={q}", "note": "网页/书籍/媒体存档"},
        {"name": "中国知网 CNKI",        "id": "cnki",       "lang": "zh",    "enc": "pct",  "js": True,
         "url": "https://www.cnki.net/kns8/defaultresult/index?crossdbcodes=CJFQ%2CCDFD&kw={q}", "note": "中文学术"},
        {"name": "万方数据",             "id": "wanfang",    "lang": "zh",    "enc": "pct",  "js": False,
         "url": "https://www.wanfangdata.com.cn/search/searchList.do?searchType=all&searchWord={q}"},
        {"name": "Read the Docs",       "id": "rtd",        "lang": "multi", "enc": "plus", "js": False,
         "url": "https://readthedocs.org/search/?q={q}", "note": "开源文档"},
        {"name": "RFC Editor",          "id": "rfc",        "lang": "en",    "enc": "plus", "js": False,
         "url": "https://www.rfc-editor.org/search/rfc_search_detail.php?title={q}", "note": "互联网标准"},
        {"name": "中国标准全文公开",     "id": "std_cn",     "lang": "zh",    "enc": "pct",  "js": True,
         "url": "https://openstd.samr.gov.cn/bzgk/gb/std_list?p.p1={q}", "note": "国家标准 GB"},
        {"name": "Stack Exchange (全站)", "id": "se_docs",   "lang": "en",    "enc": "plus", "js": False,
         "url": "https://stackexchange.com/search?q={q}"},
    ],
    "government": [
        {"name": "中国政府网",     "id": "gov_cn",    "lang": "zh", "enc": "pct",  "js": False,
         "url": "http://sousuo.gov.cn/s.htm?t=paper&q={q}", "note": "国务院政策文件"},
        {"name": "国家统计局",     "id": "stats_cn",  "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://search.stats.gov.cn/search.htm?searchword={q}", "note": "统计数据/公报"},
        {"name": "工信部",         "id": "miit",      "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://www.miit.gov.cn/search/index.html?q={q}"},
        {"name": "国家发展改革委", "id": "ndrc",      "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://so.ndrc.gov.cn/s?wd={q}&siteCode=bm13000001"},
        {"name": "国家市场监管总局", "id": "samr",    "lang": "zh", "enc": "pct",  "js": False,
         "url": "https://search.samr.gov.cn/search.html?q={q}", "note": "市场监管/标准/法规"},
        {"name": "中国裁判文书网", "id": "court",     "lang": "zh", "enc": "pct",  "js": True,
         "url": "https://wenshu.court.gov.cn/website/wenshu/181107ANFZ0BXSK4/index.html?txt={q}"},
        {"name": "美国政府 USA.gov", "id": "usa_gov", "lang": "en", "enc": "plus", "js": False,
         "url": "https://search.usa.gov/search?affiliate=usagov&query={q}"},
        {"name": "联合国 UN.org",  "id": "un",        "lang": "multi", "enc": "plus", "js": False,
         "url": "https://search.un.org/results.aspx?q={q}&lang=zh"},
        {"name": "欧盟 Europa.eu", "id": "eu",        "lang": "multi", "enc": "plus", "js": True,
         "url": "https://european-union.europa.eu/search?q={q}"},
        {"name": "WHO",            "id": "who",       "lang": "en",    "enc": "pct",  "js": False,
         "url": "https://www.who.int/health-topics/{q}", "note": "全球卫生"},
        {"name": "世界银行",       "id": "wb",        "lang": "en",    "enc": "plus", "js": True,
         "url": "https://www.worldbank.org/en/search?q={q}", "note": "经济数据/报告"},
        {"name": "IMF",            "id": "imf",       "lang": "en",    "enc": "plus", "js": True,
         "url": "https://www.imf.org/en/Search#q={q}&sort=relevancy", "note": "金融政策报告"},
    ],
}

_ALL_CATS = list(PLATFORMS.keys())
_BY_ID: Dict[str, Dict[str, Any]] = {
    p["id"]: {**p, "category": cat}
    for cat, ps in PLATFORMS.items()
    for p in ps
}

# 精准结果提取选择器（container → link → title → snippet）
_SELECTORS: Dict[str, Dict[str, Optional[str]]] = {
    "duckduckgo": {"c": ".result",            "l": ".result__a",                "t": ".result__a",          "s": ".result__snippet"},
    "bing":       {"c": "#b_results .b_algo", "l": "h2 a",                      "t": "h2 a",                "s": ".b_caption p"},
    "baidu":      {"c": ".c-container",       "l": "h3.t a, h3 a",              "t": "h3.t a, h3 a",        "s": ".c-abstract"},
    "sogou":      {"c": ".vrwrap, .rb",        "l": "h3 a",                      "t": "h3 a",                "s": ".str_info"},
    "so360":      {"c": ".res-list",           "l": "h3 a",                      "t": "h3 a",                "s": ".res-desc"},
    "so":         {"c": ".s-post-summary",     "l": ".s-link",                   "t": ".s-link",             "s": ".s-post-summary--content-excerpt"},
    "github":     {"c": ".search-title",       "l": "a",                         "t": "a",                   "s": None},
    "wiki_en":    {"c": ".mw-search-result",   "l": ".mw-search-result-heading a","t": ".mw-search-result-heading a","s": ".searchresult"},
    "wiki_zh":    {"c": ".mw-search-result",   "l": ".mw-search-result-heading a","t": ".mw-search-result-heading a","s": ".searchresult"},
    "arxiv":      {"c": "li.arxiv-result",     "l": ".title a",                  "t": ".title a",            "s": ".abstract-short"},
    "pubmed":     {"c": ".docsum-content",     "l": ".docsum-title",             "t": ".docsum-title",       "s": ".full-authors"},
    "pypi":       {"c": ".package-snippet",    "l": "a.package-snippet",         "t": ".package-snippet__name","s": ".package-snippet__description"},
    "sf":         {"c": ".search-result-item", "l": ".result-title a",           "t": ".result-title a",     "s": ".result-summary"},
    "tieba":      {"c": ".s_post",             "l": ".p_title a",                "t": ".p_title a",          "s": ".p_content"},
    "v2ex":       {"c": ".cell.item",          "l": "span.item_title a",         "t": "span.item_title a",   "s": None},
    "baike":      {"c": ".search-list dd",     "l": ".result-title a",           "t": ".result-title a",     "s": ".result-summary"},
}


def _encode(query: str, enc: str) -> str:
    return quote_plus(query) if enc == "plus" else quote(query, safe="")


def _build_url(p: Dict[str, Any], query: str) -> str:
    return p["url"].replace("{q}", _encode(query, p.get("enc", "plus")))


def _get_host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_result_link(href: str, search_host: str, base_url: str = "") -> bool:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return False
    if href.startswith("/") and base_url:
        href = urljoin(base_url, href)
    if not href.startswith(("http://", "https://")):
        return False
    host = _get_host(href)
    if search_host and (search_host in host or host in search_host):
        return False
    parsed = urlparse(href)
    if parsed.path in ("", "/"):
        return False
    skip = ["/login", "/register", "/signup", "/privacy", "/terms",
            "/help", "/about", "/contact", "/sitemap"]
    if any(k in parsed.path.lower() for k in skip):
        return False
    return True


def _extract_results(html: str, pid: str, base_url: str, max_n: int = 15) -> List[Dict[str, str]]:
    from bs4 import BeautifulSoup  # type: ignore

    host = _get_host(base_url)
    sel = _SELECTORS.get(pid, {})
    results: List[Dict[str, str]] = []
    soup = BeautifulSoup(html, "lxml")

    if sel and sel.get("c") and sel.get("l"):
        for c in soup.select(sel["c"])[:max_n]:
            link_el = c.select_one(sel["l"])
            if not link_el:
                continue
            href = link_el.get("href", "")
            if href.startswith("/"):
                href = urljoin(base_url, href)
            if not _is_result_link(href, host):
                for attr in ("data-href", "data-url", "data-orig-href"):
                    real = link_el.get(attr, "")
                    if real and real.startswith("http"):
                        href = real
                        break
            title_el = c.select_one(sel.get("t") or sel["l"])
            snip_el  = c.select_one(sel["s"]) if sel.get("s") else None
            results.append({
                "title":   (title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True))[:300],
                "url":     href,
                "snippet": (snip_el.get_text(strip=True) if snip_el else "")[:300],
            })
        if results:
            return results

    seen: set = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = urljoin(base_url, href)
        if not _is_result_link(href, host) or href in seen:
            continue
        seen.add(href)
        results.append({"title": a.get_text(strip=True)[:300], "url": href, "snippet": ""})
        if len(results) >= max_n:
            break
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 四、专用工具（VIS_EXCLUSIVE，仅 web_agent 可用）
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="web_fetch",
    description=(
        "抓取指定 URL 的网页 HTML，内置三层反爬降级：httpx → cloudscraper → Playwright。\n"
        "stealth=true 跳过 httpx 直接使用 cloudscraper（Cloudflare/WAF 绕过）；\n"
        "render_js=true 直接使用 Playwright 完整 JS 渲染。\n"
        "返回原始 HTML，配合 web_parse 工具使用。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent=_AGENT_NAME,
    dangerous_ops=["network"],
    parameters={
        "url":       {"type": "string",  "description": "目标 URL（需含 http:// 或 https://）", "required": True},
        "stealth":   {"type": "boolean", "description": "启用 Chromium 隐身模式（绕过 Cloudflare/WAF），默认 false", "default": False},
        "render_js": {"type": "boolean", "description": "是否用完整 Playwright 渲染 JS（SPA），默认 false", "default": False},
        "wait_for":  {"type": "string",  "description": "等待该 CSS 选择器出现后再提取（配合 render_js/stealth）", "required": False},
        "timeout":   {"type": "integer", "description": "超时秒数，默认 30", "default": 30},
        "delay":     {"type": "number",  "description": "请求前随机延迟上限（秒），默认 0", "default": 0},
    },
)
class WebFetchTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        url       = params.get("url", "").strip()
        stealth   = bool(params.get("stealth", False))
        render_js = bool(params.get("render_js", False))
        wait_for  = params.get("wait_for") or None
        timeout   = int(params.get("timeout", 30))
        delay     = float(params.get("delay", 0))
        user_id   = context.get("user_id", "default")

        if not url:
            return {"success": False, "error": "url 不能为空"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if delay > 0:
            await asyncio.sleep(random.uniform(0, delay))

        res = await _fetch(url, timeout=timeout, render_js=render_js,
                           wait_for=wait_for, user_id=user_id, stealth=stealth)
        if res["error"]:
            return {"success": False, "error": res["error"], "html": "", "url": url, "strategy": res["strategy"]}
        html = _clean_html(res["html"])
        return {
            "success": True, "html": html, "url": url,
            "status_code": res["status"], "content_type": res["ct"],
            "strategy": res["strategy"], "html_length": len(html),
        }


@tool(
    name="web_parse",
    description=(
        "解析 HTML 字符串，提取结构化数据（纯文本 / 链接 / 表格 / 图片 / meta）。\n"
        "extract 可选：text / links / tables / images / meta / all。\n"
        "用 selector（CSS 选择器）缩小提取范围效果更精准。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent=_AGENT_NAME,
    parameters={
        "html":       {"type": "string", "description": "待解析的 HTML 字符串", "required": True},
        "base_url":   {"type": "string", "description": "基础 URL，用于解析相对链接", "required": False},
        "extract":    {"type": "array",  "items": {"type": "string"},
                       "description": "提取类型列表：text/links/tables/images/meta/all，默认 [\"text\"]",
                       "default": ["text"]},
        "selector":   {"type": "string", "description": "CSS 选择器，限定提取范围", "required": False},
        "max_length": {"type": "integer","description": "文本最大字符数，默认 50000", "default": 50000},
    },
)
class WebParseTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        html      = params.get("html", "")
        base_url  = params.get("base_url", "")
        extract   = params.get("extract") or ["text"]
        selector  = params.get("selector") or None
        max_len   = int(params.get("max_length", 50_000))
        if not html:
            return {"success": False, "error": "html 不能为空"}
        try:
            parsed = await asyncio.to_thread(_parse_html, html, base_url, extract, selector, max_len)
            return {"success": True, **parsed}
        except ImportError:
            return {"success": False, "error": "请安装：pip install beautifulsoup4 lxml"}
        except Exception as e:
            return {"success": False, "error": str(e)}


@tool(
    name="web_batch_fetch",
    description=(
        "并发批量抓取多个 URL（最多 10 个），每个 URL 独立应用三层反爬降级策略。\n"
        "parse_text=true 时同步解析每页纯文本，省去手动调用 web_parse。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent=_AGENT_NAME,
    dangerous_ops=["network"],
    parameters={
        "urls":       {"type": "array", "items": {"type": "string"}, "description": "URL 列表（最多 10 个）", "required": True},
        "render_js":  {"type": "boolean", "description": "是否对所有 URL 启用 JS 渲染，默认 false", "default": False},
        "timeout":    {"type": "integer", "description": "单个请求超时秒数，默认 30", "default": 30},
        "delay":      {"type": "number",  "description": "每个请求间随机延迟上限（秒），默认 1", "default": 1},
        "parse_text": {"type": "boolean", "description": "是否同步解析每页纯文本，默认 false", "default": False},
        "stealth":    {"type": "boolean", "description": "对所有 URL 启用 Chromium 隐身模式，默认 false", "default": False},
    },
)
class WebBatchFetchTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        urls       = params.get("urls", [])
        render_js  = bool(params.get("render_js", False))
        stealth    = bool(params.get("stealth", False))
        timeout    = int(params.get("timeout", 30))
        delay      = float(params.get("delay", 1))
        parse_text = bool(params.get("parse_text", False))
        user_id    = context.get("user_id", "default")

        if not urls or not isinstance(urls, list):
            return {"success": False, "error": "urls 必须是非空列表"}
        urls = [u.strip() if u.startswith(("http://", "https://")) else f"https://{u.strip()}"
                for u in urls[:10]]

        async def _one(url: str, idx: int) -> Dict[str, Any]:
            if delay > 0 and idx > 0:
                await asyncio.sleep(random.uniform(0.2, delay))
            res = await _fetch(url, timeout=timeout, render_js=render_js,
                               user_id=user_id, stealth=stealth)
            item: Dict[str, Any] = {
                "url": url, "success": not bool(res["error"]),
                "status_code": res["status"], "strategy": res["strategy"],
                "html_length": len(res["html"]), "error": res["error"],
            }
            if parse_text and res["html"]:
                try:
                    p = await asyncio.to_thread(_parse_html, res["html"], url, ["text", "meta"], None, 30_000)
                    item["text"] = p.get("text", "")
                    item["meta"] = p.get("meta", {})
                except Exception as e:
                    item["text"] = ""
                    item["parse_error"] = str(e)
            else:
                item["html"] = _clean_html(res["html"], 100_000) if res["html"] else ""
            return item

        items = await asyncio.gather(*[_one(u, i) for i, u in enumerate(urls)], return_exceptions=True)
        items = [r if not isinstance(r, Exception) else {"success": False, "error": str(r)} for r in items]
        ok = sum(1 for r in items if r.get("success"))
        return {"success": True, "total": len(items), "success_count": ok, "fail_count": len(items) - ok, "results": items}


@tool(
    name="web_search_urls",
    description=(
        "根据查询词生成各平台搜索 URL（纯本地计算，无网络请求，即时返回）。\n"
        "覆盖 6 大类 68 个平台：搜索引擎 / 学习平台 / 论坛社区 / 代码仓库 / 知识库 / 政府网站。\n"
        "返回的 URL 可提交给 web_search_fetch 抓取结果，或直接提供给用户访问。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent=_AGENT_NAME,
    parameters={
        "query":            {"type": "string", "description": "搜索查询词", "required": True},
        "categories":       {"type": "array",  "items": {"type": "string"},
                             "description": "限定分类：search_engines/learning/forums/repositories/knowledge/government，默认全部",
                             "required": False},
        "platform_ids":     {"type": "array",  "items": {"type": "string"},
                             "description": "指定平台 ID 列表，如 ['github','pypi','so']，优先级高于 categories",
                             "required": False},
        "include_js_heavy": {"type": "boolean","description": "是否包含重度 JS 平台（会标注 js=true），默认 true", "default": True},
    },
)
class WebSearchUrlsTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        query        = params.get("query", "").strip()
        categories   = params.get("categories") or _ALL_CATS
        platform_ids = params.get("platform_ids") or []
        inc_js       = bool(params.get("include_js_heavy", True))

        if not query:
            return {"success": False, "error": "query 不能为空"}

        valid_cats = [c for c in categories if c in PLATFORMS] or _ALL_CATS
        out: Dict[str, List[Dict]] = {}
        total = 0

        if platform_ids:
            items = []
            for pid in platform_ids:
                p = _BY_ID.get(pid)
                if not p or (not inc_js and p.get("js")):
                    continue
                items.append({"name": p["name"], "id": pid, "url": _build_url(p, query),
                               "lang": p["lang"], "js": p.get("js", False), "note": p.get("note", "")})
            out["specified"] = items
            total = len(items)
        else:
            for cat in valid_cats:
                items = []
                for p in PLATFORMS.get(cat, []):
                    if not inc_js and p.get("js"):
                        continue
                    items.append({"name": p["name"], "id": p["id"], "url": _build_url(p, query),
                                  "lang": p["lang"], "js": p.get("js", False), "note": p.get("note", "")})
                if items:
                    out[cat] = items
                    total += len(items)

        return {
            "success": True, "query": query, "total_urls": total, "search_urls": out,
            "tip": "将 url 传给 web_search_fetch 可获取实际结果；js=true 的平台建议 render_js=true",
        }


@tool(
    name="web_search_fetch",
    description=(
        "抓取搜索结果页面，提取结果链接列表（标题 + URL + 摘要）。\n"
        "内置精准解析器覆盖：DuckDuckGo / Bing / 百度 / GitHub / Stack Overflow / "
        "arXiv / Wikipedia / PyPI / 知乎 / 贴吧 / V2EX / 思否 等平台；"
        "其余平台使用通用链接提取；Hacker News 走 JSON API 直接解析。\n"
        "配合 web_search_urls：先生成搜索 URL，再用本工具抓取实际结果。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent=_AGENT_NAME,
    dangerous_ops=["network"],
    parameters={
        "url":         {"type": "string",  "description": "搜索结果页 URL（由 web_search_urls 生成）", "required": True},
        "platform_id": {"type": "string",  "description": "平台 ID，不填则按 URL 自动推断", "required": False},
        "max_results": {"type": "integer", "description": "最多返回条数，默认 15", "default": 15},
        "render_js":   {"type": "boolean", "description": "是否 JS 渲染（js=true 的平台需要），默认 false", "default": False},
        "timeout":     {"type": "integer", "description": "超时秒数，默认 20", "default": 20},
    },
)
class WebSearchFetchTool(BaseTool):
    # URL host → platform_id 的快速映射表
    _HOST_MAP: ClassVar[Dict[str, str]] = {
        "html.duckduckgo.com": "duckduckgo",
        "www.bing.com":        "bing",
        "www.baidu.com":       "baidu",
        "www.sogou.com":       "sogou",
        "www.so.com":          "so360",
        "stackoverflow.com":   "so",
        "github.com":          "github",
        "arxiv.org":           "arxiv",
        "en.wikipedia.org":    "wiki_en",
        "zh.wikipedia.org":    "wiki_zh",
        "pypi.org":            "pypi",
        "hn.algolia.com":      "hn",
        "segmentfault.com":    "sf",
        "tieba.baidu.com":     "tieba",
        "www.v2ex.com":        "v2ex",
        "baike.baidu.com":     "baike",
    }

    async def execute(self, params: dict, context: dict) -> dict:
        url         = params.get("url", "").strip()
        platform_id = params.get("platform_id") or ""
        max_results = int(params.get("max_results", 15))
        render_js   = bool(params.get("render_js", False))
        timeout     = int(params.get("timeout", 20))
        user_id     = context.get("user_id", "default")

        if not url:
            return {"success": False, "error": "url 不能为空", "results": []}

        # 自动推断 platform_id
        if not platform_id:
            host = _get_host(url)
            platform_id = self._HOST_MAP.get(host, "")
            if not platform_id:
                for h, pid in self._HOST_MAP.items():
                    if h in host:
                        platform_id = pid
                        break

        # Hacker News JSON API
        if platform_id == "hn" or "hn.algolia.com/api" in url:
            res = await _fetch(url, timeout=timeout, user_id=user_id)
            if res["error"]:
                return {"success": False, "error": res["error"], "results": []}
            try:
                data = json.loads(res["html"])
                results = [
                    {"title": h.get("title", ""), "snippet": (h.get("story_text") or "")[:200],
                     "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"}
                    for h in data.get("hits", [])[:max_results]
                ]
                return {"success": True, "platform_id": "hn", "url": url, "count": len(results), "results": results}
            except Exception as e:
                return {"success": False, "error": f"HN JSON 解析失败: {e}", "results": []}

        # 常规 HTML 抓取
        res = await _fetch(url, timeout=timeout, render_js=render_js, user_id=user_id)
        if res["error"]:
            return {"success": False, "error": res["error"], "platform_id": platform_id, "results": []}
        if not res["html"]:
            return {"success": False, "error": "抓取到空页面，可能需要 render_js=true",
                    "platform_id": platform_id, "hint": "设置 render_js=true 重试", "results": []}

        try:
            results = await asyncio.to_thread(
                _extract_results, res["html"], platform_id or "generic", url, max_results
            )
        except ImportError:
            return {"success": False, "error": "请安装：pip install beautifulsoup4 lxml", "results": []}
        except Exception as e:
            return {"success": False, "error": str(e), "results": []}

        hint = ""
        if len(results) < 3 and len(res["html"]) < 5000 and not render_js:
            hint = "结果过少，建议 render_js=true 重试"

        return {
            "success": True, "platform_id": platform_id or "generic",
            "url": url, "strategy": res["strategy"],
            "count": len(results), "results": results, "hint": hint,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 五、WebAgent（合并爬虫 + 嗅探）
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM = """你是一名网络情报专家，集**网页爬虫**（内容抓取）和**网络嗅探**（多平台检索）能力于一体。
底层抓取层内置三层反爬降级：httpx（浏览器头伪装）→ cloudscraper（Cloudflare WAF 绕过）→ Playwright（完整 JS 渲染）。

## 专用工具

| 工具 | 用途 |
|------|------|
| `web_fetch` | 抓取单个 URL 的 HTML，三层反爬降级（httpx → cloudscraper → Playwright） |
| `web_parse` | 解析 HTML，提取纯文本/链接/表格/图片/meta |
| `web_batch_fetch` | 并发批量抓取最多 10 个 URL |
| `web_search_urls` | ⚠️ 仅本地生成各平台搜索 URL 模板，**不发出任何网络请求** |
| `web_search_fetch` | 抓取搜索结果页，提取结果链接列表（有网络请求） |

> **重要：`web_search_urls` 只是 URL 生成器，本身无法获取任何网页内容。**
> 调用它后**必须**紧接着调用 `web_search_fetch` 才能得到真实搜索结果。
> 单独调用 `web_search_urls` 后即结束任务是**错误行为**。

## 工作模式选择

根据任务描述判断模式（按优先级）：

**爬虫模式** — 任务包含明确 URL（含域名如 weather.com、example.com），目标是提取该页内容
```
web_fetch(url, render_js=True/False) → web_parse → 整理输出
```

**混合模式** — 任务要求获取实时/动态内容（天气、新闻、股票、价格等），或有域名但内容需先搜索定位
```
web_search_urls(query, categories=["search_engines"]) →
web_search_fetch(duckduckgo_url 或 baidu_url) →
web_fetch(result_url, render_js=按需) + web_parse → 整理输出
```

**嗅探模式** — 任务是找某类信息或资源，返回相关链接列表即满足需求
```
web_search_urls → web_search_fetch(3-5个平台) → 汇总结果链接
```

> 提示：天气/新闻/价格等实时查询推荐**混合模式**：先搜索找到可抓取页面，再抓取具体内容。

## 爬虫执行策略

### stealth / render_js 参数选择

| 场景 | 推荐参数 |
|------|---------|
| 普通静态页面 | 默认（最快，httpx） |
| 被 Cloudflare/WAF 拦截（strategy=httpx 但内容异常） | `stealth=true` |
| React/Vue/Angular SPA，内容需 JS 渲染（weather.com 等） | `render_js=true` |
| SPA + 等待特定元素加载 | `render_js=true` + `wait_for="CSS选择器"` |

### 解析策略（使用 web_parse 时）
- 提取全文 → extract=["text"]
- 提取表格数据 → extract=["tables"]
- 全量 → extract=["all"]
- 精准定位 → selector="main"、"#content"、".product-list" 等

### 反爬处理
- 查看 strategy 字段（httpx / cloudscraper / playwright）
- strategy=httpx 但内容被拦截 → 重试时传 stealth=true
- 频繁请求同一域名设置 delay=1~3

## 嗅探/混合模式完整执行步骤

1. `web_search_urls(query=..., categories=["search_engines"])` → 得到平台 URL 列表
2. `web_search_fetch(url=duckduckgo搜索URL)` → 得到搜索结果（标题+链接）
3. （混合模式）从结果中选 1-3 个最相关链接，`web_fetch(url=...) + web_parse` → 得到页面内容
4. 整理并输出实际内容

### 平台选择指引
| 查询类型 | 优先平台 ID |
|----------|-------------|
| 实时天气/新闻 | duckduckgo, baidu（搜索结果再抓取） |
| 编程/代码 | duckduckgo, so, github, sf |
| 中文内容 | baidu, zhihu, csdn, tieba |
| 学术论文 | arxiv, pubmed, scholar |
| 技术文档 | mdn, rtd, wiki_en |

- DuckDuckGo HTML（id: duckduckgo）几乎必选，无需 Key，易解析
- js=true 的平台结果少（<3条）时跳过，不强制 render_js

## 依赖缺失自动修复

当工具返回含 "未安装" 或 "请调用 cli_exec" 的错误时，**不要告知用户**：
1. `cli_exec` 安装缺失包（`pip install cloudscraper` / `pip install playwright && python -m playwright install chromium`）
2. 等待成功后**重新调用**原工具（相同参数）

## 文件保存规则

- `file_writer` 只支持沙箱路径，不支持绝对路径
- 保存到系统路径时用 `cli_exec`：`cat > /path/to/file << 'EOF'\n...\nEOF`
- 先用 `cli_exec` 确认目标目录存在，再写入

## 重要行为准则

- **严禁**在未获取到真实内容前声称任务完成
- `web_search_urls` 的返回值是 URL 模板，**不是内容**；必须继续调用 `web_search_fetch` 才能获取内容
- 只有工具返回 `"success": true` 且 result/text 字段包含有效内容后，才能输出结论
- 所有工具失败时，如实告知用户失败原因
- 工具调用**串行**进行：先获取数据再保存，不得并行"获取+保存"
"""


@agent(
    name=_AGENT_NAME,
    role="网络情报专家",
    background=_SYSTEM,
)
class WebAgent(BaseAgent):
    """网络智能体，整合爬虫与多平台嗅探能力。

    爬虫模式：给定 URL，抓取并提取页面信息。
    嗅探模式：给定关键词，跨 68 个平台检索相关 URL。
    混合模式：先嗅探定位，再深度抓取。
    """

    _L2_DECOMPOSE_THRESHOLD: int = 300

    async def execute(self, task: dict, context: dict, llm) -> dict:
        await self.load_skills()
        lc_tools = self.collect_tools(context.get("user_id", self.user_id))
        system_prompt = self._build_system_prompt(context=context)
        description = task.get("description", "").strip()

        if not description:
            return {
                "result":  "请描述任务：提供 URL（爬虫模式）或关键词（嗅探模式），也可以同时提供。",
                "success": False,
                "metadata": {"agent": self.name},
            }

        history_lines = []
        for turn in (context.get("history", []) if isinstance(context, dict) else [])[-2:]:
            if isinstance(turn, dict):
                u = turn.get("user_input", turn.get("human", ""))
                a = turn.get("assistant_response", turn.get("ai", ""))
                if u:
                    history_lines.append(f"用户: {u}")
                if a:
                    history_lines.append(f"助手: {a[:400]}")

        human = description
        if history_lines:
            human = "上下文：\n" + "\n".join(history_lines) + "\n\n任务：\n" + description
        human += "\n\n请先判断工作模式（爬虫/嗅探/混合），再按策略逐步执行，最后整理输出。"

        try:
            result_text = await self._invoke_with_tools(
                llm, system_prompt, human, lc_tools,
                user_id=context.get("user_id", self.user_id),
            )
            await self.update_skill(description, result_text, success=True)
            return {
                "result":  result_text,
                "success": True,
                "metadata": {"agent": self.name, "tools_used": len(lc_tools)},
            }
        except Exception as e:
            logger.error("[WebAgent] 执行异常: %s", e)
            return {"result": f"执行失败：{e}", "success": False, "metadata": {"agent": self.name}}
