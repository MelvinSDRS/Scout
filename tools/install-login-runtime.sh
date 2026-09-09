#!/usr/bin/env bash
# Ubuntu/Debian: unpack remote display tools locally, without root or package scripts.
set -euo pipefail
cd "$(dirname "$0")/.."
login_root="${SCOUT_DATA:-$PWD/data}/login-runtime"
mkdir -p "$login_root/packages"
login_root="$(cd "$login_root" && pwd)"
cd "$login_root/packages"
apt-get download x11vnc libvncserver1 libvncclient1 novnc
for package in ./*.deb; do
    dpkg-deb -x "$package" "$login_root"
done
printf 'Remote login runtime installed. Xvfb and xauth must also be on PATH.\n'
