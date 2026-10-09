# 仅交互式会话带 HUD；管理命令与脚本仍运行原生 Codex。
codex() {
  case "$1" in
    exec|e|review|login|logout|mcp|plugin|app-server|remote-control|app|completion|update|doctor|sandbox|debug|apply|queue|archive|delete|migrate-rollouts|unarchive|cloud|exec-server|features|help|agents|--help|-h|--version|-V)
      command codex "$@" ;;
    *)
      if [[ -t 0 && -t 1 && "${BLEET_HUD:-}" != 0 ]]; then
        /usr/bin/python3 "$HOME/.codex/scripts/bleet-hud.py" launch "$@"
      else
        command codex "$@"
      fi ;;
  esac
}
