# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A quantitative-trading learning project (`QuantLearning`). It is in a very early state: there is no README, dependency manifest, test suite, or build configuration yet. The `JoinQuant/` package (empty) signals an intent to work with JoinQuant (聚宽), a Chinese quantitative trading platform.

## Environment

- Python 3.14.6 installed via Homebrew (`/opt/homebrew/bin/python3`).
- No `python` alias exists on `PATH` — use `python3`.
- No virtualenv or package manager is configured yet (no `requirements.txt`, `pyproject.toml`, or `Pipfile`).

## Running Code

The only entry point is the PyCharm sample script:

```bash
python3 main.py
```

There are currently no lint, type-check, or test commands.

## Structure

- `main.py` — PyCharm's default sample script (`print_hi`); boilerplate, not project code.
- `JoinQuant/__init__.py` — empty package placeholder, the intended home for JoinQuant integration.
- `.idea/` — PyCharm IDE config (untracked).
