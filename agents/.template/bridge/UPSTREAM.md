# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: 7b17aee795d39c7f1f1933a704c0c37d81de61e0

## Why vendored instead of a git submodule

A submodule would leave `bridge/` empty on a plain `git clone` (without
`--recursive`), which breaks the "self-contained, runs when cloned standalone"
goal of this repo. The bridge is therefore vendored. Submodule wiring is
deferred; if this repo later wants the bridge as a submodule, remove this
directory and run:

    git submodule add https://github.com/coolcoolk/claude-code-telegram bridge
    cd bridge && git checkout 7b17aee795d39c7f1f1933a704c0c37d81de61e0

To refresh the vendored copy from upstream, re-copy the upstream tree over this
directory (excluding any real `.env`, `venv/`, `__pycache__/`).
