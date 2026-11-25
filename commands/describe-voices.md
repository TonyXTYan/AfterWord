---
description: Write one-line descriptions for AfterWord's voice profiles, so /afterword:voice pick can choose well
argument-hint: "[profile …]"
disable-model-invocation: true
---
<!-- afterword:describe-voices-command -->
The user ran AfterWord's describe-voices command with: `$ARGUMENTS`.

The descriptions are what `/afterword:voice pick` shows you when you choose a
voice for a session, so they should say how a voice sounds and what it suits.

1. Run `uv run afterword voices --json` (or `afterword voices --json` if it is
   installed) to see the profiles: name, voice id, lang, style, persona and
   any description.
2. Work on the profiles named in `$ARGUMENTS`; if it is empty, on every
   profile without a description. For each, write a one-line description of
   at most 80 characters in plain words, from its voice id, style, persona and
   language, for example "calm British narrator for long explanations". Do
   not invent traits the profile does not suggest.
3. Show the proposals to the user as a short list (name: description) and ask
   which to keep or change. Write nothing before they answer.
4. For each one they keep, run
   `afterword voices describe <name> "<text>"` (prefix with `uv run` the same
   way as above). It edits `config.toml` and keeps its comments.
5. Finish with `afterword voices` and say in one line what changed.

Use no other tools than running these commands.
