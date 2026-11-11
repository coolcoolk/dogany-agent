<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- instance-drive.plist.tpl : DGN-1655 framework-owned instance drive.
     Rendered ONLY by routines/lib/drive-setup.sh from the ledger's policy
     query (terminal-state-ledger.py policy). Never hand-edit the rendered
     copy: a TTL change re-renders and reloads it. The .tpl extension keeps
     it outside mint/update substitution and the routines/*.plist load glob;
     the rendered routines/LABEL.plist is what the default loaders see.
     StartInterval = period = TTL/4; the sweep argv carries the throttle
     (period/2) and the beat TTL, and the environment records both rendered
     values for audit. DOGANY_TSL_DRIVE=1 is the drive marker: only this unit
     may write the reserved terminal-state-sweep row (ledger F2).
     Tokens: __LABEL__ __ROOT__ __HOMEDIR__ __PERIOD__ __THROTTLE__ __TTL__ -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>__LABEL__</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__ROOT__/routines/cron-guard.sh</string>
        <string>--label</string>
        <string>__LABEL__</string>
        <string>--friendly-name</string>
        <string>instance-drive</string>
        <string>--log</string>
        <string>__ROOT__/.telegram_bot/logs/instance-drive.stdout.log</string>
        <string>--queue-policy</string>
        <string>fail-open</string>
        <string>--</string>
        <string>/usr/bin/python3</string>
        <string>__ROOT__/routines/terminal-state-ledger.py</string>
        <string>sweep</string>
        <string>--throttle</string>
        <string>__THROTTLE__</string>
        <string>--beat-ttl</string>
        <string>__TTL__</string>
    </array>
    <key>StartInterval</key>
    <integer>__PERIOD__</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>__HOMEDIR__/.local/bin:__HOMEDIR__/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>__HOMEDIR__</string>
        <key>DOGANY_TSL_DRIVE</key>
        <string>1</string>
        <key>DOGANY_KIT_DB_FREE</key>
        <string>1</string>
        <key>DOGANY_DRIVE_PERIOD</key>
        <string>__PERIOD__</string>
        <key>DOGANY_DRIVE_TTL</key>
        <string>__TTL__</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>__ROOT__/.telegram_bot/logs/instance-drive.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__ROOT__/.telegram_bot/logs/instance-drive.stderr.log</string>
    <key>WorkingDirectory</key>
    <string>__ROOT__</string>
</dict>
</plist>
