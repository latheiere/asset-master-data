[Unit]
Description=Collect public exchange universes for asset master-data
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=__USER__
WorkingDirectory=__PROJECT_DIR__
Environment=PYTHONUNBUFFERED=1
ExecStart=__PROJECT_DIR__/.venv/bin/python -m mdv.cli --config __CONFIG_PATH__ collect
TimeoutStartSec=1800
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=__PROJECT_DIR__/.data
