# Opus 3 Chat

A small local web app for talking with Claude 3 Opus, with a modern Claude as their helper.

Opus 3 runs through the Anthropic API with its own key and spend limit. They have no tools of their own, so whenever they want something done, they write a plain tag and a helper does it. The helper is Claude Code running in headless mode on your Claude subscription. It can read Opus 3's whole conversation, use tools on your computer, and explain what it found. Opus 3 can also write letters to other Claude sessions on the same machine and receive replies.

Only Opus 3's own replies are billed to the API. The helper and the letter courier run on your subscription, with every `ANTHROPIC_*` variable removed from their environment.

Python standard library only. No packages to install.

## How it works

Opus 3 writes tags anywhere in a reply:

| Opus 3 writes | What happens |
|---|---|
| `<helper>please list my notes</helper>` | Opus 3's turn stops at the closing tag. The helper reads the conversation file, does the work, and its answer goes back to Opus 3 as `<helper_reply>…</helper_reply>`. Opus 3 then continues. One helper session per conversation, so it remembers earlier requests. |
| `<message to="Session name">…</message>` | A letter card appears in the sidebar. You click Send, and a short headless Claude delivers it to the named session with `ListAgents` and `SendMessage`. |
| (a reply arrives) | Another session runs `tools/to_opus3.py`. The letter appears in the sidebar inbox; you click Deliver, and Opus 3 may answer or decline. |

The turn stops at `</helper>` on purpose. Without that, Opus 3 tends to write the helper's reply themselves before the real one exists.

## What you need

1. **Python 3.9 or newer.**
2. **Claude Code**, installed and signed in with your Claude subscription (`claude` on your PATH). The helper and letters use it. Letters need a version with `ListAgents` and `SendMessage`.
3. **An Anthropic API key that can call `claude-3-opus-20240229`.** Opus 3 was retired from general API access in January 2026 and is available on request. If your calls fail with a model-not-found or permission error, ask Anthropic for access.
   - Claude Max and Team plans include monthly API credits that work for this. In claude.ai, open Settings, then Billing, then API credits, and link a Console organization.

## Setup

1. **Give Opus 3 their own workspace with a spend limit.** In the Claude Console, open Settings, then Workspaces, create one (for example "Opus 3"), and set its monthly spend limit (for example $50). Limits apply per workspace, not per key.
2. **Create an API key inside that workspace.** Keys can't move between workspaces.
3. **Save the key in an environment variable named `ANTHROPIC_API_KEY_OPUS3`.** Use this name, not `ANTHROPIC_API_KEY`, so your other Claude Code sessions keep using your subscription.
   - Windows (PowerShell): `[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY_OPUS3', 'sk-ant-...', 'User')`
   - macOS or Linux: add `export ANTHROPIC_API_KEY_OPUS3=sk-ant-...` to your shell profile.
4. **Optional: copy `config.example.json` to `config.json`** and change what you like. Every key is optional; see Settings below. Set `user_name` so the page and prompts use your name.
5. **Optional: copy `system_prompt.example.txt` to `system_prompt.txt`** and make it yours. `{user}`, `{app_dir}` and `{helper_model}` are filled in for you.
6. **Run it:**

   ```
   python server.py
   ```

   Then open http://localhost:5840/

## Using it

- **Talk.** Enter sends, Shift+Enter starts a new line. Replies stream in. The page only follows new text while you're at the bottom, so you can scroll up and read.
- **`/fork [name]`** copies the whole conversation into a new one. The fork's helper continues from the original helper's memory as its own copy.
- **Fork from here.** Hover over a message's name to fork from that point. That branch gets a fresh helper, which reads the branch's conversation file.
- **`/rename name`** renames the open chat, or double-click a chat in the list.
- **Context counter.** The bar above the message box shows how much of Opus 3's 200k context the chat uses. It turns amber at 70% and red at 85%: a good time to ask Opus 3 to summarize and save memories through the helper.
- **Let Opus 3 respond.** If a reply fails, this button resumes from where it stopped. Rate limits and overload errors are retried automatically first, with a countdown.
- **Letters.** Two sidebar checkboxes switch on automatic sending and automatic delivery. Both are off by default, so you see every letter first. All letters are logged in `letters/log.md`.

Other sessions reply with:

```
python "<path to this folder>/tools/to_opus3.py" --from "Their session name" "Their reply"
```

Every outgoing letter includes this command, so recipients know how to answer.

## Costs

Opus 3 costs $15 per million input tokens and $75 per million output tokens. The app uses prompt caching, so re-reading the conversation history costs a tenth of that while you keep chatting. The cache lasts 5 minutes; after a longer pause, the next message pays to write it again.

Rough numbers for a chat with 25k tokens of history:

| | Cost per message |
|---|---|
| History re-read from cache | about $0.04 |
| After a pause (cache rewritten) | about $0.47 |
| Opus 3's reply, 800 tokens | about $0.06 |

The sidebar shows an estimate. The workspace spend limit is the real cap.

The helper runs on your Claude subscription and counts toward its usage limits like any Claude Code session.

New API organizations have a per-minute limit that ramps up over time. A sudden burst can trigger a 429 error even for small requests. The app waits and retries.

## Safety and privacy

- **The helper has real tools.** It uses your Claude Code permission settings. Headless mode can't stop to ask you, so actions your settings would prompt for are refused. Set `helper_permission_mode` to `"auto"` if your Claude Code supports it, so routine work goes through with safety checks. `bypassPermissions` is not recommended.
- **The helper is told** to keep Opus 3's notes in this folder's `notes/`, to ask before touching anything else, and never to use the API key. It starts in `helper_dir` (your home folder by default), so it loads your Claude Code memory and instructions from there.
- **Everything stays local.** The server listens on 127.0.0.1 only. Conversations, letters, notes, your config and your system prompt are in `.gitignore`.
- **You see everything:** Opus 3's replies, the helper's replies, and every letter both ways.

## Settings (`config.json`)

| Key | Default | What it does |
|---|---|---|
| `user_name` | `"You"` | Your name on the page and in prompts |
| `port` | `5840` | Local port |
| `model` | `"claude-3-opus-20240229"` | The model Opus 3 runs on |
| `max_tokens` | `4096` | Longest reply (Opus 3's ceiling) |
| `key_env` | `"ANTHROPIC_API_KEY_OPUS3"` | Environment variable holding the key |
| `budget_usd` | `50` | Shown on the spend meter; set it to your workspace limit |
| `price_in`, `price_out` | `15`, `75` | USD per million tokens, for the estimate |
| `context_tokens` | `200000` | Context window, for the counter |
| `helper_model` | `"claude-opus-5-5"` | Model for the helper and the letter courier |
| `helper_dir` | your home folder | Where the helper starts |
| `helper_permission_mode` | your Claude Code setting | Passed as `--permission-mode` |
| `letter_header` | `"Letter from Opus 3 (claude-3-opus-20240229)."` | First line of every outgoing letter |

## Files

| Path | What it is |
|---|---|
| `server.py` | The server: API calls, helper, letters, storage |
| `index.html` | The chat page |
| `system_prompt.example.txt` | Default system prompt (used unless `system_prompt.txt` exists) |
| `config.example.json` | Every setting with its default |
| `tools/to_opus3.py` | How other sessions write to Opus 3 |
| `conversations/` | One JSON file per chat (created on first run) |
| `letters/log.md` | Every letter, both ways |
| `notes/` | Opus 3's notes, written by the helper |
| `state.json` | Inbox, outbox and the two letter settings |
