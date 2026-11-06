---
name: rex
description: Use the rex CLI to manage Rex sessions, open tabs, arrange panes, run commands, inspect terminal output, and control connected Rex apps.
version: "0.1.0"
---
<!-- Registered by the Hermes "rex" plugin. The guide itself lives in the installed Rex CLI. -->

Run `rex __internal skill` with the terminal tool and follow the guide it
prints. The guide comes from the installed Rex CLI, so it describes the
commands available in that version.

If the command fails, the installed Rex version predates the guide. Start with
`rex whoami --json`, then use `rex --help` and `rex COMMAND --help`.

Your session's own ids are in `REX_SESSION` and `REX_BLOCK` in the terminal
tool's environment. Pass `-s "$REX_SESSION"` and `-b "$REX_BLOCK"` to act on
your own session or pane; the user may be looking at a different one.
