---
name: web_agent 技能
description: 用于处理网络情报任务，包括网页抓取和内容总结。
created_at: 2026-05-11
version: "1.0"
---

## 描述
该技能用于访问指定的网址并总结其主要内容。适用于需要从网页中提取信息的任务。

## 触发条件
- 处理 web_agent 类型任务时

## 前置条件
- 无

## 执行步骤
1. 访问网址 https://www.gov.cn/zhuanti/2026nztj/2026qglh/yw/202603/content_7060476.htm ，并总结网页中的内容。
2. 爬取并总结指定网页的主要内容。
3. 抓取指定网页的HTML内容。

## 示例
```json
{
  "url": "https://www.gov.cn/zhuanti/2026nztj/2026qglh/yw/202603/content_7060476.htm"
}
```

## 注意事项
- 如果需要使用专用工具，请在回复中返回 JSON 格式的工具请求，系统将自动为你构建并重新调用你。
- 本技能已集成 Scrapling 高性能爬虫框架，具备自适应元素定位、Cloudflare 自动绕过等能力。