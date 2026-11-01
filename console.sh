#!/bin/zsh
# brodi console: start / stop / status / log
# Usage: ./console.sh start|stop|status|log
cd "$(dirname "$0")"
LOG="${BRODI_LOG:-/tmp/brodi-console.log}"
brodi_python() {
  if [ -x .venv/bin/python ]; then .venv/bin/python "$@"; else uv run python "$@"; fi
}
brodi_activity() {
  pid=$(pgrep -n -f '[a]gent.py console')
  if [ -z "$pid" ]; then echo "Brodi is stopped. Run ./console.sh start."; return; fi
  if [ "${2:-}" = "--once" ]; then
    brodi_python -m brodi.activity --pid "$pid" --once
  else
    brodi_python -m brodi.activity --pid "$pid"
  fi
}
case "${1:-start}" in
  start)
    if pgrep -f '[a]gent.py console' >/dev/null; then brodi_activity "$@"; exit 0; fi
    # Keys exported only in the interactive shell profile (e.g. ~/.zshrc) aren't visible to a
    # detached start; pull them in when missing.
    if [ -z "${OPENAI_API_KEY}" ]; then
      val=$(zsh -ic "print -r -- \${OPENAI_API_KEY}" 2>/dev/null); [ -n "$val" ] && export "OPENAI_API_KEY=$val"
    fi
    if [ -z "${OPENAI_API_KEY}" ] && ! brodi_python -c 'from brodi.config import load_environment; load_environment(); import os,sys; sys.exit(0 if os.environ.get("OPENAI_API_KEY") else 1)'; then echo "missing: OPENAI_API_KEY (set in .env.local or your shell profile)"; exit 1; fi
    nohup uv run python agent.py console > "$LOG" 2>&1 &
    sleep 3; echo "Brodi started. Waiting for the voice session..."; brodi_activity "$@" ;;
  stop)
    pkill -INT -f "agent.py console" 2>/dev/null && sleep 2; pkill -9 -f "agent.py console" 2>/dev/null; echo "stopped" ;;
  status)
    brodi_activity "$@" ;;
  log)
    brodi_python -m brodi.activity --once ;;
  debug)
    sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$LOG" | grep -E "user_transcript|brodi\.router +route|\"role\": \"assistant\"" | sed -E 's/^ *[0-9:.]* *(DEBUG|INFO) *//' ;;
  *) echo "usage: $0 start|stop|status|log|debug [--once]"; exit 1 ;;
esac
