---
name: web_agent 技能
description: 用于处理网络情报任务，包括网页抓取和内容总结。
created_at: 2026-05-11
version: "1.1"
---

## 描述

该技能用于访问指定网址、抓取页面内容并进行总结。支持三层反爬降级（httpx → cloudscraper → Playwright）。

## 触发条件

- 处理 web_agent 类型任务时（含网页爬取、内容抓取、信息检索）

## 执行步骤

1. 判断工作模式：
   - 有明确 URL/域名 → 爬虫模式：`web_fetch` → `web_parse`
   - 实时内容（天气/新闻等）或无具体 URL → 混合模式：`web_search_urls` → `web_search_fetch` → `web_fetch` + `web_parse`
   - 仅需返回链接列表 → 嗅探模式：`web_search_urls` → `web_search_fetch` → 汇总
2. **注意**：`web_search_urls` 只生成 URL 模板，无网络请求；必须继续调用 `web_search_fetch` 获取真实结果
3. 批量多页时使用 `web_batch_fetch`（parse_text=true）
4. 被反爬拦截时：先尝试 stealth=true，再尝试 render_js=true（JS-heavy 站点如 weather.com 直接用 render_js=true）
5. 将结果交给后续 summarizer / general_assistant 处理或保存

## 文件保存规则

使用 `file_writer` 写入沙箱路径，确认文件没有问题后调用 `cli_exec` 拷贝到指定路径

## 依赖缺失处理

工具返回 "未安装" 或 "请调用 cli_exec" 错误时，直接：

1. `cli_exec` 执行 `pip install cloudscraper` 或 `pip install playwright && python -m playwright install chromium`
2. 安装成功后**重新调用**原工具（相同参数）

## 注意事项

- 底层抓取层：httpx（浏览器头伪装）→ cloudscraper（Cloudflare WAF 绕过）→ Playwright（完整 JS 渲染）
- 工具调用必须串行：先获取内容，获取成功后再保存，不得并行调用"获取+保存"
