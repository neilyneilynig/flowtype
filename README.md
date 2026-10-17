# flowtype

My own from-scratch version of [Wispr Flow](https://wisprflow.ai): voice dictation that types into whatever app you're in. Speech-to-text runs locally on the GPU with faster-whisper.

- **Hold Super+Alt+D**, talk, then let go. The text is typed at your cursor and copied to the clipboard.
- **Super+Alt+Shift+D** works as a toggle instead: tap once to start, tap again to stop.
- Cleanup: drops filler words (um, uh), collapses stutters, and turns "new line", "new paragraph", "period" and "comma" into the real thing.
- You can set your own dictionary and snippets (say "my email" to type your address) in `~/.config/flowtype/config.json`. Run `flowtype reload` after editing.
- `flowtype stats` shows words dictated, speaking pace and time saved. `flowtype history` lists past transcripts.

Runs as the `flowtype.service` systemd user service, which keeps the model loaded.
