[Unit]
Description=Scheduled asset master-data collection

[Timer]
OnCalendar=__ON_CALENDAR__
Persistent=true
Unit=asset-master-refresh.service

[Install]
WantedBy=timers.target
