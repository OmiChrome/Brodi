#!/bin/zsh
# Shared process controls, retaining this launcher's live activity display.
ROOT=${0:A:h}
cd "$ROOT" || exit 1
brodi_python() {
  if [[ -x .venv/bin/python ]]; then .venv/bin/python "$@"; else uv run python "$@"; fi
}
brodi_activity() {
  local pid=$(brodi_python -c 'from brodi.tray import AgentController; c = AgentController(); print(c.leaf_pid(c.processes()) or "")')
  if [[ -z $pid ]]; then print 'Brodi is stopped. Run ./console.sh start.'; return; fi
  if [[ ${2:-} == --once ]]; then
    brodi_python -m brodi.activity --pid "$pid" --once
  else
    brodi_python -m brodi.activity --pid "$pid"
  fi
}
case ${1:-start} in
  start|restart)
    /bin/zsh "$ROOT/brody" "${1:-start}" || exit $?
    sleep 3
    brodi_activity "$@" ;;
  status) brodi_activity "$@" ;;
  log) brodi_python -m brodi.activity --once ;;
  *) exec /bin/zsh "$ROOT/brody" "$@" ;;
esac
