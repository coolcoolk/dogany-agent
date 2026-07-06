---
name: agent-browser
description: >
  Headless browser automation via agent-browser CLI. Fires when the user asks to
  open a website, fill a form, click a button, take a screenshot, scrape data from
  a page, test a web app, log in to a site, or automate any browser task. Uses the
  agent-browser CLI (Vercel Labs) which ships Chrome for Testing and a compact
  accessibility-tree snapshot format (~7K tokens per 10-step task vs MCP ~114K).
  Skill is DORMANT by default. Enable it through the installer opt-in or by asking
  the agent to activate browser automation.
allowed-tools: Bash(agent-browser:*)
---

# agent-browser -- browser automation skill

## overview

This skill wraps the agent-browser CLI (Vercel Labs). It is script-first: the CLI
runs as a bash command, outputs compact accessibility-tree snapshots to stdout, and
writes screenshots to disk. No MCP server is loaded into context; tool-definition
overhead is zero.

Token profile (measured): ~1K tokens per snapshot, ~7K total for a 10-step task.

## setup check

Before running any command, verify the CLI is installed:

```bash
agent-browser --version
```

If the command fails, the CLI was not installed during setup. Install it manually:

```bash
npm install -g agent-browser
agent-browser install          # downloads Chrome for Testing (~684 MB)
```

## core workflow

```bash
agent-browser open <url>       # 1. open a page
agent-browser snapshot -i      # 2. see interactive elements only
agent-browser click @e3        # 3. act on refs from the snapshot
agent-browser snapshot -i      # 4. re-snapshot after each page change
```

Refs (@e1, @e2, ...) are assigned fresh on every snapshot. They go stale on any
page change. Always re-snapshot before the next ref interaction.

## reading a page

```bash
agent-browser snapshot -i               # interactive elements only (preferred)
agent-browser snapshot                  # full tree
agent-browser snapshot -i -u            # include href urls on links
agent-browser read <url>                # fetch page text / markdown (no Chrome session)
agent-browser get text @e1             # visible text of an element
agent-browser get url                  # current URL
agent-browser get title                # page title
```

## interacting

```bash
agent-browser fill @e2 "text"          # clear then type
agent-browser type @e2 "text"          # type without clearing
agent-browser click @e1                # click by ref
agent-browser click "#submit"          # click by CSS selector
agent-browser press Enter              # key press at current focus
agent-browser check @e3                # check checkbox
agent-browser select @e4 "value"       # select dropdown option
agent-browser scroll down 500          # scroll (up/down/left/right)
```

## waiting

```bash
agent-browser wait @e1                 # until element appears
agent-browser wait --text "Success"    # until text appears
agent-browser wait --url "**/dashboard" # until URL matches pattern
agent-browser wait --load networkidle  # until network idle (after SPA navigation)
```

## screenshots

Always save screenshots to files/tmp/ or files/outbox/, then deliver via
send_file::. Never inline base64 images into context (token cost is extreme).

```bash
agent-browser screenshot files/outbox/page.png
```

Deliver to user:

```
send_file:: <absolute-path-to-png>
```

## authentication and profiles

Use a dedicated persistent profile to keep login state across sessions:

```bash
agent-browser --profile ~/.dogany/browser-profile open <url>
```

For sensitive credentials, use the auth vault:

```bash
agent-browser auth save my-app --url https://app.example.com/login \
  --username user@example.com --password-stdin

agent-browser auth login my-app         # fills form and waits for success
```

Profile directory (~/.dogany/browser-profile) contains cookies and session tokens.
It is personal data: keep it out of git (covered by .gitignore contract). Do not
log or share its contents.

## session lifecycle

```bash
agent-browser open <url>               # open / reuse existing session
agent-browser close                    # close current tab
agent-browser close --all              # close all sessions
```

The browser daemon stays running across commands. Call close when the task is done.

## extracting data

```bash
agent-browser extract @e1              # structured extract from element
agent-browser get html @e1             # raw innerHTML
agent-browser get attr @e1 href        # attribute value
agent-browser get count ".item"        # count matching CSS elements
```

## rules for this skill

1. Snapshot first, act second. Always run `snapshot -i` before clicking to get
   current refs.
2. Screenshots go to disk, never inline. Save to files/outbox/ and use
   send_file:: to deliver.
3. One session at a time by default. Close the session when the task is done.
4. Login setup requires a headed session (--headed flag). The browser daemon runs
   headless by default. If the user needs to complete a login that cannot be
   automated (CAPTCHA, MFA device), have them run agent-browser with --headed in
   a terminal once to seed the profile, then the agent can reuse the saved state.
5. Do not store credentials in plain text. Use the auth vault.

## live skill docs

The CLI ships its own skill documentation. Run this to get the latest reference:

```bash
agent-browser skills get core
```

If the local SKILL.md is out of date with the installed CLI version, the live
docs from the command above are authoritative.

## when browser automation is not available

If agent-browser is not installed (opt-in was declined at install time), tell the
user clearly:

- Browser automation is not installed on this agent.
- To enable it: run `npm install -g agent-browser && agent-browser install` in a
  terminal. Note: Chrome for Testing download is approximately 684 MB.
- Alternatively, reinstall the agent and choose "yes" at the browser automation
  step.

Do not attempt to use Playwright, Selenium, or MCP browser tools as a fallback
without explicit user consent -- they have different token profiles and
installation requirements.
