#!/bin/sh
[ "$1" = get ] || exit 0
exec python3 "$(dirname "$0")/proxy.py" credential
