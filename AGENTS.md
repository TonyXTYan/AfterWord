# AGENTS.md

Notes for agents working in this repo. What AfterWord is and how it works:
[README.md](README.md), [DESIGN.md](DESIGN.md), [docs/](docs/). Where it goes
next: [roadmaps/](roadmaps/README.md).

## Versioning

One version for the whole repo (package, daemon, hooks, both plugins), in
`MAJOR.MINOR.PATCH` form.

- **Major** versions are kept for the large steps in
  [roadmaps/outlook.md](roadmaps/outlook.md), the ones that change what
  AfterWord is rather than add to it:
  - **1.0.0**: P0 (voice out) is done, meaning its "done when" in the outlook
    is met and the hook and daemon protocol is stable.
  - **2.0.0**: P1, the daemon becomes the context bus (new hook message
    version, new event-log schema).
  - Each later phase that changes the shape of the system (screen context,
    speech in, the suggester) gets the next major. Ordinary feature work never
    bumps the major.
- **Minor**: a new design generation or a large feature set (a roadmap
  milestone, a new engine, a GUI page), or a change to the event log schema
  or the hook protocol that older parts still understand.
- **Patch**: fixes, small polish, and small additions such as a new config
  setting with a default that keeps things working (0.3.2 added
  `[daemon] keep_days` as a patch).

The design docs' "v1 / v2 / v3" (`DESIGN.md`, `docs/design-v2.md`,
`docs/research-v3.md`) name design generations, not releases. They map to
0.1, 0.2 and 0.3. A future design-v4 would land as 0.4 (or as 1.0 if it
completes P0), not as 4.0.0.

### Where the version lives

Bump all of these together; `tests/test_version.py` checks they agree:

- `pyproject.toml` (`[project] version`), then run `uv lock` so `uv.lock`
  follows;
- `afterword/__init__.py` (`__version__`, shown by `afterword --version` and
  the daemon's status);
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and
  `.codex-plugin/plugin.json` (`version`). With a version set, an installed
  plugin may only pick up changes when the version changes, so anything that
  changes hooks or commands must come with a bump.

### Releasing

1. Add the new version's entry to [CHANGELOG.md](CHANGELOG.md).
2. Bump the version in the files above, in the same pull request as the
   change it describes (or a small one right after).
3. After the merge to `main`, tag the merge commit `vX.Y.Z`.
