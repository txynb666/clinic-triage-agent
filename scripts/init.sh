#!/usr/bin/env bash
# ============================================================
# scripts/init.sh —— 首次克隆后的一键初始化
# 用法：bash scripts/init.sh
# ============================================================
set -e

echo "==> 检查 Python 版本"
python3 -c "import sys; assert sys.version_info >= (3,11), '需要 Python 3.11+'" || exit 1
python3 --version

echo "==> 创建虚拟环境"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> 安装依赖（约 1-2 分钟）"
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "==> 准备环境变量"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    已生成 .env，请填入 LLM_API_KEY 后再启动"
else
  echo "    .env 已存在，跳过"
fi

echo "==> 确保数据目录存在"
mkdir -p data/kb_files data/index

echo ""
echo "✅ 初始化完成。下一步："
echo "   1. 编辑 .env 填入 LLM_API_KEY / LLM_BASE_URL"
echo "   2. source .venv/bin/activate"
echo "   3. make dev          # 启动服务"
echo "   4. 打开 http://localhost:8000         （员工端）"
echo "   5. 打开 http://localhost:8000/admin/login （后台，admin/admin123）"
