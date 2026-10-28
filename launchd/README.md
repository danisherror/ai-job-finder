# Schedule

Two LaunchAgents run as you (not root):

- `com.jobfinder.daily` — 6:45 AM: collect, match, send lists (and the shared lists to every bot subscriber).
  If the Mac was asleep at 6:45, it runs on wake.
- `com.jobfinder.bot` — always running: answers bot commands (/start, /latest, /search, /filters, ...) within
  a second and saves Applied/Skipped button taps. Restarted automatically if it stops. The bot only answers
  while this Mac is on and awake; messages sent meanwhile are answered when it wakes.

Logs go to `private/logs/`.

## Install

```sh
mkdir -p private/logs
cp launchd/com.jobfinder.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobfinder.daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobfinder.bot.plist
sudo pmset repeat wakeorpoweron MTWRFSU 06:40:00   # wake the Mac at 6:40 every day
```

Run one now to test: `launchctl kickstart gui/$(id -u)/com.jobfinder.daily`

Or run the bot in a terminal instead: `uv run python -m src.main bot` (Ctrl+C stops it).

After changing the command list, update Telegram's / menu once: `uv run python -m src.main commands`

**macOS privacy:** this project lives in `~/Desktop`, which macOS protects. If the log shows
"Operation not permitted", give `/opt/homebrew/bin/uv` Full Disk Access (System Settings → Privacy & Security),
or move the project out of Desktop and update the paths in both plists.

## Remove

```sh
launchctl bootout gui/$(id -u)/com.jobfinder.daily
launchctl bootout gui/$(id -u)/com.jobfinder.bot
rm ~/Library/LaunchAgents/com.jobfinder.*.plist
sudo pmset repeat cancel
```
