#!/bin/bash
set -euo pipefail

readonly TARGET_FILE="${1:-}"
readonly EXCLUDE_NEWER_PATTERN='^exclude-newer = "[^"]+"$'
EXCLUDE_NEWER_VALUE="$(date -u -d '7 days ago' +'%Y-%m-%dT00:00:00Z')"
readonly EXCLUDE_NEWER_VALUE
readonly LOG_FILE="${LOG_FILE:-/tmp/bump_exclude_newer.log}"

log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp file line function_name
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')
    file="${BASH_SOURCE[1]##*/}"
    line="${BASH_LINENO[0]}"
    function_name="${FUNCNAME[1]:-main}"
    printf '{"time":"%s","level":"%s","file":"%s","line":%d,"func":"%s","msg":"%s"}\n' \
        "$timestamp" "$level" "$file" "$line" "$function_name" "$message" >&2
}

trap 'log ERROR "command failed"' ERR
exec > >(tee -a "$LOG_FILE") 2>&1

if [[ -z "$TARGET_FILE" ]]; then
    log ERROR "usage: bump_exclude_newer.sh <pyproject.toml>"
    exit 1
fi

if [[ ! -f "$TARGET_FILE" ]]; then
    log ERROR "pyproject file does not exist"
    exit 1
fi

if ! grep -qE "$EXCLUDE_NEWER_PATTERN" "$TARGET_FILE"; then
    log ERROR "no uv exclude-newer setting found"
    exit 1
fi

sed -i -E "s|$EXCLUDE_NEWER_PATTERN|exclude-newer = \"$EXCLUDE_NEWER_VALUE\"|" "$TARGET_FILE"
log INFO "updated uv exclude-newer cutoff"
