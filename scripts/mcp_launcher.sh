#!/bin/sh
set -eu

find_repo_root() {
    current_dir="$1"
    while [ -n "$current_dir" ] && [ "$current_dir" != "/" ]; do
        if [ -f "$current_dir/preciso_mcp/server.py" ]; then
            echo "$current_dir"
            return 0
        fi
        current_dir=$(dirname "$current_dir")
    done
    return 1
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(find_repo_root "$script_dir")
if [ -z "$repo_root" ]; then
    echo "Unable to locate repo root containing preciso_mcp/server.py." >&2
    exit 1
fi

python_path="$repo_root/.venv/bin/python"
if [ ! -x "$python_path" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python_path="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        python_path="$(command -v python)"
    else
        echo "No Python runtime found. Create .venv or install python3." >&2
        exit 1
    fi
fi

cd "$repo_root"
# -m keeps the repo root on sys.path so `preciso_mcp`, `core`, `ingest`, and
# `config` resolve without an editable install (and `mcp` stays the SDK).
exec "$python_path" -m preciso_mcp.server
