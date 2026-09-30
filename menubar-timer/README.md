# menubar-timer

Countdown timer in the macOS menu bar (built with [rumps](https://github.com/jaredks/rumps)).

| Idle | Counting down | Menu |
|---|---|---|
| <img src="screenshots/idle.png" width="47" alt="Idle"> | <img src="screenshots/running.png" width="65" alt="Counting down"> | <img src="screenshots/menu.png" width="130" alt="Menu"> |

When time is up, it plays a sound and shows a notification.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```sh
.venv/bin/python timer.py
```
