"""
API 测试脚本 - 演示如何使用 Hermes Multi-Agent System
"""

import requests
import json
from typing import Dict, Any


# API 基础 URL
BASE_URL = "http://localhost:8000"


def test_health_check():
    """测试健康检查端点"""
    print("\n" + "="*60)
    print("测试 1: 健康检查")
    print("="*60)
    
    response = requests.get(f"{BASE_URL}/health")
    print(f"状态码: {response.status_code}")
    print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.status_code == 200


def test_chat_with_bearer_token(message: str, token: str = "my_secret_token_123") -> Dict[str, Any]:
    """
    测试聊天接口 - 使用 Bearer Token (推荐方式)
    
    Args:
        message: 用户消息
        token: 认证令牌
        
    Returns:
        dict: API 响应
    """
    print("\n" + "="*60)
    print(f"测试 2: 聊天接口 (Bearer Token 方式)")
    print("="*60)
    print(f"消息: {message}")
    print(f"Token: {token}")
    
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    response = requests.post(
        f"{BASE_URL}/chat",
        headers=headers,
        data={"message": message}
    )
    
    print(f"\n状态码: {response.status_code}")
    print(f"响应:\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.json()


def test_chat_with_query_token(message: str, token: str = "my_token_123") -> Dict[str, Any]:
    """
    测试聊天接口 - 使用 Query 参数 Token (备用方式)
    
    Args:
        message: 用户消息
        token: 认证令牌
        
    Returns:
        dict: API 响应
    """
    print("\n" + "="*60)
    print(f"测试 3: 聊天接口 (Query 参数方式)")
    print("="*60)
    print(f"消息: {message}")
    print(f"Token: {token}")
    
    params = {"token": token}
    response = requests.post(
        f"{BASE_URL}/chat",
        params=params,
        data={"message": message}
    )
    
    print(f"\n状态码: {response.status_code}")
    print(f"响应:\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.json()


def test_chat_without_token():
    """测试没有提供 Token 的情况 - 应该返回 401 错误"""
    print("\n" + "="*60)
    print("测试 4: 聊天接口 (没有 Token - 预期失败)")
    print("="*60)
    
    response = requests.post(
        f"{BASE_URL}/chat",
        data={"message": "你好"}
    )
    
    print(f"状态码: {response.status_code}")
    print(f"响应:\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.status_code == 401


def test_get_system_config(token: str = "my_secret_token_123"):
    """获取系统配置"""
    print("\n" + "="*60)
    print("测试 5: 获取系统配置")
    print("="*60)
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        f"{BASE_URL}/config/system",
        headers=headers
    )
    
    print(f"状态码: {response.status_code}")
    print(f"响应:\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.status_code == 200


def test_get_agents_config(token: str = "my_secret_token_123"):
    """获取代理配置"""
    print("\n" + "="*60)
    print("测试 6: 获取代理配置")
    print("="*60)
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        f"{BASE_URL}/config/agents",
        headers=headers
    )
    
    print(f"状态码: {response.status_code}")
    print(f"响应:\n{json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    return response.status_code == 200


def print_curl_examples():
    """打印 curl 命令示例"""
    print("\n" + "="*60)
    print("📝 CURL 命令示例")
    print("="*60)
    
    examples = """
# 示例 1: 健康检查
curl http://localhost:8000/health

# 示例 2: 聊天 - Bearer Token (推荐)
curl -X POST "http://localhost:8000/chat" \\
     -H "Authorization: Bearer my_secret_token_123" \\
     -d "message=你好，请帮我分析数据"

# 示例 3: 聊天 - Query 参数
curl -X POST "http://localhost:8000/chat" \\
     -d "message=你好" \\
     -d "token=my_token_123"

# 示例 4: 获取系统配置
curl -H "Authorization: Bearer my_secret_token_123" \\
     http://localhost:8000/config/system

# 示例 5: 获取代理配置
curl -H "Authorization: Bearer my_secret_token_123" \\
     http://localhost:8000/config/agents

# 示例 6: 没有提供 Token (会返回 401 Unauthorized)
curl -X POST "http://localhost:8000/chat" \\
     -d "message=你好"
    """
    print(examples)


def print_python_examples():
    """打印 Python 示例代码"""
    print("\n" + "="*60)
    print("🐍 Python 代码示例")
    print("="*60)
    
    examples = """
import requests

# 示例 1: 使用 Bearer Token 聊天
headers = {"Authorization": "Bearer my_secret_token_123"}
response = requests.post(
    "http://localhost:8000/chat",
    headers=headers,
    data={"message": "你好"}
)
print(response.json())

# 示例 2: 使用 Query 参数聊天
response = requests.post(
    "http://localhost:8000/chat",
    data={"message": "你好"},
    params={"token": "my_token_123"}
)
print(response.json())

# 示例 3: 获取配置
headers = {"Authorization": "Bearer my_secret_token_123"}
response = requests.get(
    "http://localhost:8000/config/system",
    headers=headers
)
print(response.json())
    """
    print(examples)


if __name__ == "__main__":
    print("\n" + "🚀 "*30)
    print("Hermes Multi-Agent System - API 测试")
    print("🚀 "*30)
    
    # 检查服务器是否运行
    try:
        requests.get(f"{BASE_URL}/health", timeout=2)
    except requests.exceptions.ConnectionError:
        print(f"\n❌ 无法连接到 {BASE_URL}")
        print("请先运行服务器: python main.py")
        exit(1)
    
    # 运行测试
    results = []
    
    # 测试 1: 健康检查
    results.append(("健康检查", test_health_check()))
    
    # 测试 2: 使用 Bearer Token 聊天
    try:
        test_chat_with_bearer_token("你好，我是新用户")
        results.append(("Bearer Token 聊天", True))
    except Exception as e:
        print(f"❌ 错误: {e}")
        results.append(("Bearer Token 聊天", False))
    
    # 测试 3: 使用 Query 参数聊天
    try:
        test_chat_with_query_token("请帮我查询最新数据")
        results.append(("Query 参数聊天", True))
    except Exception as e:
        print(f"❌ 错误: {e}")
        results.append(("Query 参数聊天", False))
    
    # 测试 4: 没有 Token
    results.append(("没有 Token 的请求", test_chat_without_token()))
    
    # 测试 5: 获取系统配置
    try:
        results.append(("获取系统配置", test_get_system_config()))
    except Exception as e:
        print(f"❌ 错误: {e}")
        results.append(("获取系统配置", False))
    
    # 测试 6: 获取代理配置
    try:
        results.append(("获取代理配置", test_get_agents_config()))
    except Exception as e:
        print(f"❌ 错误: {e}")
        results.append(("获取代理配置", False))
    
    # 打印示例
    print_curl_examples()
    print_python_examples()
    
    # 打印测试摘要
    print("\n" + "="*60)
    print("📊 测试摘要")
    print("="*60)
    for test_name, passed in results:
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{status} - {test_name}")
    
    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"\n总计: {passed}/{total} 测试通过")
