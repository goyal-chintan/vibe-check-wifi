# Vibe Check WiFi

Vibe Check WiFi is a macOS CLI that checks if your network is ready for meetings and video calls.

It evaluates real-time call quality using:
- Latency
- Jitter
- Packet loss
- DNS responsiveness
- Route stability
- Local Wi-Fi signal quality
- Latency under light load

## What It Helps With

- Audio call readiness
- Video call readiness
- Video + screen-share readiness
- Live monitoring during active calls

## Requirements

- macOS
- Python 3.10+

## Installation

```bash
git clone https://github.com/<your-username>/wifi-call-health.git
cd wifi-call-health
python3 -m pip install --user rich
```

## Run

```bash
python3 vibe_check_wifi.py
```

The app runs as a terminal UI with live rolling readiness metrics, animated theme-specific layouts, and a final results screen.

## Menu Options

1. Quick Check (1-2 min)
2. Meeting Check (10-15 min)
3. Live Monitor
4. Choose Call Profile
5. Choose Theme (`Studio Board`, `Signal Radar`, `Vibe Arcade`)
6. Run Optional Speed Test
7. View Recent History
8. Exit

`Studio Board` is the default theme. Theme preference is stored locally in `.vibe-check-wifi.json`.
The three themes are intentionally different in personality and layout.

## Output Includes

Each report includes:
- Overall verdict (`PASS`, `WARN`, `FAIL`)
- Profile verdict
- Top detected problems
- Detailed metrics
- Actionable next steps
- Optional speed-test result based on staged download samples (more stable than a single short transfer)

Run history is appended to:

```text
./history.log
```

## Privacy and Public Repo Safety

- No cloud account is required.
- Results are stored locally in `history.log`.
- Runtime artifacts are ignored via `.gitignore` (`history.log`, caches).
- Before publishing, verify that `history.log` is not staged.

## Tests

```bash
cd wifi-call-health
python3 -m pytest -q
```

## Roadmap Ideas

- Export reports as JSON/CSV
- Optional desktop notifications for live monitor alerts
- Cross-platform support
