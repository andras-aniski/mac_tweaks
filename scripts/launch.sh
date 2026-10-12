#!/bin/sh
# launch.sh <tool folder> <script> [args]
# Without args, starts the menu bar app in the background (unless it's already running).
# With args, runs the tool's command line in the foreground.
dir="$(realpath "$(dirname "$(realpath "$0")")/../$1")"
script=$2
shift 2

if [ $# -gt 0 ]; then
    exec "$dir/.venv/bin/python" "$dir/$script" "$@"
fi
if pgrep -qf "(^|[ /])$script\$"; then
    echo "$script is already running" >&2
    exit 1
fi
nohup "$dir/.venv/bin/python" "$dir/$script" >/dev/null 2>&1 &
