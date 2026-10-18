# flowtype

My own from-scratch version of [Wispr Flow](https://wisprflow.ai): voice dictation that types into whatever app you're in. Speech-to-text runs locally on the GPU with faster-whisper.

- **Press Super+Alt+D** to start listening. Talk, then **press it again**: the text is typed at your cursor and copied to the clipboard.
- AI polish: a local model through Ollama (qwen3:4b-instruct by default), or Claude, resolves self-corrections ("3, no wait, 4") and matches the app you're in: casual in chat apps, full sentences in email, exact commands in a terminal. Set `"polish": "off"` to use rules only.
- Cleanup: drops filler words (um, uh), collapses stutters, and turns "new line", "new paragraph", "period" and "comma" into the real thing.
- You can set your own dictionary and snippets (say "my email" to type your address) in `~/.config/flowtype/config.json`. Run `flowtype reload` after editing.
- `flowtype stats` shows words dictated, speaking pace and time saved. `flowtype history` lists past transcripts.

Runs as the `flowtype.service` systemd user service, which keeps the model loaded.

Website: https://neilyneilynig.github.io/flowtype/ (source in `docs/`).
