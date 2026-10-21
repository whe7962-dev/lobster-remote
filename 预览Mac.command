#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 app.py --preview
read -r -p '按回车关闭。'
