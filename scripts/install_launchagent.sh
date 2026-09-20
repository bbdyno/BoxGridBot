#!/bin/zsh
# 이 맥에서 봇을 상시 가동한다. 저장소 경로와 사용자에 맞춰 plist 를 만들어 등록한다.
#   scripts/install_launchagent.sh          설치·시작
#   scripts/install_launchagent.sh remove   중지·제거
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.boxgrid.bot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "$1" == "remove" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "제거했습니다."
  exit 0
fi
[[ -x "$ROOT/.venv/bin/python" ]] || { echo ".venv 가 없습니다. MIGRATION.md 2단계를 먼저 하세요."; exit 1; }
[[ -f "$ROOT/.env" ]] || { echo ".env 가 없습니다. MIGRATION.md 3단계를 먼저 하세요."; exit 1; }
mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"
sed "s#/Users/denny.k/Documents/BoxGridBot#$ROOT#g" "$ROOT/deploy/com.boxgrid.bot.plist" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "시작했습니다. 로그: $ROOT/logs/boxgrid.log"
