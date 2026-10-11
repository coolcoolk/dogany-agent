# Shared representative-instance rule from install.sh (G1 / P13).
# Sourced library: no side effects. Absent key/file = main; domain is the
# only non-representative class. Read config as data, never source it.
instance_class_of() {
  local v
  v="$(grep -E '^DOGANY_AGENT_CLASS=' "$1/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  case "$v" in domain) printf 'domain' ;; *) printf 'main' ;; esac
}
