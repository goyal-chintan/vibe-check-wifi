# Vibe Check WiFi

Interactive macOS CLI to assess whether your network is stable for:
- Audio calls
- Video calls
- Video + screen-share calls (network profile)

It focuses on meeting quality signals: latency, jitter, packet loss, DNS responsiveness, route instability, local Wi-Fi quality, and latency under light load.

## Run

```bash
cd wifi-call-health
python3 -m pip install --user rich
python3 vibe_check_wifi.py
```

The app runs as a full terminal UI with live progress panels, phase-by-phase activity, and a final results screen.

## Menu Options

1. Quick Check (1-2 min)
2. Meeting Check (10-15 min)
3. Live Monitor
4. Choose Call Profile
5. Run Optional Speed Test
6. View Recent History
7. Exit

## Output

Text-only report with:
- Overall verdict (`PASS`, `WARN`, `FAIL`)
- Profile verdict
- Top detected problems
- Detailed metrics
- Actionable next steps

Run history is appended to:

```text
./history.log
```

## Tests

```bash
cd wifi-call-health
python3 -m pytest -q
```
