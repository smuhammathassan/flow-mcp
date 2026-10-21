# flow-mcp

An MCP server that drives [Google Flow](https://flow.google.com) from an agent such as Claude Code. It runs Flow in headless Chromium with your own Google session cookies, so an agent can create projects, submit video prompts, wait for them, download the clips and stitch them into one video.

This is browser automation of a web app with no public API. It breaks when Flow's interface changes, and Google may sign the session out at any time.

## Tools

| Tool | What it does | Spends credits |
|---|---|---|
| `flow_status` | Checks the saved session is logged in; returns account and plan | no |
| `flow_set_cookies` | Saves a fresh cookie export and verifies it logs in | no |
| `flow_list_projects` | Lists project ids and URLs | no |
| `flow_create_project` | Creates a project, optionally named | no |
| `flow_generate` | Submits text-to-video prompts with aspect ratio, duration, resolution, model and outputs per prompt. `dry_run=true` returns the credit cost without submitting | **yes** |
| `flow_list_media` | Lists a project's media with state (generating %, done, failed) | no |
| `flow_wait` | Polls until nothing in the project is generating | no |
| `flow_download` | Downloads finished videos; names them `clipN.mp4` when a prompt starts with `Clip N of M` | no |
| `flow_stitch` | Joins videos end to end with ffmpeg | no |

## Setup

Requires Python 3.10+ and ffmpeg (for `flow_stitch`).

```bash
python3 -m venv ~/.venvs/flow
~/.venvs/flow/bin/pip install -r requirements.txt
~/.venvs/flow/bin/python -m playwright install --with-deps chromium
```

Register it with Claude Code for all projects:

```bash
claude mcp add google-flow --scope user -- ~/.venvs/flow/bin/python /path/to/flow-mcp/server.py
```

## Cookies

Export your cookies for `flow.google.com` while signed in (for example with a Cookie-Editor browser extension, as JSON), then ask the agent to call `flow_set_cookies` with them. They are saved to `~/.config/flow-mcp/cookies.json` with mode 600. Set `FLOW_MCP_HOME` to use another directory.

The cookies give full access to your Google account. Keep the jar private and never commit it.

After each successful run the server writes back the cookies Google rotated during that run. Reusing stale rotated cookies is a likely cause of early sign-outs, though Google can still end the session for other reasons. When a tool reports that the session expired, export fresh cookies and call `flow_set_cookies` again.

## Typical loop

`flow_status` → `flow_create_project` → `flow_generate` (dry run, then real) → `flow_wait` → `flow_download` → `flow_stitch`

## Tested against

Flow web UI as of October 2026 with Omni 1.1 Flash: 9:16, 720p, 10 s clips cost 15 credits each.
