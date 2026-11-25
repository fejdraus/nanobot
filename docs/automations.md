# Automations

<!-- Meta description: Create, run, and manage nanobot scheduled automations, local triggers, and heartbeat-backed background checks. -->

Automations let nanobot run tasks on its own—reminders, daily summaries, CI follow-ups, local script reports, or quiet background checks.

Always create an automation from the WebUI topic or chat channel where you want its replies to appear. Keep `nanobot gateway` running (your WebUI or TUI already runs one; use `nanobot gateway --background` to keep it alive after closing the terminal).

## Choose an Automation Type

| Type | Starts from | Best for | How to create |
|---|---|---|---|
| **Scheduled automation** | A time, interval, or cron schedule | Reminders, daily reports, one-time future tasks | Ask nanobot in the target chat |
| **Local trigger** | Running `nanobot trigger ...` from a script or webhook | CI failures, local scripts, external event alerts | Send `/trigger <name>` in the target chat |
| **Heartbeat** | Built-in periodic check | Quiet background monitoring that only messages you when action is needed | Edit `<workspace>/HEARTBEAT.md` |

## Scheduled Automations

To create a scheduled task, just ask nanobot in the chat where you want the results delivered:

```text
Every weekday at 9am, check open pull requests and summarize blockers here.
```

```text
Tomorrow at 4pm, remind me to send the release notes.
```

- **Schedules:** Supports one-time dates, recurring intervals, and cron expressions (with optional IANA timezones like `America/Vancouver`).
- **Delivery:** By default, nanobot sends the final reply back to the chat where the task was created—you do not need to mention a chat ID or channel name. If you want a task to send to additional chats or recipients, say so explicitly in the instructions.
- **Edit, pause, or inspect runs:** You can manage tasks right in chat—for example, *"Move my daily report from 9am to 10am"*, *"Pause my daily report"*, or *"Show the last five runs of my daily report"*—or use the WebUI **Automations** view.

### Change the chat for a scheduled task

To move a scheduled task to a different chat:

1. Open the task in WebUI **Automations**.
2. Pick a new destination under **Run and reply in** (chat-app destinations appear once their channel is running and nanobot has received a message there).
3. Review the task instructions—click **Edit instructions** if you need to update any hardcoded destination text.
4. Click **Confirm change** (or **Save and change**).

Future runs will use the new chat's history and reply there. Previous messages stay in the original chat. Note that members of the new chat will see future results, and moving a task does not isolate workspace files or shared memory.

## Local Triggers

Local triggers let a shell script, CI job, or external webhook wake nanobot inside a specific chat.

1. In the target chat or WebUI topic, create a trigger:

```text
/trigger PR review
```

2. nanobot replies with a trigger ID and ready-to-use command:

```bash
nanobot trigger trg_8K4P2Q9X "Review PR #4502"
```

3. Run that command whenever an event happens, or pipe longer output through stdin:

```bash
generate-report | nanobot trigger trg_8K4P2Q9X
```

If you run multiple nanobot instances, pass the same `--config` or `--workspace` (your configured agent workspace) used by that gateway:

```bash
nanobot trigger --workspace ./bot-a/workspace trg_8K4P2Q9X "Nightly report"
```

nanobot ships one built-in webhook receiver: the [`gitlab_review`
channel](./configuration.md#gitlab-review-channel), which turns merge-request
activity into review turns. For other systems (GitHub, CI, any HTTP source),
run your own small webhook service and have it call `nanobot trigger` after it
builds the final message.

### Claude Code Hooks

A Claude Code hook is a shell command that Claude Code runs itself, so it needs
no nanobot code at all — it is just another local script calling
`nanobot trigger`.

Create the trigger first with `/trigger <name>` in the target chat, then point a
Claude Code hook at it. In `settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "nanobot trigger trg_8K4P2Q9X \"Claude Code finished a task\""
          }
        ]
      }
    ]
  }
}
```

Claude Code passes the hook payload to the command as JSON on stdin, so a hook
that should send real content reads it rather than hardcoding a message:

```python
#!/usr/bin/env python
"""Claude Code hook: forward a session summary into a nanobot session."""

import json
import subprocess
import sys

payload = json.load(sys.stdin)
summary = f"Claude Code ({payload.get('session_id', '?')}) stopped."

subprocess.run(
    ["nanobot", "trigger", "trg_8K4P2Q9X", summary],
    check=False,
)
```

Two properties of the local trigger queue make this reliable:

- The gateway drains the queue every 0.5s, so the message arrives while the
  session is running, without polling.
- If the linked topic is already running a turn, the message waits until the
  session is idle instead of being injected mid-turn.

Delivery is at-least-once: a hook that may run twice should produce a message
that is safe to see twice. If the gateway is not running, the message waits in
the workspace until it starts.


## Heartbeat

Use **Heartbeat** for recurring checks that should stay silent unless something needs your attention—for example, watching a repo for failures or scanning an inbox for urgent messages. (Use a scheduled automation instead when you want a reminder at a specific time every day.)

Edit `~/.nanobot/workspace/HEARTBEAT.md` (or ask nanobot to add a periodic check):

```markdown
## Active Tasks

- Check weather forecast and notify me only if storms are expected
- Scan inbox for urgent emails and notify me if any are found
```

Heartbeat is enabled by default while `nanobot gateway` is running and sends actionable results to your most recently active chat. Configure its interval or disable it in [`configuration.md#gateway-heartbeat`](./configuration.md#gateway-heartbeat).

## Manage Automations in WebUI

Open **Automations** in the WebUI to:

- Filter by all, active, paused, needs-attention, or system jobs.
- Search by name, instructions, chat, schedule, or status (e.g. `chat:WeChat`, `status:paused`).
- Run a scheduled automation immediately, pause/resume it, rename it, or delete it.
- Copy the `nanobot trigger ...` command for a local trigger.

Protected system jobs (such as Heartbeat and Dream) appear as read-only.

## Delivery and Reliability

Keep these behaviors in mind when relying on automations:

- **Missed schedules after downtime:** If the gateway was stopped during a scheduled time, nanobot does **not** replay missed runs when it restarts. Recurring schedules simply wait for their next future run; one-time tasks in the past will not run automatically.
- **Exact clock times:** Use a cron expression (not an interval) when exact wall-clock timing matters, since intervals recalculate after a run finishes.
- **Retry on channel failure:** If a chat app is temporarily unreachable before a scheduled reply is sent, the gateway automatically retries for up to one hour (without re-running the model). If a send times out mid-flight, nanobot will not auto-retry to avoid duplicate messages—check the destination first.
- **Local triggers:** Trigger messages wait in a durable workspace queue if the gateway is offline or if the target chat is busy. Delivery is **at-least-once** (a restart mid-run can replay a trigger), so external scripts should tolerate duplicate messages safely.

## Troubleshooting

- **Automation never runs:** Confirm `nanobot gateway` is running, the automation is enabled, and it is linked to a chat.
- **Local trigger sits waiting:** Make sure `nanobot trigger` uses the same `--config` or `--workspace` as the running gateway.
- **Scheduled message did not arrive:** Check the task's run history in **Automations** and `nanobot gateway logs` for channel errors.

## Related Docs

- [WebUI Automations](./webui.md#automations)
- [In-Chat Commands: Local Triggers](./chat-commands.md#local-triggers)
- [CLI Reference: Local Triggers](./cli-reference.md#local-triggers)
- [Configuration: Heartbeat](./configuration.md#gateway-heartbeat)
- [Long-Running AI Agent Guide](./guides/long-running-ai-agent.md)
