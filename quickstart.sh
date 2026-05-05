#!/bin/bash
# Hermes Multi-Agent System 快速启动脚本

set -e

echo "╔════════════════════════════════════════════════════════╗"
echo "║     🚀 Hermes 多智能体系统快速启动                     ║"
echo "╚════════════════════════════════════════════════════════╝"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查 Python 版本
echo -e "\n${YELLOW}📋 环境检查${NC}"
echo "  检查 Python 版本..."
python_version=$(python --version | awk '{print $2}')
echo "  ✅ Python 版本: $python_version"

# 检查依赖
echo -e "\n${YELLOW}📦 检查依赖${NC}"
echo "  检查关键包..."
python -c "import langchain; print(f'  ✅ langchain: {langchain.__version__}')" 2>/dev/null || echo "  ⚠️ langchain 需要安装"
python -c "import langgraph; print('  ✅ langgraph: 已安装')" 2>/dev/null || echo "  ⚠️ langgraph 需要安装"
python -c "import fastapi; print('  ✅ fastapi: 已安装')" 2>/dev/null || echo "  ⚠️ fastapi 需要安装"

# 运行基础测试
echo -e "\n${YELLOW}🧪 运行基础测试${NC}"
if python test_basic.py > test_basic.log 2>&1; then
    echo -e "  ${GREEN}✅ 基础测试通过${NC}"
else
    echo -e "  ${RED}❌ 基础测试失败，查看 test_basic.log${NC}"
fi

# 运行演示
echo -e "\n${YELLOW}🎭 运行系统演示${NC}"
if python run_demo.py > demo.log 2>&1; then
    echo -e "  ${GREEN}✅ 系统演示成功${NC}"
else
    echo -e "  ${RED}❌ 系统演示失败，查看 demo.log${NC}"
fi

# 显示启动说明
echo -e "\n${YELLOW}💡 启动应用${NC}"
echo "  运行以下命令启动 FastAPI 服务:"
echo ""
echo -e "  ${GREEN}uvicorn main:app --reload --port 8000${NC}"
echo ""
echo "  然后访问:"
echo "    - API 文档: http://localhost:8000/docs"
echo "    - 健康检查: http://localhost:8000/health"
echo ""

# 显示环境配置说明
echo -e "\n${YELLOW}⚙️  环境配置${NC}"
echo "  如需使用 OpenAI API，请设置环境变量:"
echo ""
echo -e "  ${GREEN}export OPENAI_API_KEY=sk-...${NC}"
echo ""

echo -e "\n${GREEN}✨ 准备就绪！${NC}\n"
