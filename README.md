# dotfiles

My personal terminal / editor / shell config. Tracked here so a fresh machine
(or a fresh remote) is one `git clone` and one `dotfiles init` away from the
setup I actually use.

## My stack

What I actually use day-to-day, and where to look in the repo.

- **[Ghostty](https://ghostty.org/)** — terminal. Config: `.config/ghostty/config`.
  Replaced iTerm2 for me. See [Ghostty + Zellij](#ghostty--zellij) for the keymap.
- **[Zellij](https://zellij.dev/)** — terminal multiplexer (the tmux successor I
  stuck with). Config: `.config/zellij/`. Helpers: `bin/z` (session attach/create
  wrapper — I always run `z` on shell start), `bin/tn` (auto-renames the
  active tab from the running command — `claude`, `codex`, `ssh <host>`, or
  cwd. Also doubles as a manual rename: `tn my-tab`).
- **[Zsh](https://www.zsh.org/) + [oh-my-zsh](https://ohmyz.sh/)** — shell.
  Entry: `.zshrc`. Cross-shell config: `bin/commonrc` (also sourced from
  `.bashrc`). `~/.zshrc` is a real (untracked) stub that sources this tracked
  `.zshrc`, so installers appending to it don't dirty the repo; deliberate
  per-machine config goes in `~/.zshrc.local`, sourced at the end. What's custom
  on top of stock oh-my-zsh:
    - Custom two-line prompt `intheloop` (in `bin/commonrc-post`):
      `[user@host] cwd (git)` on the left; venv, gcloud config, k8s context,
      last-command time, exit status on the right.
    - Auto-activate `.venv/` on `cd` (and deactivate when leaving the dir tree).
    - AWS profile and gcloud config persisted across shells in `~/.aws-profile`
      and `~/.gcloud-profile`.
    - Lazy `fnm` setup, plus a small `_lazy_load` helper for slow init scripts.
    - Aliases: `k=kubectl`, `ll=ls -lh`, `pnpm`/`bun` wrapped through `ding`.
- **Claude Code / Codex** — coding agents. `claude` and `codex` are profile
  pickers in `bin/commonrc`: use `--profile <name>` / `-P <name>`,
  `CLAUDE_PROFILE` / `CODEX_PROFILE`, or the interactive picker. Codex keeps
  each profile in an isolated `CODEX_HOME` under `~/.codex-profiles/`.
  `dotfiles claude-hooks` wires `bin/cc-hook` into every Claude profile:
  the zellij tab reads `🧠 · <session>` (icon: `CC_TAB_ICON`) (the `/rename` title, else Claude's auto
  title), gets a `●` when Claude waits on you in a background tab, and desktop
  notifications show the session plus an excerpt of Claude's question or reply,
  with the Claude icon (a renamed terminal-notifier copy in `~/Applications`).
  Clicking one jumps to the pane. Needs zellij ≥ 0.44 (`rename-tab --tab-id`).
- **[Git](https://git-scm.com/)** — `.gitconfig` (identity, LFS, Kaleidoscope
  diff/merge), `.gitignore_global`, and `.config/git/ignore`. Machine paths use
  `~` so the same config works on Linux remotes.

## Other tools

Small helpers in `bin/`:

- `ding` — runs a command and sends an OS notification if it took longer than
  5s. `bun` and `pnpm` are aliased through it so long installs nudge me.
  `ding` with no args fires a test notification.
- `selectors` — fzf-driven pickers, sourced from `commonrc`:
  `ap` (AWS profile), `gp` (gcloud config), `kk` (kubectl context),
  `kn` (kubectl namespace).
- `idea`, `mate` — open files from the shell in IntelliJ / VS Code.
- `dff` — render a `git diff` as a Pierre HTML page and open it in the browser.
  Works standalone (`dff origin/main...HEAD`) or as `GIT_EXTERNAL_DIFF=dff git diff`.
  Zero-install: resolved via bun auto-install. Needs [bun](https://bun.sh/).
- `mcp-view` — point it at an MCP server URL and get every tool, prompt and
  resource as JSON, schemas and all (`mcp-view https://host/mcp | jq`), or add
  `--ui` for a Textual browser that expands each schema property by property and
  runs tools and prompts on Enter, seeding the arguments from their schema.
  Runs the OAuth flow in the browser on first use and caches the tokens under
  `~/.cache/mcp-view`; `--show-auth` prints those credentials with expiries
  resolved, `--reauth` starts over. Also speaks `-t sse` and `-t stdio`.
  Zero-install: a `uv` script with inline dependencies. Needs [uv](https://docs.astral.sh/uv/).
- `wifi-check` — explain and grade the current macOS Wi-Fi link (RSSI, SNR, MCS,
  channel busy) from `wdutil info`. Run with `sudo`; `--channels` scans nearby
  networks via CoreWLAN.
- `dotfiles` — install + deploy script (`dotfiles init`, `dotfiles deploy <host>`).

## Install

```sh
git clone https://github.com/vklimontovich/dotfiles ~/dotfiles
~/dotfiles/dotfiles init
```

`dotfiles init` symlinks everything into `$HOME` (idempotent — re-run any time;
unrelated existing files get backed up with a timestamp suffix).

To push the same setup to a remote machine:

```sh
~/dotfiles/dotfiles deploy <host>          # rsyncs + runs `dotfiles init` remotely
~/dotfiles/dotfiles deploy <host> --no-keys # skip ssh key copy
```

The deploy refuses to overwrite a remote dotfiles checkout that has uncommitted
changes; pass `--ignore-changes` to force.

## Agent skills and commands

Every agent CLI reads skills, instructions and slash commands from its own
config dir, and I run several profiles of each (`~/.claude-profiles/*`,
`~/.codex-profiles/*`, `~/.gemini`). One repo feeds all of them:
[`~/agents`](https://github.com/vklimontovich/agents). One command syncs it:

```sh
~/dotfiles/dotfiles agents-sync            # update, install, link everything
~/dotfiles/dotfiles agents-sync --no-fetch # relink only, no download
~/dotfiles/dotfiles agents-sync --list     # what is installed, and from where
```

`agents-sync` does four things:

1. clones or updates `~/agents`;
2. points every profile's `CLAUDE.md` at `~/agents/AGENTS.md`;
3. downloads each entry of `~/agents/skills.yaml` with
   [`npx skills`](https://github.com/vercel-labs/skills) into
   `~/agents/skills/<name>` and symlinks that dir into every target's `skills/`;
4. symlinks every `~/agents/commands/<name>.md` into each Claude profile's
   `commands/`, so `/name` works in all of them.

It also removes skills and links that `skills.yaml` no longer asks for. It is
idempotent — run it any time.

Commands go to Claude profiles only, because the format is Claude Code's:
frontmatter plus a !`shell` block. For example `~/agents/commands/idea.md`
opens the working directory in IntelliJ IDEA.

`~/agents/skills` is the source of truth. A skill you write there by hand is
tracked in git and linked like the rest; sync never touches it. Skills that come
from `skills.yaml` are gitignored, because sync can fetch them again.

One entry per repo, not per skill:

```yaml
skills:
  - source: vercel-labs/agent-skills       # owner/repo on GitHub
    select: [web-design-guidelines]        # omit for every skill in the repo
    targets: [claude:pp]                   # optional, narrows the targets
```

The default target lists are `SKILLS_DEFAULT_TARGETS` and
`COMMANDS_DEFAULT_TARGETS` in the `dotfiles` script, not the yaml. Tokens: `claude` and `codex` mean every profile of that CLI plus its
native home, `claude:<name>` / `codex:<name>` mean one profile, `gemini` is
`~/.gemini`, `agents` is the legacy `~/.agents`, and any path links into
`<path>/skills`.

## Ghostty + Zellij

I run [Ghostty](https://ghostty.org/) as the terminal and
[Zellij](https://zellij.dev/) as the multiplexer. Ghostty owns the GUI keys
(window, copy/paste); Zellij owns tabs / panes / sessions.

**The trick:** Ghostty rewrites `cmd+<key>` into `ESC <key>` (i.e. `Alt+<key>`)
before sending to the terminal. Zellij is bound to `Alt+<key>`. So Mac muscle
memory works *and* the same shortcuts work over SSH on a Linux box where there
is no `cmd` key — you just press `Alt+<key>` directly.

### Cheatsheet — only the keys I override

Both columns trigger the same Zellij action. Use `cmd+` on the Mac host, `alt+`
when connected over SSH or on Linux directly.

| macOS (Ghostty) | Remote / Linux (any term) | Action                                |
| --------------- | ------------------------- | ------------------------------------- |
| `cmd+t`         | `alt+t`                   | new tab                               |
| `cmd+w`         | `alt+x`                   | close current pane                    |
| `cmd+opt+left`  | `alt+[`                   | previous tab                          |
| `cmd+opt+right` | `alt+]`                   | next tab                              |
| `cmd+c`         | `alt+c`                   | copy current selection                |
| —               | `alt+n`                   | new pane                              |
| —               | `alt+R` / `alt+D`         | split pane right / down               |
| —               | `alt+/`                   | toggle floating pane                  |
| —               | `alt+,`                   | rename current tab (also see `tn` )   |
| —               | `alt+d`                   | detach session (zellij keeps running) |
| —               | `alt+h/j/k/l` or arrows   | focus pane left/down/up/right         |
| —               | `alt+i` / `alt+o`         | move tab left / right                 |

Other Ghostty keymap rewrites (Mac-style line editing inside the terminal):

| Key                    | Sends                        | Effect                                                       |
| ---------------------- | ---------------------------- | ------------------------------------------------------------ |
| `cmd+left/right`       | `^A` / `^E`                  | start / end of line                                          |
| `cmd+backspace`        | `^U`                         | delete to start of line                                      |
| `opt+backspace`        | `ESC ^?`                     | delete previous word                                         |
| `cmd+shift+left/right` | `Shift+Home` / `Shift+End`   | line selection (nvim, micro)                                 |
| `opt+shift+left/right` | word selection               | word selection (nvim, micro)                                 |
| `shift+enter`          | `LF`                         | newline in input (vs submit) — used by Claude Code, readline |

Standard Zellij defaults (`ctrl+p` → pane mode, `ctrl+t` → tab mode, etc.) are
unchanged; see `.config/zellij/config.kdl` for the full bindings.
