#!/usr/bin/env bash
# QQ / NapCat 服务器只读预检。
#
# 这个脚本只“看”，不启动/停止任何服务，也不打印配置文件内容。
# 在服务器上用 root（宝塔终端）运行：
#
#   bash /root/qq_server_preflight.sh
#
# 把输出贴回来时，请先删掉可能出现的 QQ 号、token、二维码路径等敏感信息。
# 注意：/root 下的目录普通用户 adom 读不到，所以必须用 root 跑。

set -u

echo "== 当前用户 =="
id

echo
echo "== NapCat / QQ 进程 =="
# 方括号写法避免 grep 匹配到 grep 自己。
ps -ef | grep -E '[n]apcat|[Q]Q' || echo "（没有发现进程）"

echo
echo "== 监听端口（3000/3001/6099 是 NapCat/OneBot 常见端口）=="
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | grep -E ':(3000|3001|6099|18080)\b' || echo "（没有监听）"
else
  netstat -lntp 2>/dev/null | grep -E ':(3000|3001|6099|18080)\b' || echo "（没有监听）"
fi

echo
echo "== systemd 里有没有 NapCat 服务 =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-unit-files 2>/dev/null | grep -i napcat || echo "（没有 systemd 单元）"
else
  echo "（没有 systemctl）"
fi

echo
echo "== 常见安装目录 =="
for dir in /root/Napcat /root/app/agent/QQrobot /root/app/agent/QQrobot/AstrBot /root/.config/QQ/NapCat; do
  if [ -e "$dir" ]; then
    ls -ld "$dir"
  else
    echo "$dir ：不存在"
  fi
done

echo
echo "== NapCat 版本（只读 package.json 的 name/version）=="
if [ -f /root/Napcat/package.json ]; then
  python3 - <<'PY'
import json
from pathlib import Path

path = Path("/root/Napcat/package.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print("name:", data.get("name"))
    print("version:", data.get("version"))
except Exception as exc:  # noqa: BLE001 - 预检脚本只报告，不中断
    print("读取失败:", type(exc).__name__)
PY
else
  echo "（没有 /root/Napcat/package.json）"
fi

echo
echo "== NapCat 配置文件清单（只看路径，不看内容）=="
if [ -d /root/.config/QQ/NapCat ]; then
  find /root/.config/QQ/NapCat -maxdepth 2 -type f -printf '%p\n' 2>/dev/null | sort
else
  echo "（没有 /root/.config/QQ/NapCat）"
fi

echo
echo "== Docker（如果装了）=="
if command -v docker >/dev/null 2>&1; then
  docker ps --format '{{.Names}} {{.Image}} {{.Ports}}' 2>/dev/null || echo "（docker 不可用）"
else
  echo "（没有 docker）"
fi

echo
echo "预检完成。这个脚本没有启动或修改任何服务。"
