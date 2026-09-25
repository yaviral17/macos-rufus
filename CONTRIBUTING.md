# Contributing to macos-rufus

Thanks for helping out! This project writes bootable Windows USB drives, so
correctness and safety matter more than speed.

## Ground rules

- Be kind — see the [Code of Conduct](CODE_OF_CONDUCT.md).
- For security problems, do **not** open a public issue; see [SECURITY.md](SECURITY.md).
- For big changes, open an issue first so we can agree on the approach.

## Development setup

```bash
git clone https://github.com/yaviral17/macos-rufus.git
cd macos-rufus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires macOS 12+ and Python 3.11+.

## Running the tests

```bash
python -m pytest tests/
```

Tests must not touch real disks. Anything that would write to a device should
be mocked.

> ⚠️ When testing manually, always use a spare USB drive. The tool erases the
> target disk.

## Making a change

1. Fork the repo and create a branch from `main`
   (`feature/…`, `fix/…`, `docs/…`).
2. Keep changes focused — one concern per pull request.
3. Add or update tests when you change behavior.
4. Update the README if user-facing behavior changes.
5. Open a pull request and fill in the template.

## Commit messages

Short imperative summary, with a prefix such as `feat:`, `fix:`, `docs:`,
`test:` or `chore:`.

## Reporting bugs

Use the bug report template. Include your macOS version, Python version, the
Windows ISO version, and the terminal output.
