# Hermis — Keka Attendance Automation

Deterministic macOS attendance automation for Keka using `launchd`, Playwright, a dedicated persistent browser profile, and an authorized Telegram control plane.

## Behavior

- Weekday Clock In and Clock Out schedules in `Asia/Kolkata`
- Bounded startup/grace-window retries
- Remote attendance-state checks and duplicate protection before mutations
- Persistent browser authentication without storing a Keka password
- DarkWake detection to avoid launching Chromium while the Mac is asleep
- Strict Telegram user/chat allowlist and fixed command routing
- Sanitized local audit records

## Security

Runtime data is intentionally excluded from Git:

- `browser-profile/` (cookies and authenticated session)
- `config.json` (tenant and Telegram IDs)
- `logs/` and `state/`
- `.venv/` and lock files

The Telegram bot token is stored in macOS Keychain and is never committed. Do not commit a browser profile, credentials, tokens, or generated state.

## Requirements

- macOS with a logged-in user session
- Python 3.11+
- Playwright Chromium
- A manually authenticated Keka session in the dedicated browser profile

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Create local configuration with:

```bash
bin/keka-attendance init
```

Then configure the tenant URL, exact selectors, Telegram numeric allowlist, and Keychain token locally. Production scheduling remains disabled until explicitly configured and enabled.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q keka_automation tests
```

## Important limitation

A user LaunchAgent cannot execute while the Mac is fully powered off. Startup catch-up is bounded and records the real execution time; it never fabricates or backfills attendance timestamps.
