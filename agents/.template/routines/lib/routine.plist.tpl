<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- routine.plist.tpl : rendered at ACTIVATION time by routines/lib/routine-ctl.sh
     (NOT by mint.sh: the .tpl extension is deliberately outside mint's
     substitution globs, and this dir is outside install.sh's routines/*.plist
     auto-schedule glob, so bundle routines are never scheduled without the
     user's conversational opt-in). Tokens, each wrapped in two underscores
     on both sides: LABEL SCRIPT HOUR MINUTE ROOT HOMEDIR LOGNAME
     WEEKDAY_ENTRY (the last renders to a Weekday key line for weekly
     routines, or to nothing). They are named bare here so rendering never
     writes a value into this comment; routine-ctl.sh drops the comment
     from the rendered unit anyway (DGN-1990). No double hyphen in here. -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>__LABEL__</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__SCRIPT__</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>__HOUR__</integer>
        <key>Minute</key>
        <integer>__MINUTE__</integer>__WEEKDAY_ENTRY__
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>__HOMEDIR__/.local/bin:__HOMEDIR__/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>__HOMEDIR__</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>__ROOT__/.telegram_bot/logs/__LOGNAME__.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__ROOT__/.telegram_bot/logs/__LOGNAME__.stderr.log</string>
    <key>WorkingDirectory</key>
    <string>__ROOT__</string>
</dict>
</plist>
