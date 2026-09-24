#!/bin/zsh
cd "$(dirname "$0")"
exec python3 -B lan_file_hub.py "$@"
