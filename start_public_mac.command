#!/bin/zsh
cd "$(dirname "$0")"
exec python3 -B start_public.py "$@"
