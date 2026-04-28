#!/usr/bin/env bash
# bench/models/serve.sh — bring a benchmark model profile up or down.
#
# Usage:
#   ./bench/models/serve.sh up   <profile>   # run profile.serve_command
#   ./bench/models/serve.sh down <profile>   # run profile.teardown_command
#   ./bench/models/serve.sh list             # list available profiles
#
# The profile name is the basename of bench/models/<profile>.yaml without
# extension (e.g. "llama3-8b").
#
# Idempotent: a missing/empty serve_command (e.g. for hosted API profiles
# like claude-sonnet-4.5) is treated as a no-op.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

usage() {
    cat <<EOF
Usage: $0 <up|down|list> [profile]

Examples:
  $0 list
  $0 up   llama3-8b
  $0 down llama3-8b
  $0 up   claude-sonnet-4.5     # no-op; hosted API
EOF
}

list_profiles() {
    find "${SCRIPT_DIR}" -maxdepth 1 -name '*.yaml' \
        ! -name '*.compose.yaml' \
        -exec basename {} .yaml \; \
        | sort
}

# Extract a top-level scalar field from a YAML profile.
# Uses python's yaml module for correctness; falls back to grep+sed only as
# a last resort if python/yaml is unavailable.
extract_field() {
    local file="$1"
    local field="$2"

    if command -v python3 >/dev/null 2>&1; then
        python3 - "$file" "$field" <<'PY'
import sys, yaml
path, field = sys.argv[1], sys.argv[2]
with open(path) as fh:
    doc = yaml.safe_load(fh) or {}
val = doc.get(field, "")
if val is None:
    val = ""
sys.stdout.write(str(val))
PY
        return
    fi

    # Best-effort fallback: only handles flat scalars + simple block scalars.
    awk -v f="$field" '
        $0 ~ "^"f":" {
            sub("^"f":[ \t]*", "")
            if ($0 == "|" || $0 == ">") { in_block = 1; next }
            print; exit
        }
        in_block && /^[ \t]/ { sub("^[ \t]+", ""); printf "%s\n", $0; next }
        in_block && !/^[ \t]/ { exit }
    ' "$file"
}

run_profile_command() {
    local action="$1"   # serve_command | teardown_command
    local profile="$2"
    local profile_path="${SCRIPT_DIR}/${profile}.yaml"

    if [[ ! -f "$profile_path" ]]; then
        echo "error: profile not found: $profile_path" >&2
        echo "available profiles:" >&2
        list_profiles | sed 's/^/  /' >&2
        exit 1
    fi

    local cmd
    cmd="$(extract_field "$profile_path" "$action")"

    if [[ -z "${cmd//[[:space:]]/}" ]]; then
        echo "[serve.sh] profile '$profile' has no $action — nothing to do (probably a hosted API profile)."
        exit 0
    fi

    echo "[serve.sh] $action for $profile:"
    echo "----"
    printf '%s\n' "$cmd"
    echo "----"

    # Run from repo root so the relative paths inside serve_command
    # ("docker compose -f bench/models/...") resolve correctly.
    (cd "$REPO_ROOT" && bash -c "$cmd")
}

main() {
    if [[ $# -lt 1 ]]; then
        usage; exit 1
    fi

    case "$1" in
        list)
            list_profiles
            ;;
        up)
            [[ $# -ge 2 ]] || { usage; exit 1; }
            run_profile_command serve_command "$2"
            ;;
        down)
            [[ $# -ge 2 ]] || { usage; exit 1; }
            run_profile_command teardown_command "$2"
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            usage; exit 1
            ;;
    esac
}

main "$@"
