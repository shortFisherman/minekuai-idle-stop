#!/usr/bin/env bash
set -euo pipefail
umask 022

cold_backup=false
if [[ $# -eq 1 && $1 == --cold-backup ]]; then
    cold_backup=true
elif (( $# != 0 )); then
    printf '%s\n' '用法: sudo bash install.sh [--cold-backup]' >&2
    exit 1
fi

if (( EUID != 0 )); then
    printf '%s\n' '请使用 sudo bash install.sh 安装。' >&2
    exit 1
fi

src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app=/opt/minekuai-stop
conf=/etc/minekuai-stop
unit=/etc/systemd/system/minekuai-stop.service

command -v python3 >/dev/null
command -v systemctl >/dev/null
install -d -m 755 "$app" "$conf"
python3 -m venv "$app/venv"

pip_args=(--disable-pip-version-check --requirement "$src/requirements.txt")
if [[ -n "${WHEEL_DIR:-}" ]]; then
    pip_args+=(--no-index --find-links "$WHEEL_DIR")
fi
"$app/venv/bin/python" -m pip install "${pip_args[@]}"

install -m 644 "$src/monitor.py" "$src/minekuai.py" "$src/keepalive.py" "$src/inventory.py" "$src/authorize.py" "$app/"
install -m 644 "$src/status.py" "$app/"
if [[ ! -e "$conf/config.json" ]]; then
    install -m 644 "$src/config.example.json" "$conf/config.json"
fi
install -m 644 "$src/minekuai-stop.service" "$unit"
if "$cold_backup"; then
    install -m 644 "$src/cold_backup.py" "$src/cold_zip.py" \
        "$src/backup_files.py" "$src/backup_state.py" "$app/"
    install -m 644 "$src/minekuai-cold.service" "$src/minekuai-cold.timer" \
        "$src/minekuai-cold-recover.service" "$src/minekuai-cold-recover.timer" \
        /etc/systemd/system/
fi
systemctl daemon-reload
printf '%s\n' '安装完成。按 README 配置实例，授权并检查连接后再启动服务。'
