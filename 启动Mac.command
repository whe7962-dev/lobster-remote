#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
if ! command -v python3 >/dev/null 2>&1; then
  echo '请先从 https://www.python.org/downloads/macos/ 安装 Python 3.10 或更高版本。'
  read -r -p '按回车退出。'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || exit 1
fi
if ! .venv/bin/python -c 'import playwright' >/dev/null 2>&1; then
  echo '首次运行：正在安装浏览器执行依赖，请保持网络连接。'
  .venv/bin/python -m pip install -r requirements.txt --index-url https://pypi.org/simple || { read -r -p '安装失败，按回车退出。'; exit 1; }
fi
.venv/bin/python app.py
read -r -p '程序已停止，按回车关闭。'
