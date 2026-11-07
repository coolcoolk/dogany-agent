[Unit]
# DGN-1655: fires the paired instance-drive service every period (TTL/4).
# OnActiveSec gives the first sweep right after (re)start -- the launchd
# twin's RunAtLoad -- so install verification sees a first beat promptly.
# Rendered policy: period=__PERIOD__ ttl=__TTL__
Description=dogany instance drive timer (__LABEL__) -- __PERIOD__s cadence

[Timer]
OnActiveSec=5
OnUnitActiveSec=__PERIOD__
Unit=__LABEL__.service

[Install]
WantedBy=timers.target
