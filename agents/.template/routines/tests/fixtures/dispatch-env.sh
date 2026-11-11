# test-lane: lib shared scratch environment for dispatch fixtures (DGN-2196)
# Source after allocating the suite's scratch root, before creating any repos.
# Keep the real runner's budget token: nested runners must inherit their slot.
isolate_dispatch_env() {
  local root="$1" name
  while IFS= read -r name; do
    case "$name" in
      DGN_WORKER_TEST_HELD|DGN_HEAVY_LOCK_HELD) continue ;;
      DOGANY_*|DGN_*|CLOUD_*|CLAUDE*|CODEX_*|OPENAI_*|ANTHROPIC_*|DISPATCH_*|RESET_PASS*|XDG_*|AGENT_LANG|TASK_FILE|REQUESTED_*|LOCAL_WHY|NEEDS_CONNECTORS|WT_*|WORKSPACE)
        unset "$name" ;;
    esac
  done < <(compgen -e)
  # Do not clear GIT_CONFIG_*: the runner's no-network-push guard lives there.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  export DOGANY_RESOURCE_SETTINGS_DIR="$root/resource-settings"
  export HOME="$root/home" DOGANY_SHARED_HOME="$root/shared"
  export DOGANY_USAGE_STATE_DIR="$root/usage-state" DOGANY_DISPATCH_WT_BASE="$root/wt"
  export DOGANY_DISPATCH_MACHINE="0.1 16 64000 0"
  export DOGANY_DISPATCH_MACHINE_SNAPSHOT="0.1 16 64000" DOGANY_DISPATCH_MACHINE_PRESSURE=off
  mkdir -p "$HOME" "$DOGANY_SHARED_HOME" "$DOGANY_USAGE_STATE_DIR"
}
