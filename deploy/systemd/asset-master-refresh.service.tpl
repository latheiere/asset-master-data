[Unit]
Description=Collect public exchange universes for asset master-data
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=__USER__
Slice=asset-master-data.slice
WorkingDirectory=__PROJECT_DIR__
Environment=PYTHONUNBUFFERED=1
ExecStart=__PYTHON__ -m mdv.cli --config __CONFIG_PATH__ collect
TimeoutStartSec=1800
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
