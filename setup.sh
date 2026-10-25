#!/bin/bash
# 项目快速设置脚本

set -e
cd -- "$(dirname -- "$0")"

echo "🚀 小鹅通视频下载工具 - 快速设置"
echo "=================================="
echo ""

# 检查 uv 是否安装
echo "📦 检查 uv 是否安装..."
if ! command -v uv &> /dev/null; then
    echo "❌ 未检测到 uv，正在安装..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "✅ uv 安装完成"
else
    echo "✅ uv 已安装"
fi

# 安装项目依赖
echo ""
echo "📦 安装项目依赖（含 yt-dlp、内存检测和 FFmpeg）..."
uv sync --locked
uv run python -c 'import subprocess; from runtime_tools import ffmpeg_executable; subprocess.run([ffmpeg_executable(), "-version"], check=True)'

# 安装 Playwright 浏览器
echo ""
echo "📦 安装 Playwright 浏览器..."
uv run playwright install chromium

echo ""
echo "✅ 设置完成！"
echo ""
echo "🎉 您现在可以运行程序了:"
echo "   uv run python main.py"
echo ""

