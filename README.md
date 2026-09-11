# dogany-agent

[한국어 안내 -> README-ko.md](README-ko.md)

Your own Claude Code agent, reachable over Telegram. It lives on your own
machine (Mac, Linux, or Windows via WSL2), runs whenever the machine is on,
and actually remembers you: long-term memory, scheduled routines, and a
skill system -- fully self-contained per instance, with zero personal data
baked in.

## Install (2 minutes)

    git clone https://github.com/coolcoolk/dogany-agent ~/.dogany/framework && cd ~/.dogany/framework && bash install.sh

The wizard checks prerequisites (it installs Claude Code for you if
missing), asks your language, timezone, and a bot token from @BotFather,
then mints your agent. Autostart and an email account (a dedicated one is
recommended) are optional steps you can skip.

Open Telegram and greet your bot. It starts unowned: it prints a one-time
claim code, and the first account to send `/claim <code>` becomes its sole
owner. The first conversation is onboarding -- the agent asks what to be
called and how to speak.

Good to know:

- Clone under `~/.dogany/` as shown. On macOS, do NOT clone into
  Documents/Desktop/Downloads -- background services cannot read those
  folders (the installer refuses such paths).
- To preview every step without changing anything:
  `bash install.sh --dry-run --lang en`
- Windows needs WSL2. The support is implemented but unverified.

## Philosophy

<p align="center"><img src="docs/img/golden-circle-en.png" width="440" alt="You decide the WHY; your agent proposes and runs the HOW and WHAT"></p>

- CEO of your own life: you set the Why; your agent proposes and runs
  the How and the What, and you choose among outcomes.
- Memory, forged: conversations are compressed and refined into a second
  brain -- the longer you live with your agent, the deeper it knows you.
- A better you, daily: deciding Whys and picking outcomes surfaces your
  taste, and growth becomes visible in retrospectives and numbers.
- Your data is yours: a minted instance keeps memories and records local;
  the shipped tree carries zero personal data, and instance data never
  enters the repo.

## How it's built

Everything is one containment relation, smallest unit outward:

<p align="center"><img src="docs/img/structure-tree-en.png" width="700" alt="Structure tree: skill inside module inside pack inside kit"></p>

- **Skill** -- one action: a reminder skill pings you once at 9pm to take
  your meds. Skills are the atoms everything larger is assembled from.
- **Module** -- one ability, from skills plus data: the diet-log module
  turns "200g chicken breast" into calories saved in LifeKit.
- **Pack** -- modules plus knowledge, bundled to shape a staff agent: a
  health-trainer pack becomes a workout coach that plans your routines and
  checks in on you.
- **Kit** -- the data contract for a whole domain, so every module and pack
  in it shares one schema: **LifeKit** covers daily life -- meals, workouts,
  schedule. (Kits for other domains are planned.)

Two shortcuts sit outside the nesting: a core module (reminders, memory
search) attaches straight to the main agent, skipping the kit; and
installing a pack creates a staff agent under the main agent -- your one
and only body, which holds the identity and memory and equips the kit.

## Memory

Your profile and the agent's identity are in context on every turn -- you
never re-explain yourself. Everything else is recalled the moment you send
a message: a hybrid search (keyword + semantic) over your memory files
splices the top matches in before the model reads it. Semantic search needs
local Ollama with the bge-m3 model; without it, keyword search still works.
Memory itself is plain local Markdown -- the search index is optional and
rebuildable anytime.

Writing is just as deliberate. Nightly, the day's conversations are
distilled into an inbox -- noise and duplicates dropped, secrets redacted,
only facts worth keeping a year from now. Weekly, inbox items are filed
into topic files. The agent never writes memory files directly, so the
vault stays clean and auditable.

<p align="center"><img src="docs/img/distillation-en.png" width="520" alt="Memory processing: raw chat is refined nightly into an inbox, then filed weekly into topic files"></p>

The code is the reference for how the routing works underneath.

## Windows

Windows needs WSL2. The support is implemented but unverified. One
platform limit to know: under WSL2 the agent comes back after a reboot
only once you sign in to Windows, not before.

## Talk to it locally (no Telegram needed)

The agent is the folder, not the bot. Telegram is just one door; running
`dogany` in any terminal opens a local session with the SAME agent -- same
identity, memory, hooks, and skills:

    dogany

Telegram and CLI conversations are separate threads but share one long-term
memory: the nightly consolidate pass reads both, so tomorrow's Telegram chat
remembers today's terminal session. Running both at once is safe.

If you prefer to open the agent directory manually instead:

    cd ~/.dogany/main && claude

Tip for laptops: the agent runs only while the machine is awake. Keep it on
AC power and prevent system sleep. On macOS: System Settings > Battery >
Options > turn on "Prevent automatic sleeping on power adapter when the
display is off" (exact label may vary by macOS version), or run
`sudo pmset -c sleep 0`. Do not close the lid unless an external display is
connected (clamshell mode keeps the machine awake). Display sleep is fine --
only system sleep stops the agent.

## Keeping it up to date

- Ask your agent in Telegram: "update yourself"
- Or run locally: `dogany update`

The agent pins itself to the latest published release tag -- you only ever
receive versioned releases with release notes -- and refreshes its built-in
skills. Your memories, identity, and personal data are never touched. If
you edited a built-in skill, the update backs your copy up before replacing
it; to customize durably, copy the skill under your own name and edit that.

## Data and privacy

- The bot is owner-locked: claim it on first contact, and no one else can
  reach your agent.
- All personal data -- memories, records, sessions, and tokens -- lives in
  your instance directory and never enters the repo or its git history.
- The repo ships code and empty scaffolding only.

## Under the hood

- **`agents/.template/`** is the blueprint: `install.sh` mints your agent
  from it (default `~/.dogany/main`) as a fully self-contained copy --
  nothing in the instance points back at this repo, so it keeps working
  wherever it lives.
- **`skills/`** holds the built-in skills every agent starts with:
  reminders, memory search, scheduling, onboarding, and more.
- Life-tracking abilities (meals, appointments, relationships, ...) come
  from the separate LifeKit pack (repo `dogany-lifekit`), installed after
  mint -- ask your agent to enable them.
- Each agent's `bridge/` is a self-contained Telegram <-> Claude bridge
  built on the official `claude-agent-sdk`.

Contributor-level detail -- mint mechanics, hook wiring, memory routing,
pack gating -- is not documented separately; read the code.

## License

[Apache 2.0](LICENSE).
