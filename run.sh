#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec python3 keystuck_station.py "$@"
