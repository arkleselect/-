#!/bin/zsh
cd "$(dirname "$0")"
export MAX_CONCURRENT_JOBS=3
IP=""
for IFACE in en0 en1 en2; do
  FOUND="$(ipconfig getifaddr "$IFACE" 2>/dev/null)"
  if [ -n "$FOUND" ]; then
    IP="$FOUND"
    break
  fi
done
echo "课程视频压缩系统启动中..."
echo "本机访问：http://localhost:8088"
if [ -n "$IP" ]; then
  echo "同事访问：http://$IP:8088"
else
  echo "未能自动获取局域网 IP，请在系统设置中查看本机 IP。"
fi
python3 web_app.py
