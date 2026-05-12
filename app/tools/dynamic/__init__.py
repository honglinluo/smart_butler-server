"""
【目录说明】动态工具目录（dynamic/）— AI 在运行时自动创建的工具存放处

这个目录下的 .py 文件不是开发者手写的，而是由 AI（AgentEventLoop 的 ToolBuilder）
在服务运行过程中按需自动生成的。

【生成时机】
  当 Agent 遇到没有合适工具的任务，并且用户批准了工具创建请求后，
  系统会让 CodeAssistant 生成工具代码，写入到这个目录，立即注册可用。

【文件命名】
  每个工具文件名对应工具名，如 web_content_summary.py 对应工具 web_content_summary

【注意】这个目录的内容会随系统运行动态增减，不建议手动修改。
"""
# 此目录下的 .py 文件由 AgentEventLoop 的 ToolBuilder 在运行时生成
