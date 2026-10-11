[Unit]
# DGN-1655: framework-owned instance drive (Linux twin of
# instance-drive.plist.tpl). Rendered ONLY by routines/lib/drive-setup.sh from
# `terminal-state-ledger.py policy`; never hand-edit the rendered unit.
# One-shot sweep through cron-guard; DOGANY_TSL_DRIVE=1 is the drive marker
# (only this unit may write the reserved terminal-state-sweep ledger row).
# Rendered policy: period=__PERIOD__ throttle=__THROTTLE__ ttl=__TTL__
Description=dogany instance drive (__LABEL__) -- terminal-state sweep every __PERIOD__s

[Service]
Type=oneshot
ExecStart=/bin/bash __ROOT__/routines/cron-guard.sh --label __LABEL__ --friendly-name instance-drive --log __ROOT__/.telegram_bot/logs/instance-drive.stdout.log --queue-policy fail-open -- /usr/bin/python3 __ROOT__/routines/terminal-state-ledger.py sweep --throttle __THROTTLE__ --beat-ttl __TTL__
WorkingDirectory=__ROOT__
Environment=HOME=__HOMEDIR__
Environment=PATH=__HOMEDIR__/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
Environment=DOGANY_TSL_DRIVE=1
Environment=DOGANY_KIT_DB_FREE=1
Environment=DOGANY_DRIVE_PERIOD=__PERIOD__
Environment=DOGANY_DRIVE_TTL=__TTL__
