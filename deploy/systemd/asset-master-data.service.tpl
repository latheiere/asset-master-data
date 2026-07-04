[Unit]
Description=Asset master-data authenticated API
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=__USER__
WorkingDirectory=__PROJECT_DIR__
Environment=PYTHONUNBUFFERED=1
Environment=MDV_GIT_SHA=__GIT_SHA__
ExecStart=__PROJECT_DIR__/.venv/bin/python -m mdv.cli --config __CONFIG_PATH__ serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=__PROJECT_DIR__/.data

[Install]
WantedBy=multi-user.target
