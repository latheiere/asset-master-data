[Unit]
Description=Asset master-data authenticated API
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=__USER__
Slice=asset-master-data.slice
WorkingDirectory=__PROJECT_DIR__
Environment=PYTHONUNBUFFERED=1
Environment=MDV_GIT_SHA=__GIT_SHA__
ExecStart=__PYTHON__ -m mdv.cli --config __CONFIG_PATH__ serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__DATA_DIR__
MemoryHigh=160M
MemoryMax=224M
TasksMax=96
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
