#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
echo '本步骤会打开腾讯官方授权页面。请核对页面权限，由你本人完成授权。'
echo '授权凭据只保存到本机 .local-auth 文件夹，不要把它发给面试官。'
python3 meeting_cli.py auth login
read -r -p '授权流程结束；回到网页点“重新检查”。按回车关闭。'
