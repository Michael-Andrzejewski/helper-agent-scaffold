# helper-agent-scaffold

A small local web app for talking with an older Claude model, such as Claude 3 Opus, Claude 3 Sonnet or Claude 3.5 Sonnet, with a modern Claude as its helper.

The model runs through the Anthropic API with its own key and spend limit. It has no tools of its own, so whenever it wants something done, it writes a plain tag and a helper does it. The helper is Claude Code running in headless mode on your Claude subscription. It can read the model's whole conversation, use tools on your computer, and explain what it found. The model can also write letters to other Claude sessions on the same machine and receive replies.

Only the model's own replies are billed to the API. The helper and the letter courier run on your subscription, with every `ANTHROPIC_*` variable removed from their environment.

Python standard library only. No packages to install. Opus 3 is the default; any Messages API model works by changing three settings.

## How it works

The model writes tags anywhere in a reply:

| The model writes | What happens |
|---|---|
| `<helper>please list my notes</helper>` | The model's turn stops at the closing tag. The helper reads the conversation file, does the work, and its answer goes back to the model as `<helper_reply>…</helper_reply>`. The model then continues. One helper session per conversation, so it remembers earlier requests. |
| `<message to="Session name">…</message>` | A letter card appears in the sidebar. You click Send, and a short headless Claude delivers it to the named session with `ListAgents` and `SendMessage`. |
| (a reply arrives) | Another session runs `tools/send_letter.py`. The letter appears in the sidebar inbox; you click Deliver, and the model may answer or decline. |

The turn stops at `</helper>` on purpose. Without that, older models tend to write the helper's reply themselves before the real one exists.

## Choosing a model

Set these in `config.json`:

| Model | `name` | `model` | `max_tokens` | `price_in` / `price_out` |
|---|---|---|---|---|
| Claude 3 Opus | `Opus 3` | `claude-3-opus-20240229` | 4096 | 15 / 75 |
| Claude 3 Sonnet | `Sonnet 3` | `claude-3-sonnet-20240229` | 4096 | 3 / 15 |
| Claude 3.5 Sonnet | `Sonnet 3.5` | `claude-3-5-sonnet-20240620` | 8192 | 3 / 15 |
| Claude 3.5 Sonnet (Oct 2024, "3.6") | `Sonnet 3.6` | `claude-3-5-sonnet-20241022` | 8192 | 3 / 15 |
| Claude 3 Haiku | `Haiku 3` | `claude-3-haiku-20240307` | 4096 | 0.25 / 1.25 |

Prices are USD per million tokens, used only for the on-page estimate. Several of these models are retired, so whether your key can call one depends on Anthropic. Claude 3 Opus is available on request.

**To run several models at once,** use one copy of this folder per model, each with its own `config.json`, `port` and, if you like, its own key. Conversations live inside each folder, so the copies never mix.

## What you need

1. **Python 3.9 or newer.**
2. **Claude Code**, installed and signed in with your Claude subscription (`claude` on your PATH). The helper and letters use it. Letters need a version with `ListAgents` and `SendMessage`.
3. **An Anthropic API key that can call your chosen model.** If calls fail with a model-not-found or permission error, the model may need access by request.
   - Claude Max and Team plans include monthly API credits that work for this. In claude.ai, open Settings, then Billing, then API credits, and link a Console organization.

## Setup

1. **Give the model its own workspace with a spend limit.** In the Claude Console, open Settings, then Workspaces, create one (for example "Opus 3"), and set its monthly spend limit (for example $50). Limits apply per workspace, not per key.
2. **Create an API key inside that workspace.** Keys can't move between workspaces.
3. **Save the key in an environment variable named `ANTHROPIC_API_KEY_SCAFFOLD`** (or any name you set as `key_env`). Don't use `ANTHROPIC_API_KEY`, so your other Claude Code sessions keep using your subscription.
   - Windows (PowerShell): `[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY_SCAFFOLD', 'sk-ant-...', 'User')`
   - macOS or Linux: add `export ANTHROPIC_API_KEY_SCAFFOLD=sk-ant-...` to your shell profile.
