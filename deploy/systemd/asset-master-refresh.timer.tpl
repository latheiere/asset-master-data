[Unit]
Description=Scheduled asset master-data collection

[Timer]
OnCalendar=__ON_CALENDAR__
Persistent=false
RandomizedDelaySec=0
AccuracySec=1s
Unit=asset-master-refresh.service

[Install]
WantedBy=timers.target
