# Python Scripts

When running python code use uv as a runner with declaring dependencies in script itself as 

# /// script
# dependencies = [
#   ...
# ]
# ///

Prefer standard packages to external dependencies

# Bash commands

When telling me bash commands try not to use line breaks an fit 
into single line unless it's more that 100 chars

# Git

If you work in folder with git repo, use git mv / git rm / git add when working with files.

When asked to generate a commit messages, or generating commit message by other means please follow those rules, 
unless there're specific project based rules

 - Use "conventional commits" convensions
 - If you can infere issue id from context - branch name, etc - add it to commit message too
 - Use my writing style (see below)
 - Try to make commits short and straight to the point. NEVER add redundant information (such as list of changed files) to commit message. If something 
 can be inferred from from commit itself, don't do it

 - IMPORTANT: NEVER mention Claude in the commit message
 - IMPORTANT: don't commit changes unles explicitely asked

## Bun / PNPM etc

Never run bunx, npmx, pnpnx etc. Run `bun <tool>`, `pnpm <tool>` or similar (dependind on project package manager). Install `<tool>` as dev dependency if needed.
 
# Formatting

When writing text files like MD or MDX (README, documentation etc), make sure to keep lines under 120 chars if possible. This is IMPORTANT, I can't
read code otherwise

# My writing style

See [writing-style.md](docs/writing-style.md) for detailed guidelines.

Summary: concise, simple words, short sentences, no metaphors. Reference: Paul Graham essays.

# Browser Usage

If you have playwright MCP prefere it over Chrome usage. Run playwright in headless mode so the window won't open.
The only reason to use Chrome would be if you should rely on pre-existing login session that exists in Chrome. In this case,
double check with me via question tool and always tell where and how I should login