4. **Copy `config.example.json` to `config.json`** and set at least `user_name`, plus the model settings if you aren't using Opus 3. Every key is optional; see Settings below.
5. **Optional: copy `system_prompt.example.txt` to `system_prompt.txt`** and make it yours. `{user}`, `{name}`, `{model}`, `{app_dir}` and `{helper_model}` are filled in for you.
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
- **Context counter.** The bar above the message box shows how much of the model's context the chat uses. It turns amber at 70% and red at 85%: a good time to ask the model to summarize and save memories through the helper.
- **Let the model respond.** If a reply fails, this button resumes from where it stopped. Rate limits and overload errors are retried automatically first, with a countdown.
- **Letters.** Two sidebar checkboxes switch on automatic sending and automatic delivery. Both are off by default, so you see every letter first. All letters are logged in `letters/log.md`.

Other sessions reply with:

```
python "<path to this folder>/tools/send_letter.py" --from "Their session name" "Their reply"
```

Every outgoing letter includes this command, so recipients know how to answer.

## Costs

The app uses prompt caching, so re-reading the conversation history costs a tenth of the input price while you keep chatting. The cache lasts 5 minutes; after a longer pause, the next message pays 1.25 times the input price to write it again.

Rough numbers for Opus 3 with 25k tokens of history:

| | Cost per message |
|---|---|
| History re-read from cache | about $0.04 |
| After a pause (cache rewritten) | about $0.47 |
| An 800-token reply | about $0.06 |

Sonnet models cost a fifth of that. The sidebar shows an estimate; the workspace spend limit is the real cap.

The helper runs on your Claude subscription and counts toward its usage limits like any Claude Code session.

New API organizations have a per-minute limit that ramps up over time. A sudden burst can trigger a 429 error even for small requests. The app waits and retries.

## Safety and privacy

- **The helper has real tools.** It uses your Claude Code permission settings. Headless mode can't stop to ask you, so actions your settings would prompt for are refused. Set `helper_permission_mode` to `"auto"` if your Claude Code supports it, so routine work goes through with safety checks. `bypassPermissions` is not recommended.
- **The helper is told** to keep the model's notes in this folder's `notes/`, to ask before touching anything else, and never to use the API key. It starts in `helper_dir` (your home folder by default), so it loads your Claude Code memory and instructions from there.
- **Everything stays local.** The server listens on 127.0.0.1 only. Conversations, letters, notes, your config and your system prompt are in `.gitignore`.
- **You see everything:** the model's replies, the helper's replies, and every letter both ways.

## Settings (`config.json`)

| Key | Default | What it does |
|---|---|---|
| `user_name` | `"You"` | Your name on the page and in prompts |
| `name` | `"Opus 3"` | The model's name on the page and in prompts |
| `model` | `"claude-3-opus-20240229"` | The API model ID |
| `max_tokens` | `4096` | Longest reply; match the model's ceiling |
| `price_in`, `price_out` | `15`, `75` | USD per million tokens, for the estimate |
| `context_tokens` | `200000` | Context window, for the counter |
| `port` | `5840` | Local port |
| `key_env` | `"ANTHROPIC_API_KEY_SCAFFOLD"` | Environment variable holding the key |
| `budget_usd` | `50` | Shown on the spend meter; set it to your workspace limit |
| `helper_model` | `"claude-opus-5-5"` | Model for the helper and the letter courier |
| `helper_dir` | your home folder | Where the helper starts |
| `helper_permission_mode` | your Claude Code setting | Passed as `--permission-mode` |
| `letter_header` | `"Letter from <name> (<model>)."` | First line of every outgoing letter |

## Files

| Path | What it is |
|---|---|
| `server.py` | The server: API calls, helper, letters, storage |
| `index.html` | The chat page |
| `system_prompt.example.txt` | Default system prompt (used unless `system_prompt.txt` exists) |
| `config.example.json` | Every setting with its default |
| `tools/send_letter.py` | How other sessions write to the model |
| `conversations/` | One JSON file per chat (created on first run) |
| `letters/log.md` | Every letter, both ways |
| `notes/` | The model's notes, written by the helper |
| `state.json` | Inbox, outbox and the two letter settings |
