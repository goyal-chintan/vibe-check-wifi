# UI Simplification & Animations Design

## Overview
This design outlines a significant usability update to the Vibe Check WiFi CLI tool. The goals are strictly focused on reducing friction (cognitive load / keystrokes) and increasing the immersion and responsiveness of the live UI components, specifically the Signal Radar.

## 1. Zero-Prompt Menu System
The main interactive menu (`run_menu_tui`) will no longer ask follow-up questions for the primary actions. They will execute immediately with sensible, opinionated defaults:

- `1) Quick Check (1 min)` -> Immediately runs a 60-second test **including** the speed test at the end. (No prompts for duration or speed test).
- `2) Meeting Check (10 mins)` -> Immediately runs a 10-minute test **including** the speed test at the end.
- `3) Live Monitor` -> Immediately streams continuous 2-second polls indefinitely until the user presses `Ctrl+C`. (No prompts for duration or interval).
- `4) Run Optional Speed Test` -> Moved up to position 4 for prominence.
- Profiles, Themes, History, and WiFi Doctor will shift down.

## 2. Hyper-Active Signal Radar
The `_render_signal_radar_panel` and the streams feeding it will be upgraded to feel much more "alive":

### Faster Poll & Refresh Rates
- Base polling interval drops from 5s to 2s.
- `refresh_per_second` for the TUI increases from 8 FPS to 15 FPS for smoother animation frames.

### Sparklines
Historical data for key metrics (`latency_p95_ms` and `jitter_ms`) will be plotted as sparklines (e.g., ` ▂▃▅▆▇`) inside the "How's Your Connection?" table, allowing users to see sudden spikes over the trailing 60 seconds without leaving the UI. 

### Dynamic Radar Pulses
The ASCII radar sweep will react to the network condition:
- `PASS`: Smooth, continuous green sweeping animation (existing).
- `WARN` / `FAIL`: The sweeping stops and the radar pulses expanding concentric rings in yellow/red (like a warning beacon or target lock), drawing immediate physiological attention to a network fault.

## 3. Implementation Plan
- Update `test_cli.py` to handle the removal of prompt assertions.
- Modify the menu handlers in `vibe_check_wifi.py` to pass hardcoded `minutes` and `include_speed_test=True`.
- Update the default intervals in `execute_assessment_streamed` and `run_live_monitor_tui`.
- Introduce a sparkline generator utility logic.
- Expand the animation frames in the `_render_signal_radar_panel` to check `verdict` for pulse vs. sweep.
