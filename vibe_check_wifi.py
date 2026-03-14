from __future__ import annotations

import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from doctor import (
    FRIENDLY_NAMES,
    IMPACT_DESCRIPTIONS,
    RADAR_LABELS,
    RADAR_ONE_LINERS,
    DiagnosticResult,
    apply_fix,
    get_friendly_name,
    get_friendly_recommendation,
    get_impact_description,
    get_radar_label,
    get_radar_one_liner,
    run_diagnostics,
)
from history import append_history, read_recent_history
from probes import (
    DEFAULT_PRIMARY_TARGET,
    DEFAULT_SECONDARY_TARGET,
    DNS_HOSTS,
    congestion_probe,
    dns_latency,
    get_gateway_ip,
    get_wifi_info,
    optional_speed_test,
    ping_target,
    route_snapshot,
    summarize_latency,
)
from profiles import evaluate_profile
from reporting import format_report

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except Exception:
    HAS_RICH = False


EventCallback = Callable[[str, str, float, dict[str, Any] | None], None]

PROFILE_CHOICES = {
    "1": ("audio", "Audio call"),
    "2": ("video", "Video call"),
    "3": ("video_share", "Video + Screen-share call"),
}

THEME_CHOICES = {
    "1": ("studio_board", "Studio Board"),
    "2": ("signal_radar", "Signal Radar"),
    "3": ("vibe_arcade", "Vibe Arcade"),
}

DEFAULT_THEME = "studio_board"
SETTINGS_PATH = Path(__file__).resolve().parent / ".vibe-check-wifi.json"


def _prompt_int(input_fn: Callable[[str], str], prompt: str, default: int, minimum: int, maximum: int) -> int:
    raw = input_fn(prompt).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _prompt_yes_no(input_fn: Callable[[str], str], prompt: str, default: bool = False) -> bool:
    raw = input_fn(prompt).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true"}


def choose_profile(current_profile: str, input_fn=input, output_fn=print) -> str:
    output_fn("")
    output_fn("Choose call profile")
    for key, (_, label) in PROFILE_CHOICES.items():
        output_fn(f"{key}) {label}")
    output_fn(f"Current: {current_profile}")
    choice = input_fn("Select profile [1-3, Enter to keep current]: ").strip()
    if choice in PROFILE_CHOICES:
        selected = PROFILE_CHOICES[choice][0]
        output_fn(f"Profile updated to: {PROFILE_CHOICES[choice][1]}")
        return selected
    output_fn("Profile unchanged.")
    return current_profile


def _theme_label(theme: str) -> str:
    for _, (key, label) in THEME_CHOICES.items():
        if key == theme:
            return label
    return "Studio Board"


def _normalize_theme(theme: str | None) -> str:
    if not theme:
        return DEFAULT_THEME
    valid_themes = {key for key, _ in THEME_CHOICES.values()}
    return theme if theme in valid_themes else DEFAULT_THEME


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, str]:
    try:
        if not path.exists():
            return {"theme": DEFAULT_THEME}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"theme": _normalize_theme(data.get("theme"))}
    except Exception:
        return {"theme": DEFAULT_THEME}


def save_settings(settings: dict[str, str], path: Path = SETTINGS_PATH) -> None:
    payload = {"theme": _normalize_theme(settings.get("theme"))}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def choose_theme(current_theme: str, input_fn=input, output_fn=print) -> str:
    output_fn("")
    output_fn("Choose UI theme")
    for key, (_, label) in THEME_CHOICES.items():
        output_fn(f"{key}) {label}")
    output_fn(f"Current: {_theme_label(current_theme)}")
    choice = input_fn("Select theme [1-3, Enter to keep current]: ").strip()
    if choice in THEME_CHOICES:
        selected_key, selected_label = THEME_CHOICES[choice]
        output_fn(f"Theme updated to: {selected_label}")
        return selected_key
    output_fn("Theme unchanged.")
    return current_theme


def _build_report(profile: str, assessment: dict, include_speed_test: bool) -> dict:
    metrics = assessment["metrics"]
    profile_result = evaluate_profile(metrics, profile)
    use_case_results = {
        key: evaluate_profile(metrics, key) for key in ("audio", "video", "video_share")
    }
    report = {
        "profile": profile,
        "metrics": metrics,
        "profile_result": profile_result,
        "overall_verdict": profile_result["verdict"],
        "use_case_results": use_case_results,
        "wifi_performance": _summarize_wifi_performance(metrics, use_case_results),
    }
    if include_speed_test and "speed_test" in assessment:
        report["speed_test"] = assessment["speed_test"]
    return report


def _default_probe_ops() -> dict[str, Any]:
    return {
        "primary_target": DEFAULT_PRIMARY_TARGET,
        "secondary_target": DEFAULT_SECONDARY_TARGET,
        "get_wifi_info": get_wifi_info,
        "get_gateway_ip": get_gateway_ip,
        "ping_target": ping_target,
        "summarize_latency": summarize_latency,
        "dns_latency": dns_latency,
        "route_snapshot": route_snapshot,
        "congestion_probe": congestion_probe,
        "optional_speed_test": optional_speed_test,
    }


def _summarize_wifi_performance(
    metrics: dict[str, Any],
    use_case_results: dict[str, dict[str, Any]],
) -> dict[str, str]:
    snr = metrics.get("snr_db")
    tx_rate = metrics.get("tx_rate_mbps")
    video_share_verdict = use_case_results.get("video_share", {}).get("verdict")
    video_verdict = use_case_results.get("video", {}).get("verdict")

    if video_share_verdict == "PASS" and snr is not None and snr >= 25 and tx_rate is not None and tx_rate >= 150:
        return {
            "rating": "Strong",
            "summary": "Stable for meetings and healthy enough for video calls with screen sharing.",
        }
    if video_verdict in {"PASS", "WARN"}:
        return {
            "rating": "Fair",
            "summary": "Good for normal meetings, but heavier workloads may be sensitive to distance or interference.",
        }
    return {
        "rating": "Poor",
        "summary": "Likely to struggle with real-time meetings until signal quality or stability improves.",
    }


def _current_clock_str() -> str:
    return time.strftime("%H:%M:%S")


def _format_metric(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _verdict_style(verdict: str | None) -> str:
    return {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(verdict or "WARN", "cyan")


def _verdict_emoji(verdict: str | None) -> str:
    return {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}.get(verdict or "WARN", "⏳")


def _status_for_metric(snapshot: dict[str, Any] | None, metric: str) -> str:
    if not snapshot:
        return "PASS"
    issue_map = snapshot.get("issue_severity_by_metric") or {}
    return issue_map.get(metric, "PASS")


def _status_emoji(status: str) -> str:
    return {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴"}.get(status, "⚪")


def _badge_for_status(status: str) -> str:
    return {"PASS": "LOCK", "WARN": "WATCH", "FAIL": "DROP"}.get(status, "WATCH")


def _interpretation_for_verdict(verdict: str | None) -> str:
    if verdict == "PASS":
        return "Ready for this call profile"
    if verdict == "WARN":
        return "Usable, but watch the flagged signal"
    return "Not stable enough for this call profile"


def _event_tag(event_type: str) -> str:
    return {
        "start": "🚀 BOOT",
        "phase_start": "📡 SCAN",
        "phase_done": "✅ DONE",
        "done": "🏁 DONE",
        "phase_tick": "⏱️  LOAD",
    }.get(event_type, "📡 SCAN")


def _sparkline(history: list[float], width: int = 20) -> str:
    if not history:
        return "░" * width
    blocks = " ▁▂▃▄▅▆▇█"
    recent = history[-width:]
    lo = min(recent) if recent else 0
    hi = max(recent) if recent else 1
    span = max(hi - lo, 0.001)
    return "".join(blocks[min(8, int((v - lo) / span * 8))] for v in recent).ljust(width, "░")


def _bar_gauge(value: float, maximum: float, width: int = 20, reverse: bool = False) -> str:
    if maximum <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, value / maximum))
    if reverse:
        ratio = 1.0 - ratio
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _styled_metric_text(label: str, value: Any, status: str, unit: str = "") -> Text:
    style = _verdict_style(status)
    emoji = _status_emoji(status)
    val_str = _format_metric(value)
    text = Text()
    text.append(f"{emoji} ", style="bold")
    text.append(f"{label}: ", style="bold white")
    text.append(f"{val_str}", style=f"bold {style}")
    if unit:
        text.append(f" {unit}", style="dim")
    return text


def _use_case_readiness_text(snapshot: dict[str, Any] | None, profile: str) -> Text:
    text = Text()
    if not snapshot:
        text.append("  📞 Audio ⏳  📹 Video ⏳  🖥️  Share ⏳", style="dim")
        return text
    metrics = {
        "latency_p95_ms": snapshot.get("latency_p95_ms"),
        "jitter_ms": snapshot.get("jitter_ms"),
        "packet_loss_pct": snapshot.get("packet_loss_pct"),
        "snr_db": snapshot.get("snr_db"),
        "dns_p95_ms": snapshot.get("dns_p95_ms"),
        "route_timeout_hops": snapshot.get("route_timeout_hops"),
        "congestion_delta_ms": snapshot.get("congestion_delta_ms"),
        "download_mbps": snapshot.get("download_mbps"),
        "upload_mbps": snapshot.get("upload_mbps"),
    }
    cases = [
        ("📞 Audio", evaluate_profile(metrics, "audio")["verdict"]),
        ("📹 Video", evaluate_profile(metrics, "video")["verdict"]),
        ("🖥️  Share", evaluate_profile(metrics, "video_share")["verdict"]),
    ]
    for label, v in cases:
        emoji = _verdict_emoji(v)
        style = _verdict_style(v)
        text.append(f"  {label} ", style="bold")
        text.append(f"{emoji}", style=f"bold {style}")
    return text


def _wifi_info_text(state: dict[str, Any]) -> Text:
    wifi = state.get("wifi_info") or {}
    text = Text()
    ssid = wifi.get("ssid") or "Unknown"
    band = wifi.get("band") or "?"
    channel = wifi.get("channel") or "?"
    rssi = wifi.get("rssi_dbm")
    text.append("📶 ", style="bold")
    text.append(f"{ssid}", style="bold bright_cyan")
    text.append(f"  •  {band}", style="dim")
    text.append(f"  •  Ch {channel}", style="dim")
    if rssi is not None:
        text.append(f"  •  {rssi} dBm", style="dim")
    return text


def _build_live_snapshot(
    profile: str,
    sample_index: int,
    sample_count: int,
    primary_summary: dict[str, Any],
    latest_wifi: dict[str, Any],
    latest_dns_p95: float | None,
    latest_route_timeout: float | None,
    latest_congestion_delta: float | None,
    latest_download_mbps: float | None,
    latest_upload_mbps: float | None,
) -> dict[str, Any]:
    metrics = {
        "latency_p95_ms": primary_summary.get("latency_p95_ms"),
        "jitter_ms": primary_summary.get("jitter_ms"),
        "packet_loss_pct": primary_summary.get("packet_loss_pct"),
        "snr_db": latest_wifi.get("snr_db"),
        "dns_p95_ms": latest_dns_p95,
        "route_timeout_hops": latest_route_timeout,
        "congestion_delta_ms": latest_congestion_delta,
        "download_mbps": latest_download_mbps,
        "upload_mbps": latest_upload_mbps,
    }
    evaluated = evaluate_profile(metrics, profile)
    top_issue = evaluated["issues"][0]["metric"] if evaluated["issues"] else None
    issue_severity_by_metric = {issue["metric"]: issue["severity"] for issue in evaluated["issues"]}
    return {
        "sample_index": sample_index,
        "sample_count": sample_count,
        "verdict": evaluated["verdict"],
        "top_issue_metric": top_issue,
        "latency_p95_ms": metrics["latency_p95_ms"],
        "jitter_ms": metrics["jitter_ms"],
        "packet_loss_pct": metrics["packet_loss_pct"],
        "snr_db": metrics["snr_db"],
        "dns_p95_ms": metrics["dns_p95_ms"],
        "route_timeout_hops": metrics["route_timeout_hops"],
        "congestion_delta_ms": metrics["congestion_delta_ms"],
        "download_mbps": metrics["download_mbps"],
        "upload_mbps": metrics["upload_mbps"],
        "issue_severity_by_metric": issue_severity_by_metric,
    }


def _vibe_score(snapshot: dict[str, Any] | None) -> tuple[int, str]:
    if not snapshot:
        return 50, "BUILDING"
    score = 50
    verdict = snapshot.get("verdict")
    if verdict == "PASS":
        score += 25
    elif verdict == "WARN":
        score += 5
    else:
        score -= 25
    loss = snapshot.get("packet_loss_pct")
    if loss is not None:
        score -= min(float(loss) * 12.0, 20.0)
    snr = snapshot.get("snr_db")
    if snr is not None:
        score += min(max(float(snr) - 20.0, 0.0), 10.0)
    if snapshot.get("top_issue_metric") in {"download_mbps", "upload_mbps"}:
        score -= 10
    score = int(max(0, min(99, score)))
    if score >= 85:
        tier = "LOCKED IN"
    elif score >= 65:
        tier = "CRUISING"
    elif score >= 40:
        tier = "SHAKY"
    else:
        tier = "CURSED"
    return score, tier


def _vibe_tier_emoji(tier: str) -> str:
    return {"LOCKED IN": "🔥", "CRUISING": "😎", "SHAKY": "😬", "CURSED": "💀", "BUILDING": "🔧"}.get(tier, "🔧")


def _progress_bar_text(progress: float, width: int = 60) -> Text:
    filled = int(progress / 100.0 * width)
    bar = "━" * filled + "╸" + "┄" * max(0, width - filled - 1)
    pct = f" {progress:.0f}%"
    text = Text()
    if progress >= 90:
        text.append(bar, style="bold green")
    elif progress >= 50:
        text.append(bar, style="bold bright_cyan")
    else:
        text.append(bar, style="bold blue")
    text.append(pct, style="bold white")
    return text


def _sparkline(values: list[float], length: int = 15) -> str:
    """Generate a sparkline string from a list of floats."""
    if not values:
        return " " * length
    bars = " ▂▃▄▅▆▇█"
    
    recent = values[-length:] if len(values) > length else values
    min_val, max_val = min(recent), max(recent)
    
    if min_val == max_val:
        v = bars[3] * len(recent)
        return v.ljust(length)
        
    res = ""
    for v in recent:
        idx = int((v - min_val) / (max_val - min_val) * 7)
        idx = max(0, min(7, idx))
        res += bars[idx]
    return res.ljust(length)


# ── Studio Board Theme ──────────────────────────────────────

def _render_studio_board_panel(profile: str, state: dict[str, Any], logs: deque[str]) -> Any:
    snapshot = state.get("snapshot")
    verdict = snapshot.get("verdict") if snapshot else "WARN"
    frame = state.get("frame_index", 0)
    pulse_frames = ["◉", "◎", "●", "○"]
    pulse = pulse_frames[frame % len(pulse_frames)]
    clock = _current_clock_str()

    # ── Hero banner ──
    hero = Table.grid(expand=True)
    hero.add_column(justify="left", ratio=3)
    hero.add_column(justify="right", ratio=2)

    title_text = Text()
    title_text.append(f"  {pulse} ", style="bold bright_cyan")
    title_text.append("VIBE CHECK WiFi", style="bold bright_white")
    title_text.append(f"  {pulse}", style="bold bright_cyan")

    verdict_text = Text()
    verdict_text.append(f"{_verdict_emoji(verdict)} ", style="bold")
    verdict_text.append(f"{verdict}", style=f"bold {_verdict_style(verdict)}")
    verdict_text.append(f"  •  {_interpretation_for_verdict(verdict)}", style="dim")

    profile_text = Text()
    profile_text.append(f"🎯 Profile: ", style="dim")
    profile_text.append(f"{profile.upper()}", style="bold bright_yellow")
    profile_text.append(f"  •  ⏰ {clock}", style="dim")

    hero.add_row(title_text, profile_text)
    hero.add_row(verdict_text, _wifi_info_text(state))

    # ── Use-case readiness ──
    readiness = _use_case_readiness_text(snapshot, profile)

    # ── Progress ──
    progress = _progress_bar_text(state["progress"])
    sample_text = Text()
    if snapshot:
        sample_text.append(f"  Sample {snapshot['sample_index']}/{snapshot['sample_count']}", style="dim")
    else:
        sample_text.append("  Initializing...", style="dim italic")

    # ── Metric Grid (left) ──
    latency_history = state.get("latency_history", [])
    jitter_history = state.get("jitter_history", [])
    loss_history = state.get("loss_history", [])

    metrics_grid = Table.grid(expand=True, padding=(0, 1))
    metrics_grid.add_column(ratio=3)
    metrics_grid.add_column(ratio=2, justify="right")

    metric_rows = [
        ("Latency p95", "latency_p95_ms", "ms", latency_history),
        ("Jitter", "jitter_ms", "ms", jitter_history),
        ("Packet Loss", "packet_loss_pct", "%", loss_history),
        ("SNR", "snr_db", "dB", []),
        ("DNS p95", "dns_p95_ms", "ms", []),
        ("Load Delta", "congestion_delta_ms", "ms", []),
        ("Download", "download_mbps", "Mbps", []),
        ("Upload", "upload_mbps", "Mbps", []),
    ]
    for label, key, unit, history in metric_rows:
        status = _status_for_metric(snapshot, key)
        val = snapshot.get(key) if snapshot else None
        styled = _styled_metric_text(label, val, status, unit)
        spark_text = Text()
        if history:
            spark_text.append(_sparkline(history), style=f"bold {_verdict_style(status)}")
        else:
            spark_text.append("░" * 20, style="dim")
        metrics_grid.add_row(styled, spark_text)

    # ── Session sidebar (right) ──
    sidebar = Table.grid(expand=True, padding=(0, 1))
    sidebar.add_column(justify="left")

    phase_text = Text()
    phase_text.append("⚡ ", style="bold bright_yellow")
    phase_text.append(state["phase"], style="bright_white")
    sidebar.add_row(phase_text)
    sidebar.add_row(Text(""))

    if snapshot and snapshot.get("top_issue_metric"):
        issue_text = Text()
        issue_text.append("⚠️  Top issue: ", style="bold yellow")
        issue_text.append(snapshot["top_issue_metric"], style="bold bright_red")
        sidebar.add_row(issue_text)
    else:
        sidebar.add_row(Text("✨ No issues detected", style="bold green"))

    sidebar.add_row(Text(""))
    sidebar.add_row(Text("─── Event Log ───", style="dim"))
    for line in list(logs)[-6:]:
        sidebar.add_row(Text(line, style="dim"))

    # ── Bottom layout ──
    bottom = Table.grid(expand=True)
    bottom.add_column(ratio=3)
    bottom.add_column(ratio=2)
    bottom.add_row(
        Panel(metrics_grid, title="📊 Metrics", border_style="bright_blue", padding=(1, 2)),
        Panel(sidebar, title="📋 Session", border_style="bright_white", padding=(1, 2)),
    )

    # ── Assemble ──
    body = Table.grid(expand=True)
    body.add_row(Panel(hero, border_style=_verdict_style(verdict), padding=(0, 2)))
    body.add_row(readiness)
    body.add_row(Text(""))
    body.add_row(progress)
    body.add_row(sample_text)
    body.add_row(Text(""))
    body.add_row(bottom)
    return Panel(body, title="━━━ 🎛️  Studio Board ━━━", border_style="bright_blue", padding=(1, 2))


# ── Signal Radar Theme ──────────────────────────────────────

def _render_signal_radar_panel(profile: str, state: dict[str, Any], logs: deque[str]) -> Any:
    snapshot = state.get("snapshot")
    verdict = snapshot.get("verdict") if snapshot else "WARN"
    frame = state.get("frame_index", 0)
    clock = _current_clock_str()

    # ── Radar sweep animation ──
    if verdict == "PASS" or not snapshot:
        # Sweeping radar
        s = ["╱", "│", "╲", "─"][(frame // 2) % 4]
        rings = [
            ["         ", "         ", f"    {s}    ", "         ", "         "],
            ["         ", f"   {s} {s}   ", f"   {s}●{s}   ", f"   {s} {s}   ", "         "],
            [f"  {s}   {s}  ", f" {s}     {s} ", f"{s}   ●   {s}", f" {s}     {s} ", f"  {s}   {s}  "],
            ["         ", "         ", "    ●    ", "         ", "         "],
        ][(frame // 3) % 4]
    else:
        # Pulsing target lock
        pulse_frames = [
            ["         ", "         ", "    ●    ", "         ", "         "],
            ["         ", "   ( )   ", "  ( ● )  ", "   ( )   ", "         "],
            ["         ", "  (( ))  ", " (( ● )) ", "  (( ))  ", "         "],
            [" ((( ))) ", " ((( ))) ", "((( ● )))", " ((( ))) ", " ((( ))) "],
            ["         ", "  (( ))  ", " (( ● )) ", "  (( ))  ", "         "],
            ["         ", "   ( )   ", "  ( ● )  ", "   ( )   ", "         "],
        ]
        rings = pulse_frames[(frame // 2) % len(pulse_frames)]

    radar_art = Table.grid(expand=True)
    radar_art.add_column(justify="center")
    for line in rings:
        radar_art.add_row(Text(line, style=f"bold {_verdict_style(verdict)}"))
    # Verdict display with friendly headline
    verdict_display = Text()
    verdict_display.append(f"\n{_verdict_emoji(verdict)} ", style="bold")
    verdict_headline = {
        "PASS": "All Clear!",
        "WARN": "Mostly OK",
        "FAIL": "Needs Help",
    }.get(verdict, verdict)
    verdict_display.append(verdict_headline, style=f"bold {_verdict_style(verdict)}")
    if snapshot:
        verdict_display.append(f"\n{snapshot['sample_index']}/{snapshot['sample_count']}", style="dim")
    radar_art.add_row(verdict_display)

    # ── Link status board (with friendly labels) ──
    radar_keys = [
        "latency_p95_ms", "jitter_ms", "packet_loss_pct", "snr_db",
        "dns_p95_ms", "congestion_delta_ms", "download_mbps", "upload_mbps",
    ]
    board = Table(expand=True, show_header=True, header_style="bold bright_cyan", box=None, padding=(0, 1))
    board.add_column("What", justify="left", ratio=3)
    board.add_column("Value", justify="right", ratio=2)
    board.add_column("", justify="center", ratio=1)
    board.add_column("Impact", justify="left", ratio=4)
    for key in radar_keys:
        friendly_label, unit = get_radar_label(key)
        status = _status_for_metric(snapshot, key)
        val = snapshot.get(key) if snapshot else None
        val_str = _format_metric(val)
        style = _verdict_style(status)
        one_liner = get_radar_one_liner(key, status)
        
        # Add sparklines for latency and jitter
        extra = ""
        if key == "latency_p95_ms":
            history = state.get("latency_history", [])
            extra = f"  {_sparkline(history, 10)}"
        elif key == "jitter_ms":
            history = state.get("jitter_history", [])
            extra = f"  {_sparkline(history, 10)}"

        board.add_row(
            Text(friendly_label, style="bold"),
            Text(f"{val_str} {unit}{extra}", style=f"bold {style}"),
            Text(_status_emoji(status)),
            Text(one_liner, style=f"italic {style}") if one_liner else Text(""),
        )

    # ── Top row ──
    top = Table.grid(expand=True)
    top.add_column(ratio=1)
    top.add_column(ratio=3)
    top.add_row(
        Panel(radar_art, title="🔍 Radar", border_style="bright_cyan", padding=(1, 2)),
        Panel(board, title="📡 How's Your Connection?", border_style=_verdict_style(verdict), padding=(1, 1)),
    )

    # ── Wi-Fi + readiness ──
    wifi_row = Table.grid(expand=True)
    wifi_row.add_column(ratio=1)
    wifi_row.add_column(ratio=1)
    wifi_row.add_row(_wifi_info_text(state), _use_case_readiness_text(snapshot, profile))

    # ── Timeline ──
    timeline_lines = list(logs)[-6:] if logs else ["⏳ Scanning for signals..."]
    timeline_text = Text()
    for i, line in enumerate(timeline_lines):
        if i > 0:
            timeline_text.append("\n")
        timeline_text.append(line, style="dim" if i < len(timeline_lines) - 1 else "bright_white")

    # ── Progress ──
    progress = _progress_bar_text(state["progress"])
    header_text = Text()
    header_text.append(f"🎯 {profile.upper()}", style="bold bright_yellow")
    header_text.append(f"  •  ⏰ {clock}", style="dim")

    body = Table.grid(expand=True)
    body.add_row(header_text)
    body.add_row(Text(""))
    body.add_row(top)
    body.add_row(Text(""))
    body.add_row(wifi_row)
    body.add_row(Text(""))
    body.add_row(progress)
    body.add_row(Text(""))
    body.add_row(Panel(timeline_text, title="📜 What's Happening", border_style="bright_magenta", padding=(1, 2)))
    return Panel(body, title="━━━ 📡 Signal Radar ━━━", border_style="bright_green", padding=(1, 2))


# ── Vibe Arcade Theme ───────────────────────────────────────

def _render_vibe_arcade_panel(profile: str, state: dict[str, Any], logs: deque[str]) -> Any:
    snapshot = state.get("snapshot")
    score, tier = _vibe_score(snapshot)
    frame = state.get("frame_index", 0)
    streak = int(state.get("clean_streak", 0))
    clock = _current_clock_str()

    tier_emoji = _vibe_tier_emoji(tier)

    # ── Big score display ──
    border_chars = ["═", "~", "≈", "═"]
    bc = border_chars[frame % len(border_chars)]
    score_border = bc * 20

    score_grid = Table.grid(expand=True)
    score_grid.add_column(justify="center")
    score_grid.add_row(Text(score_border, style="bold bright_magenta"))
    score_grid.add_row(Text(""))

    # Giant score number
    big_score = Text()
    if score >= 85:
        big_score.append(f"  {tier_emoji}  ", style="bold")
        big_score.append(f"{score}", style="bold bright_green on black")
        big_score.append(f"  {tier_emoji}  ", style="bold")
    elif score >= 65:
        big_score.append(f"  {tier_emoji}  ", style="bold")
        big_score.append(f"{score}", style="bold bright_cyan")
        big_score.append(f"  {tier_emoji}  ", style="bold")
    elif score >= 40:
        big_score.append(f"  {tier_emoji}  ", style="bold")
        big_score.append(f"{score}", style="bold bright_yellow")
        big_score.append(f"  {tier_emoji}  ", style="bold")
    else:
        big_score.append(f"  {tier_emoji}  ", style="bold")
        big_score.append(f"{score}", style="bold bright_red")
        big_score.append(f"  {tier_emoji}  ", style="bold")

    score_grid.add_row(big_score)
    score_grid.add_row(Text(""))
    tier_text = Text()
    tier_text.append(f"『 {tier} 』", style=f"bold {_verdict_style(snapshot.get('verdict') if snapshot else 'WARN')}")
    score_grid.add_row(tier_text)
    score_bar = _bar_gauge(score, 99, width=25)
    score_grid.add_row(Text(""))
    score_grid.add_row(Text(score_bar, style="bold bright_magenta"))
    score_grid.add_row(Text(score_border, style="bold bright_magenta"))

    # ── Streak & combo ──
    streak_grid = Table.grid(expand=True)
    streak_grid.add_column(justify="center")
    streak_fire = "🔥" * min(streak, 5) if streak > 0 else "💤"
    streak_grid.add_row(Text(f"🏆 CLEAN STREAK", style="bold bright_yellow"))
    streak_grid.add_row(Text(""))
    streak_grid.add_row(Text(f"{streak_fire}", style="bold"))
    streak_grid.add_row(Text(f"× {streak}", style="bold bright_white"))
    if streak >= 10:
        streak_grid.add_row(Text("COMBO! 🎯", style="bold bright_green blink"))
    elif streak >= 5:
        streak_grid.add_row(Text("ON FIRE!", style="bold bright_yellow"))
    else:
        streak_grid.add_row(Text(""))
    streak_grid.add_row(Text(""))
    streak_grid.add_row(Text(f"🎯 {profile.upper()}", style="bold bright_cyan"))
    streak_grid.add_row(Text(f"⏰ {clock}", style="dim"))

    # ── Metric leaderboard ──
    leaderboard = Table(expand=True, show_header=True, header_style="bold bright_magenta", box=None, padding=(0, 1))
    leaderboard.add_column("Metric", justify="left", ratio=3)
    leaderboard.add_column("Value", justify="right", ratio=2)
    leaderboard.add_column("", justify="center", ratio=1)

    arcade_metrics = [
        ("⏱️  LATENCY", "latency_p95_ms", "ms"),
        ("📈 JITTER", "jitter_ms", "ms"),
        ("📦 PKT LOSS", "packet_loss_pct", "%"),
        ("📶 SIGNAL", "snr_db", "dB"),
        ("🌐 DNS", "dns_p95_ms", "ms"),
        ("🔥 LOAD", "congestion_delta_ms", "ms"),
        ("⬇️  DOWN", "download_mbps", "Mbps"),
        ("⬆️  UP", "upload_mbps", "Mbps"),
    ]
    for label, key, unit in arcade_metrics:
        status = _status_for_metric(snapshot, key)
        val = snapshot.get(key) if snapshot else None
        val_str = _format_metric(val)
        style = _verdict_style(status)
        leaderboard.add_row(
            Text(label, style="bold bright_white"),
            Text(f"{val_str} {unit}", style=f"bold {style}"),
            Text(_status_emoji(status)),
        )

    # ── Top row ──
    top = Table.grid(expand=True)
    top.add_column(ratio=2)
    top.add_column(ratio=1)
    top.add_column(ratio=2)
    top.add_row(
        Panel(score_grid, title="🕹️  VIBE METER", border_style="bright_magenta", padding=(1, 2)),
        Panel(streak_grid, title="🔥 STREAK", border_style="bright_yellow", padding=(1, 2)),
        Panel(leaderboard, title="📊 LEADERBOARD", border_style="bright_cyan", padding=(1, 1)),
    )

    # ── Use-case readiness ──
    readiness = _use_case_readiness_text(snapshot, profile)

    # ── Arcade feed ──
    feed_lines = list(logs)[-6:] if logs else ["🕹️  INSERT COIN... waiting for data"]
    feed_text = Text()
    for i, line in enumerate(feed_lines):
        if i > 0:
            feed_text.append("\n")
        feed_text.append(line, style="dim" if i < len(feed_lines) - 1 else "bold bright_green")

    # ── Progress ──
    progress = _progress_bar_text(state["progress"])

    # ── Phase ──
    phase_text = Text()
    phase_text.append("⚡ NOW PLAYING: ", style="bold bright_yellow")
    phase_text.append(state["phase"], style="bright_white")
    if snapshot and snapshot.get("top_issue_metric"):
        phase_text.append(f"  •  ⚠️  {snapshot['top_issue_metric']}", style="bold yellow")

    body = Table.grid(expand=True)
    body.add_row(top)
    body.add_row(Text(""))
    body.add_row(readiness)
    body.add_row(Text(""))
    body.add_row(progress)
    body.add_row(phase_text)
    body.add_row(Text(""))
    body.add_row(Panel(feed_text, title="🕹️  ARCADE FEED", border_style="bright_green", padding=(1, 2)))
    return Panel(body, title="━━━ 🕹️  Vibe Arcade ━━━", border_style="bright_green", padding=(1, 2))


def _render_tui_panel(theme: str, profile: str, state: dict[str, Any], logs: deque[str]) -> Any:
    selected = _normalize_theme(theme)
    if selected == "signal_radar":
        return _render_signal_radar_panel(profile, state, logs)
    if selected == "vibe_arcade":
        return _render_vibe_arcade_panel(profile, state, logs)
    return _render_studio_board_panel(profile, state, logs)


def _make_tui_event_handler(
    state: dict[str, Any],
    logs: deque[str],
    update_fn: Callable[[Any], None],
    render_fn: Callable[[], Any],
    clock: Callable[[], str] = _current_clock_str,
) -> EventCallback:
    def event_cb(event_type: str, detail: str, progress: float, snapshot: dict[str, Any] | None) -> None:
        timestamp = clock()
        if event_type in {"start", "phase_start", "phase_done", "done"}:
            logs.append(f"[{timestamp}] [{_event_tag(event_type)}] {detail}")
        state["phase"] = detail
        state["progress"] = progress
        state["snapshot"] = snapshot
        state["event_count"] = int(state.get("event_count", 0)) + 1
        if event_type == "phase_tick":
            state["frame_index"] = int(state.get("frame_index", 0)) + 1
        # Track sparkline histories
        if snapshot:
            for hist_key, snap_key in [("latency_history", "latency_p95_ms"), ("jitter_history", "jitter_ms"), ("loss_history", "packet_loss_pct")]:
                val = snapshot.get(snap_key)
                if val is not None:
                    history = state.setdefault(hist_key, [])
                    history.append(float(val))
                    if len(history) > 60:
                        del history[:len(history) - 60]
        verdict = snapshot.get("verdict") if snapshot else None
        if verdict == "PASS":
            state["clean_streak"] = int(state.get("clean_streak", 0)) + 1
        elif verdict in {"WARN", "FAIL"}:
            state["clean_streak"] = 0
        update_fn(render_fn())

    return event_cb


def _call_with_fallback(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        return fn()


def _safe_max(values: list[float]) -> float | None:
    return max(values) if values else None


def _safe_min(values: list[float]) -> float | None:
    return min(values) if values else None


def _empty_latency_summary() -> dict[str, Any]:
    return {
        "sent": 0,
        "received": 0,
        "packet_loss_pct": None,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "jitter_ms": None,
        "spike_count": None,
        "dropout_count": None,
    }


def _run_with_stream(
    label: str,
    fn: Callable[[], Any],
    expected_seconds: float,
    progress_start: float,
    progress_weight: float,
    emit: EventCallback,
) -> Any:
    emit("phase_start", label, progress_start)
    start = time.time()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        while not future.done():
            elapsed = time.time() - start
            pct = 0.95 if expected_seconds <= 0 else min(0.95, elapsed / expected_seconds)
            emit("phase_tick", f"{label} ({elapsed:.0f}s)", progress_start + progress_weight * pct)
            time.sleep(0.35)
        result = future.result()
    emit("phase_done", label, progress_start + progress_weight)
    return result


def execute_assessment_streamed(
    profile: str,
    minutes: int,
    include_speed_test: bool,
    event_cb: EventCallback | None = None,
    probe_ops: dict[str, Any] | None = None,
) -> dict:
    ops = probe_ops or _default_probe_ops()

    def emit(event_type: str, detail: str, progress: float, snapshot: dict[str, Any] | None = None) -> None:
        if event_cb is not None:
            event_cb(event_type, detail, max(0.0, min(progress, 100.0)), snapshot)

    if "duration_seconds_override" in ops:
        duration_seconds = int(ops["duration_seconds_override"])
    else:
        min_duration_seconds = int(ops.get("min_duration_seconds", 60))
        duration_seconds = max(min_duration_seconds, int(minutes * 60))
    sample_interval_seconds = int(ops.get("sample_interval_seconds", 5))
    loop_count = max(1, int(duration_seconds / sample_interval_seconds))

    primary_target = ops.get("primary_target", DEFAULT_PRIMARY_TARGET)
    secondary_target = ops.get("secondary_target", DEFAULT_SECONDARY_TARGET)
    gateway_ip = ops["get_gateway_ip"]()

    secondary_stride = 2
    wifi_stride = max(1, loop_count // 12)
    dns_stride = max(1, loop_count // 10)
    route_stride = max(1, loop_count // 5)
    load_stride = max(1, loop_count // 4)

    primary_latencies: list[float] = []
    secondary_latencies: list[float] = []
    gateway_latencies: list[float] = []
    primary_sent = 0
    secondary_sent = 0
    gateway_sent = 0
    wifi_samples: list[dict[str, Any]] = []
    dns_p95_samples: list[float] = []
    dns_failure_count = 0
    route_timeout_samples: list[float] = []
    congestion_delta_samples: list[float] = []
    download_mbps_samples: list[float] = []
    upload_mbps_samples: list[float] = []
    latest_wifi: dict[str, Any] = {}
    latest_dns_p95: float | None = None
    latest_route_timeout: float | None = None
    latest_congestion_delta: float | None = None
    latest_download_mbps: float | None = None
    latest_upload_mbps: float | None = None
    last_snapshot: dict[str, Any] | None = None

    emit("start", "Starting diagnostics", 0.0, None)
    emit("phase_start", "Monitoring all core signals over time", 0.0, None)

    for idx in range(loop_count):
        tick_start = time.time()

        primary_ping = ops["ping_target"](primary_target, count=1, interval=0.2)
        primary_sent += primary_ping.get("sent", 1)
        primary_latencies.extend(primary_ping.get("latencies_ms", []))

        if gateway_ip:
            gateway_ping = ops["ping_target"](gateway_ip, count=1, interval=0.2)
            gateway_sent += gateway_ping.get("sent", 1)
            gateway_latencies.extend(gateway_ping.get("latencies_ms", []))

        if idx % secondary_stride == 0:
            secondary_ping = ops["ping_target"](secondary_target, count=1, interval=0.2)
            secondary_sent += secondary_ping.get("sent", 1)
            secondary_latencies.extend(secondary_ping.get("latencies_ms", []))

        if idx % wifi_stride == 0:
            latest_wifi = _call_with_fallback(ops["get_wifi_info"])
            wifi_samples.append(latest_wifi)

        if idx % dns_stride == 0:
            dns_result = _call_with_fallback(ops["dns_latency"], hosts=DNS_HOSTS[:2], attempts=1)
            dns_failures = dns_result.get("dns_failures")
            dns_p95 = dns_result.get("dns_p95_ms")
            if dns_failures is not None:
                dns_failure_count += int(dns_failures)
            if dns_p95 is not None:
                dns_p95_samples.append(float(dns_p95))
                latest_dns_p95 = float(dns_p95)

        if idx % route_stride == 0:
            route_result = _call_with_fallback(ops["route_snapshot"], primary_target)
            route_timeout = route_result.get("route_timeout_hops")
            if route_timeout is not None:
                route_timeout_samples.append(float(route_timeout))
                latest_route_timeout = float(route_timeout)

        if idx % load_stride == 0:
            rolling_primary = ops["summarize_latency"](primary_latencies, sent=max(primary_sent, 1))
            load_result = _call_with_fallback(
                ops["congestion_probe"],
                primary_target,
                rolling_primary.get("latency_p95_ms"),
            )
            if load_result.get("congestion_delta_ms") is not None:
                congestion_delta_samples.append(float(load_result["congestion_delta_ms"]))
                latest_congestion_delta = float(load_result["congestion_delta_ms"])
            if load_result.get("download_mbps") is not None:
                download_mbps_samples.append(float(load_result["download_mbps"]))
                latest_download_mbps = float(load_result["download_mbps"])
            if load_result.get("upload_mbps") is not None:
                upload_mbps_samples.append(float(load_result["upload_mbps"]))
                latest_upload_mbps = float(load_result["upload_mbps"])

        rolling_primary = ops["summarize_latency"](primary_latencies, sent=max(primary_sent, 1))
        if not latest_wifi:
            latest_wifi = _call_with_fallback(ops["get_wifi_info"])
        last_snapshot = _build_live_snapshot(
            profile=profile,
            sample_index=idx + 1,
            sample_count=loop_count,
            primary_summary=rolling_primary,
            latest_wifi=latest_wifi,
            latest_dns_p95=latest_dns_p95,
            latest_route_timeout=latest_route_timeout,
            latest_congestion_delta=latest_congestion_delta,
            latest_download_mbps=latest_download_mbps,
            latest_upload_mbps=latest_upload_mbps,
        )

        progress = ((idx + 1) / loop_count) * 90.0
        emit("phase_tick", f"Monitoring interval {idx + 1}/{loop_count}", progress, last_snapshot)

        elapsed = time.time() - tick_start
        sleep_for = max(0.0, sample_interval_seconds - elapsed)
        if idx < loop_count - 1 and sleep_for > 0:
            time.sleep(sleep_for)

    emit("phase_done", "Monitoring all core signals over time", 90.0, last_snapshot)

    primary_summary = ops["summarize_latency"](primary_latencies, sent=max(primary_sent, 1))
    secondary_summary = (
        ops["summarize_latency"](secondary_latencies, sent=max(secondary_sent, 1))
        if secondary_sent > 0
        else _empty_latency_summary()
    )
    gateway_summary = (
        ops["summarize_latency"](gateway_latencies, sent=max(gateway_sent, 1))
        if gateway_sent > 0
        else _empty_latency_summary()
    )
    latest_wifi = wifi_samples[-1] if wifi_samples else latest_wifi or _call_with_fallback(ops["get_wifi_info"])

    result: dict[str, Any] = {
        "metrics": {
            "latency_p50_ms": primary_summary["latency_p50_ms"],
            "latency_p95_ms": primary_summary["latency_p95_ms"],
            "jitter_ms": primary_summary["jitter_ms"],
            "packet_loss_pct": primary_summary["packet_loss_pct"],
            "spike_count": primary_summary["spike_count"],
            "dropout_count": primary_summary["dropout_count"],
            "secondary_latency_p95_ms": secondary_summary["latency_p95_ms"],
            "secondary_packet_loss_pct": secondary_summary["packet_loss_pct"],
            "gateway_latency_p95_ms": gateway_summary["latency_p95_ms"],
            "gateway_packet_loss_pct": gateway_summary["packet_loss_pct"],
            "snr_db": latest_wifi.get("snr_db"),
            "rssi_dbm": latest_wifi.get("rssi_dbm"),
            "noise_dbm": latest_wifi.get("noise_dbm"),
            "tx_rate_mbps": latest_wifi.get("tx_rate_mbps"),
            "dns_p95_ms": _safe_max(dns_p95_samples),
            "dns_failures": dns_failure_count,
            "route_timeout_hops": _safe_max(route_timeout_samples),
            "congestion_delta_ms": _safe_max(congestion_delta_samples),
            "download_mbps": _safe_min(download_mbps_samples),
            "upload_mbps": _safe_min(upload_mbps_samples),
        },
        "raw": {
            "wifi": latest_wifi,
            "wifi_samples": wifi_samples,
            "gateway_ip": gateway_ip,
            "primary_summary": primary_summary,
            "secondary_summary": secondary_summary,
            "gateway_summary": gateway_summary,
            "dns_p95_samples": dns_p95_samples,
            "route_timeout_samples": route_timeout_samples,
            "congestion_delta_samples": congestion_delta_samples,
            "download_mbps_samples": download_mbps_samples,
            "upload_mbps_samples": upload_mbps_samples,
        },
    }

    if include_speed_test:
        speed_result = _run_with_stream(
            "Running optional throughput check",
            ops["optional_speed_test"],
            expected_seconds=15.0,
            progress_start=90.0,
            progress_weight=10.0,
            emit=emit,
        )
        result["speed_test"] = speed_result
        if speed_result.get("download_mbps") is not None:
            result["metrics"]["download_mbps"] = float(speed_result["download_mbps"])
        if speed_result.get("upload_mbps") is not None:
            result["metrics"]["upload_mbps"] = float(speed_result["upload_mbps"])
        if last_snapshot is not None:
            last_snapshot["download_mbps"] = result["metrics"].get("download_mbps")
            last_snapshot["upload_mbps"] = result["metrics"].get("upload_mbps")
            updated = evaluate_profile(
                {
                    "latency_p95_ms": last_snapshot.get("latency_p95_ms"),
                    "jitter_ms": last_snapshot.get("jitter_ms"),
                    "packet_loss_pct": last_snapshot.get("packet_loss_pct"),
                    "snr_db": last_snapshot.get("snr_db"),
                    "dns_p95_ms": last_snapshot.get("dns_p95_ms"),
                    "route_timeout_hops": last_snapshot.get("route_timeout_hops"),
                    "congestion_delta_ms": last_snapshot.get("congestion_delta_ms"),
                    "download_mbps": last_snapshot.get("download_mbps"),
                    "upload_mbps": last_snapshot.get("upload_mbps"),
                },
                profile,
            )
            last_snapshot["verdict"] = updated["verdict"]
            last_snapshot["top_issue_metric"] = updated["issues"][0]["metric"] if updated["issues"] else None
            last_snapshot["issue_severity_by_metric"] = {
                issue["metric"]: issue["severity"] for issue in updated["issues"]
            }

    emit("done", "Diagnostics complete", 100.0, last_snapshot)
    return result


def run_check(
    profile: str,
    minutes: int,
    include_speed_test: bool,
    output_fn=print,
    event_cb: EventCallback | None = None,
    probe_ops: dict[str, Any] | None = None,
) -> dict:
    output_fn("")
    output_fn(f"Running {minutes}-minute check for profile: {profile}")
    assessment = execute_assessment_streamed(
        profile=profile,
        minutes=minutes,
        include_speed_test=include_speed_test,
        event_cb=event_cb,
        probe_ops=probe_ops,
    )
    report = _build_report(profile, assessment, include_speed_test=include_speed_test)
    text = format_report(report)
    output_fn("")
    output_fn(text)
    append_history(report, text)
    return report


def run_optional_speed_test(profile: str, output_fn=print) -> None:
    run_check(profile, minutes=1, include_speed_test=True, output_fn=output_fn)


def run_live_monitor(profile: str, input_fn=input, output_fn=print) -> None:
    output_fn("")
    output_fn("Live monitor mode (Press Ctrl+C to stop)")
    interval = 2
    duration_minutes = 180  # effectively indefinite
    target = DEFAULT_PRIMARY_TARGET
    gateway_ip = get_gateway_ip()
    sample_count = int((duration_minutes * 60) / interval)
    primary_latency_history = deque(maxlen=60)
    primary_outcome_history = deque(maxlen=60)
    gateway_latency_history = deque(maxlen=60)
    gateway_outcome_history = deque(maxlen=60)
    latest_wifi: dict[str, Any] = {}
    latest_dns_p95: float | None = None
    latest_route_timeout: float | None = None
    latest_congestion_delta: float | None = None
    latest_download_mbps: float | None = None
    latest_upload_mbps: float | None = None

    wifi_stride = max(1, sample_count // 20)
    dns_stride = max(1, sample_count // 15)
    route_stride = max(1, sample_count // 12)
    load_stride = max(1, sample_count // 10)

    output_fn("")
    output_fn(
        f"Starting live monitor for {duration_minutes} minutes, interval {interval}s, "
        f"profile {profile}."
    )
    output_fn("Press Ctrl+C to stop early.")

    start = time.time()
    try:
        for index in range(sample_count):
            tick = time.time()
            primary_ping = ping_target(target, count=1, interval=0.2)
            primary_latencies = primary_ping["latencies_ms"]
            if primary_latencies:
                primary_latency_history.append(primary_latencies[0])
                primary_outcome_history.append(True)
            else:
                primary_outcome_history.append(False)

            if gateway_ip:
                gateway_ping = ping_target(gateway_ip, count=1, interval=0.2)
                gateway_latencies = gateway_ping["latencies_ms"]
                if gateway_latencies:
                    gateway_latency_history.append(gateway_latencies[0])
                    gateway_outcome_history.append(True)
                else:
                    gateway_outcome_history.append(False)

            if index % wifi_stride == 0:
                latest_wifi = get_wifi_info()
            if index % dns_stride == 0:
                dns_result = dns_latency(hosts=DNS_HOSTS[:2], attempts=1)
                latest_dns_p95 = dns_result.get("dns_p95_ms")
            if index % route_stride == 0:
                route_result = route_snapshot(target)
                latest_route_timeout = route_result.get("route_timeout_hops")
            if index % load_stride == 0:
                rolling_primary = summarize_latency(
                    list(primary_latency_history),
                    sent=max(len(primary_outcome_history), 1),
                )
                load_result = congestion_probe(target, rolling_primary.get("latency_p95_ms"))
                latest_congestion_delta = load_result.get("congestion_delta_ms")
                latest_download_mbps = load_result.get("download_mbps")
                latest_upload_mbps = load_result.get("upload_mbps")

            primary_summary = summarize_latency(
                list(primary_latency_history),
                sent=max(len(primary_outcome_history), 1),
            )
            gateway_summary = (
                summarize_latency(
                    list(gateway_latency_history),
                    sent=max(len(gateway_outcome_history), 1),
                )
                if gateway_ip and gateway_outcome_history
                else _empty_latency_summary()
            )

            metrics = {
                "latency_p95_ms": primary_summary["latency_p95_ms"],
                "jitter_ms": primary_summary["jitter_ms"],
                "packet_loss_pct": primary_summary["packet_loss_pct"],
                "snr_db": latest_wifi.get("snr_db"),
                "congestion_delta_ms": latest_congestion_delta,
                "dns_p95_ms": latest_dns_p95,
                "route_timeout_hops": latest_route_timeout,
                "gateway_packet_loss_pct": gateway_summary["packet_loss_pct"],
                "gateway_latency_p95_ms": gateway_summary["latency_p95_ms"],
                "download_mbps": latest_download_mbps,
                "upload_mbps": latest_upload_mbps,
            }
            evaluated = evaluate_profile(metrics, profile)
            ts = time.strftime("%H:%M:%S")
            latency_str = f"{primary_latencies[0]:.2f}" if primary_latencies else "timeout"
            output_fn(
                f"[{ts}] rtt={latency_str}ms "
                f"jitter={primary_summary['jitter_ms']:.2f}ms "
                f"loss={primary_summary['packet_loss_pct']:.2f}% "
                f"gateway_loss={gateway_summary['packet_loss_pct']:.2f}% "
                f"snr={metrics['snr_db'] if metrics['snr_db'] is not None else 'n/a'} "
                f"verdict={evaluated['verdict']}"
            )
            if evaluated["issues"]:
                top_issue = evaluated["issues"][0]
                output_fn(f"  warning: {top_issue['metric']} -> {top_issue['recommendation']}")

            elapsed = time.time() - tick
            sleep_time = max(0.1, interval - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        output_fn("")
        output_fn("Live monitor stopped by user.")

    total_runtime = int(time.time() - start)
    output_fn(f"Live monitor finished. Runtime: {total_runtime} seconds.")


def _theme_result_headline(theme: str, verdict: str) -> str:
    if theme == "signal_radar":
        return {"PASS": "Link locked", "WARN": "Link usable with caution", "FAIL": "Link unstable"}.get(
            verdict, "Link usable with caution"
        )
    if theme == "vibe_arcade":
        return "Final Vibe"
    return {"PASS": "Ready for this call profile", "WARN": "Usable, but watch the flagged signal", "FAIL": "Not stable enough for this call profile"}.get(
        verdict,
        "Usable, but watch the flagged signal",
    )


def _render_result_summary_panel(theme: str, profile: str, report: dict[str, Any]) -> Any:
    verdict = report.get("overall_verdict", "WARN")
    issues = report.get("profile_result", {}).get("issues") or []
    top_issue = issues[0].get("metric", "none") if issues else "none"
    headline = _theme_result_headline(theme, verdict)
    metrics = report.get("metrics", {})
    use_case_results = report.get("use_case_results", {})
    wifi_perf = report.get("wifi_performance", {})

    # ── Header ──
    header = Table.grid(expand=True)
    header.add_column(justify="left", ratio=2)
    header.add_column(justify="right", ratio=1)

    verdict_text = Text()
    verdict_text.append(f"{_verdict_emoji(verdict)} ", style="bold")
    verdict_text.append(f"{verdict}", style=f"bold {_verdict_style(verdict)}")
    verdict_text.append(f"  •  {headline}", style="dim")

    info_text = Text()
    info_text.append(f"🎯 {profile.upper()}", style="bold bright_yellow")
    info_text.append(f"  •  🎨 {_theme_label(theme)}", style="dim")

    header.add_row(verdict_text, info_text)

    # ── Use-case readiness ──
    readiness_grid = Table.grid(expand=True, padding=(0, 2))
    readiness_grid.add_column(justify="center", ratio=1)
    readiness_grid.add_column(justify="center", ratio=1)
    readiness_grid.add_column(justify="center", ratio=1)
    for case_key, case_label, case_emoji in [("audio", "Audio Call", "📞"), ("video", "Video Call", "📹"), ("video_share", "Video + Share", "🖥️ ")]:
        case = use_case_results.get(case_key, {})
        v = case.get("verdict", "n/a")
        text = Text()
        text.append(f"{case_emoji} {case_label}\n", style="bold")
        text.append(f"{_verdict_emoji(v)} {v}", style=f"bold {_verdict_style(v)}")
        readiness_grid.add_row(text) if case_key == "audio" else None
    readiness_row = Text()
    for case_key, case_label, case_emoji in [("audio", "Audio Call", "📞"), ("video", "Video Call", "📹"), ("video_share", "Video + Share", "🖥️ ")]:
        case = use_case_results.get(case_key, {})
        v = case.get("verdict", "n/a")
        readiness_row.append(f"  {case_emoji} {case_label} ", style="bold")
        readiness_row.append(f"{_verdict_emoji(v)}", style=f"bold {_verdict_style(v)}")

    # ── Key metrics (with friendly labels) ──
    key_metrics_grid = Table(expand=True, show_header=True, header_style="bold bright_cyan", box=None, padding=(0, 1))
    key_metrics_grid.add_column("What", justify="left", ratio=3)
    key_metrics_grid.add_column("Value", justify="right", ratio=2)
    key_metrics_grid.add_column("", justify="center", ratio=1)
    key_metrics_grid.add_column("Meaning", justify="left", ratio=4)
    metric_keys = [
        "latency_p95_ms", "jitter_ms", "packet_loss_pct", "snr_db",
        "dns_p95_ms", "congestion_delta_ms", "download_mbps", "upload_mbps",
    ]
    issue_metrics = {i.get("metric") for i in issues}
    for key in metric_keys:
        friendly_label, unit = get_radar_label(key)
        val = metrics.get(key)
        val_str = _format_metric(val)
        status = "FAIL" if key in issue_metrics and any(i["metric"] == key and i["severity"] == "FAIL" for i in issues) else ("WARN" if key in issue_metrics else "PASS")
        style = _verdict_style(status)
        one_liner = get_radar_one_liner(key, status)
        key_metrics_grid.add_row(
            Text(friendly_label, style="bold"),
            Text(f"{val_str} {unit}", style=f"bold {style}"),
            Text(_status_emoji(status)),
            Text(one_liner, style=f"italic {style}") if one_liner else Text(""),
        )

    # ── WiFi performance ──
    wifi_text = Text()
    wifi_rating = wifi_perf.get("rating", "Unknown")
    wifi_summary = wifi_perf.get("summary", "")
    wifi_text.append(f"📶 Wi-Fi Performance: ", style="bold")
    rating_style = {"Strong": "bold green", "Fair": "bold yellow", "Poor": "bold red"}.get(wifi_rating, "bold")
    wifi_text.append(f"{wifi_rating}", style=rating_style)
    if wifi_summary:
        wifi_text.append(f"\n   {wifi_summary}", style="dim")

    # ── Top issues + friendly recommendations ──
    issues_text = Text()
    if issues:
        issues_text.append("\n⚠️  What's Not Working Well:\n", style="bold yellow")
        seen: set[str] = set()
        for issue in issues[:5]:
            metric_key = issue.get("metric", "")
            severity = issue.get("severity", "WARN")
            friendly = get_friendly_name(metric_key)
            impact = get_impact_description(metric_key, severity)
            friendly_rec = get_friendly_recommendation(metric_key, severity)
            issues_text.append(f"  {_status_emoji(severity)} ", style="bold")
            issues_text.append(f"{friendly}", style=f"bold {_verdict_style(severity)}")
            issues_text.append(f"\n     {impact}\n", style="dim")
            if friendly_rec and friendly_rec not in seen:
                issues_text.append(f"     💡 {friendly_rec}\n", style="dim italic")
                seen.add(friendly_rec)
    else:
        issues_text.append("\n✨ Everything looks great! Your connection is solid.", style="bold green")

    # ── Assemble ──
    body = Table.grid(expand=True)
    body.add_row(Panel(header, border_style=_verdict_style(verdict), padding=(0, 2)))
    body.add_row(Text(""))
    body.add_row(readiness_row)
    body.add_row(Text(""))
    body.add_row(Panel(key_metrics_grid, title="📊 How Did You Do?", border_style="bright_blue", padding=(1, 2)))
    body.add_row(Text(""))
    body.add_row(wifi_text)
    body.add_row(issues_text)

    title = {"signal_radar": "📡 Connection Report", "vibe_arcade": "🕹️  Final Vibe"}.get(theme, "🎛️  Result Summary")
    return Panel(body, title=title, border_style=_verdict_style(verdict), padding=(1, 2))


def _init_tui_state() -> dict[str, Any]:
    return {
        "phase": "Initializing",
        "progress": 0.0,
        "snapshot": None,
        "frame_index": 0,
        "event_count": 0,
        "clean_streak": 0,
        "latency_history": [],
        "jitter_history": [],
        "loss_history": [],
        "wifi_info": {},
    }


def _run_check_tui(profile: str, theme: str, minutes: int, include_speed_test: bool) -> dict:
    console = Console()
    state = _init_tui_state()
    # Pre-fetch Wi-Fi info for display
    try:
        state["wifi_info"] = get_wifi_info()
    except Exception:
        pass
    logs: deque[str] = deque(maxlen=12)

    render = lambda: _render_tui_panel(theme, profile, state, logs)

    with Live(render(), console=console, refresh_per_second=15, screen=True) as live:
        event_cb = _make_tui_event_handler(
            state=state,
            logs=logs,
            update_fn=live.update,
            render_fn=render,
        )
        report = run_check(
            profile=profile,
            minutes=minutes,
            include_speed_test=include_speed_test,
            output_fn=lambda _: None,
            event_cb=event_cb,
            probe_ops={"sample_interval_seconds": 2},
        )

    console.print(_render_result_summary_panel(theme, profile, report))
    return report


def _run_live_monitor_tui(profile: str, theme: str) -> None:
    """Live Monitor with themed TUI panels — same visuals as Quick Check but continuous."""
    console = Console()
    state = _init_tui_state()
    try:
        state["wifi_info"] = get_wifi_info()
    except Exception:
        pass
    logs: deque[str] = deque(maxlen=12)

    interval = 2
    duration_minutes = 180  # effectively indefinite

    target = DEFAULT_PRIMARY_TARGET
    gateway_ip = get_gateway_ip()
    sample_count = max(1, int((duration_minutes * 60) / interval))
    primary_latency_history: deque[float] = deque(maxlen=120)
    primary_outcome_history: deque[bool] = deque(maxlen=120)
    gateway_latency_history: deque[float] = deque(maxlen=60)
    gateway_outcome_history: deque[bool] = deque(maxlen=60)
    latest_wifi: dict[str, Any] = state.get("wifi_info") or {}
    latest_dns_p95: float | None = None
    latest_route_timeout: float | None = None
    latest_congestion_delta: float | None = None
    latest_download_mbps: float | None = None
    latest_upload_mbps: float | None = None

    wifi_stride = max(1, sample_count // 20)
    dns_stride = max(1, sample_count // 15)
    route_stride = max(1, sample_count // 12)
    load_stride = max(1, sample_count // 10)

    render = lambda: _render_tui_panel(theme, profile, state, logs)

    clock = _current_clock_str
    logs.append(f"[{clock()}] [🚀 BOOT] Live Monitor started — {duration_minutes}min @ {interval}s")

    try:
        with Live(render(), console=console, refresh_per_second=15, screen=True) as live:
            for index in range(sample_count):
                tick = time.time()

                primary_ping = ping_target(target, count=1, interval=0.2)
                primary_latencies = primary_ping["latencies_ms"]
                if primary_latencies:
                    primary_latency_history.append(primary_latencies[0])
                    primary_outcome_history.append(True)
                else:
                    primary_outcome_history.append(False)

                if gateway_ip:
                    gw_ping = ping_target(gateway_ip, count=1, interval=0.2)
                    gw_lats = gw_ping["latencies_ms"]
                    if gw_lats:
                        gateway_latency_history.append(gw_lats[0])
                        gateway_outcome_history.append(True)
                    else:
                        gateway_outcome_history.append(False)

                if index % wifi_stride == 0:
                    latest_wifi = get_wifi_info()
                    state["wifi_info"] = latest_wifi
                if index % dns_stride == 0:
                    dns_result = dns_latency(hosts=DNS_HOSTS[:2], attempts=1)
                    latest_dns_p95 = dns_result.get("dns_p95_ms")
                if index % route_stride == 0:
                    route_result = route_snapshot(target)
                    latest_route_timeout = route_result.get("route_timeout_hops")
                if index % load_stride == 0:
                    rolling_primary = summarize_latency(
                        list(primary_latency_history),
                        sent=max(len(primary_outcome_history), 1),
                    )
                    load_result = congestion_probe(target, rolling_primary.get("latency_p95_ms"))
                    latest_congestion_delta = load_result.get("congestion_delta_ms")
                    latest_download_mbps = load_result.get("download_mbps")
                    latest_upload_mbps = load_result.get("upload_mbps")

                primary_summary = summarize_latency(
                    list(primary_latency_history),
                    sent=max(len(primary_outcome_history), 1),
                )

                snapshot = _build_live_snapshot(
                    profile=profile,
                    sample_index=index + 1,
                    sample_count=sample_count,
                    primary_summary=primary_summary,
                    latest_wifi=latest_wifi,
                    latest_dns_p95=latest_dns_p95,
                    latest_route_timeout=latest_route_timeout,
                    latest_congestion_delta=latest_congestion_delta,
                    latest_download_mbps=latest_download_mbps,
                    latest_upload_mbps=latest_upload_mbps,
                )

                progress = ((index + 1) / sample_count) * 100.0
                state["phase"] = f"Live Monitoring {index + 1}/{sample_count}"
                state["progress"] = progress
                state["snapshot"] = snapshot
                state["frame_index"] = int(state.get("frame_index", 0)) + 1

                # Sparkline tracking
                for hist_key, snap_key in [("latency_history", "latency_p95_ms"), ("jitter_history", "jitter_ms"), ("loss_history", "packet_loss_pct")]:
                    val = snapshot.get(snap_key)
                    if val is not None:
                        history = state.setdefault(hist_key, [])
                        history.append(float(val))
                        if len(history) > 60:
                            del history[:len(history) - 60]

                verdict = snapshot.get("verdict")
                if verdict == "PASS":
                    state["clean_streak"] = int(state.get("clean_streak", 0)) + 1
                elif verdict in {"WARN", "FAIL"}:
                    state["clean_streak"] = 0

                if (index + 1) % 5 == 0 or index == 0:
                    logs.append(f"[{clock()}] [📡 SCAN] {_verdict_emoji(verdict)} {verdict} — Sample {index + 1}/{sample_count}")

                live.update(render())

                elapsed = time.time() - tick
                sleep_time = max(0.1, interval - elapsed)
                time.sleep(sleep_time)

            logs.append(f"[{clock()}] [🏁 DONE] Live monitor complete")
            live.update(render())
    except KeyboardInterrupt:
        pass

    console.print(f"\n[bold]Live monitor finished.[/bold] Total samples: {sample_count}")


def _run_wifi_doctor_tui() -> None:
    """Run WiFi Doctor: diagnose issues and offer one-click fixes."""
    console = Console()
    console.print("")
    console.print(Panel(
        Text("🩺 WiFi Doctor is checking your setup...", style="bold bright_cyan"),
        border_style="bright_cyan",
        padding=(1, 2),
    ))

    results = run_diagnostics(output_fn=lambda msg: console.print(f"  {msg}", style="dim"))
    console.print("")

    # Build results panel
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(justify="left")

    fixable_results: list[DiagnosticResult] = []
    for r in results:
        icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}.get(r.status, "❓")
        style = {"ok": "green", "warn": "yellow", "fail": "red"}.get(r.status, "white")
        line = Text()
        line.append(f"{icon} ", style="bold")
        line.append(f"{r.title}: ", style=f"bold {style}")
        line.append(r.detail, style="dim")
        grid.add_row(line)
        if r.fixable and r.status != "ok":
            fix_line = Text()
            fix_line.append(f"     🔧 Can fix: ", style="bold bright_yellow")
            fix_line.append(r.fix_description, style="italic")
            grid.add_row(fix_line)
            fixable_results.append(r)

    ok_count = sum(1 for r in results if r.status == "ok")
    total = len(results)
    if ok_count == total:
        summary_text = "Everything looks great! No issues found."
        summary_style = "bold green"
    elif any(r.status == "fail" for r in results):
        summary_text = f"{total - ok_count} issue(s) found — fixes available below."
        summary_style = "bold red"
    else:
        summary_text = f"{total - ok_count} minor issue(s) — optional fixes below."
        summary_style = "bold yellow"

    console.print(Panel(grid, title="🩺 Diagnosis Results", border_style="bright_cyan", padding=(1, 2)))
    console.print(f"\n  {summary_text}", style=summary_style)

    if not fixable_results:
        console.print("\n  No automated fixes needed. 🎉", style="bold green")
        return

    # Offer fixes
    console.print("")
    console.print("  Would you like to apply the available fixes?", style="bold")
    console.print("  Each fix will be shown before applying — you approve each one.\n", style="dim")

    for i, r in enumerate(fixable_results, 1):
        console.print(Panel(
            Text(f"Fix {i}/{len(fixable_results)}: {r.fix_description}", style="bold bright_yellow"),
            border_style="bright_yellow",
            padding=(0, 2),
        ))
        console.print(f"  Problem: {r.detail}", style="dim")
        answer = Prompt.ask("  Apply this fix?", choices=["y", "n", "skip"], default="y")

        if answer == "n":
            console.print("  Stopping. No more fixes will be applied.\n", style="dim")
            break
        if answer == "skip":
            console.print("  Skipped.\n", style="dim")
            continue

        console.print("  Applying...", style="dim italic")
        success, message = apply_fix(r)
        if success:
            console.print(f"  ✅ {message}\n", style="bold green")
        else:
            console.print(f"  ❌ {message}\n", style="bold red")

    console.print("\n  🩺 WiFi Doctor session complete!\n", style="bold bright_cyan")


def run_menu_tui() -> None:
    if not HAS_RICH:
        run_menu()
        return

    console = Console()
    selected_profile = "video"
    selected_theme = load_settings().get("theme", DEFAULT_THEME)

    while True:
        table = Table.grid(expand=True)
        table.add_row("1) Quick Check (1-2 min)")
        table.add_row("2) Meeting Check (10-15 min)")
        table.add_row("3) Live Monitor")
        table.add_row("4) Choose Call Profile")
        table.add_row("5) Choose Theme")
        table.add_row("6) Run Optional Speed Test")
        table.add_row("7) View Recent History")
        table.add_row(Text("8) 🩺 WiFi Doctor — Diagnose & Fix", style="bold bright_cyan"))
        table.add_row("9) Exit")
        table.add_row("")
        table.add_row(f"Current profile: {selected_profile}")
        table.add_row(f"Current theme: {_theme_label(selected_theme)}")

        console.print(Panel(table, title="Vibe Check WiFi", border_style="blue"))
        choice = Prompt.ask("Select option", choices=["1", "2", "3", "4", "5", "6", "7", "8", "9"], default="1")

        if choice == "9":
            console.print("Exit selected. Bye.")
            return
        if choice == "1":
            _run_check_tui(selected_profile, selected_theme, minutes=1, include_speed_test=True)
            continue
        if choice == "2":
            _run_check_tui(selected_profile, selected_theme, minutes=10, include_speed_test=True)
            continue
        if choice == "3":
            _run_live_monitor_tui(selected_profile, selected_theme)
            continue
        if choice == "4":
            selected_profile = choose_profile(selected_profile)
            continue
        if choice == "5":
            selected_theme = choose_theme(selected_theme)
            save_settings({"theme": selected_theme})
            continue
        if choice == "6":
            _run_check_tui(selected_profile, selected_theme, minutes=1, include_speed_test=True)
            continue
        if choice == "7":
            console.print(Panel(read_recent_history(), title="Recent history", border_style="magenta"))
        if choice == "8":
            _run_wifi_doctor_tui()
            continue


def run_menu(input_fn=input, output_fn=print, handlers: dict[str, Callable[[], None]] | None = None) -> None:
    selected_profile = "video"
    selected_theme = load_settings().get("theme", DEFAULT_THEME)

    def quick_handler() -> None:
        run_check(selected_profile, minutes=1, include_speed_test=True, output_fn=output_fn)

    def meeting_handler() -> None:
        run_check(selected_profile, minutes=10, include_speed_test=True, output_fn=output_fn)

    def live_handler() -> None:
        run_live_monitor(selected_profile, input_fn=input_fn, output_fn=output_fn)

    def profile_handler() -> None:
        nonlocal selected_profile
        selected_profile = choose_profile(selected_profile, input_fn=input_fn, output_fn=output_fn)

    def theme_handler() -> None:
        nonlocal selected_theme
        selected_theme = choose_theme(selected_theme, input_fn=input_fn, output_fn=output_fn)
        save_settings({"theme": selected_theme})

    def speed_handler() -> None:
        run_optional_speed_test(selected_profile, output_fn=output_fn)

    def history_handler() -> None:
        output_fn("")
        output_fn("Recent history")
        output_fn(read_recent_history())

    def doctor_handler() -> None:
        output_fn("")
        output_fn("🩺 WiFi Doctor — Diagnosing your setup...")
        results = run_diagnostics(output_fn=output_fn)
        output_fn("")
        fixable = []
        for r in results:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}.get(r.status, "❓")
            output_fn(f"{icon} {r.title}: {r.detail}")
            if r.fixable and r.status != "ok":
                output_fn(f"     🔧 Can fix: {r.fix_description}")
                fixable.append(r)
        if fixable:
            output_fn("")
            for r in fixable:
                answer = input_fn(f"Apply fix: {r.fix_description}? [y/n]: ").strip().lower()
                if answer in {"y", "yes"}:
                    success, msg = apply_fix(r)
                    output_fn(f"  {'✅' if success else '❌'} {msg}")
        else:
            output_fn("\n✨ No fixes needed!")

    active_handlers = handlers or {
        "1": quick_handler,
        "2": meeting_handler,
        "3": live_handler,
        "4": profile_handler,
        "5": theme_handler,
        "6": speed_handler,
        "7": history_handler,
        "8": doctor_handler,
    }

    while True:
        output_fn("")
        output_fn("Vibe Check WiFi")
        output_fn(f"Current profile: {selected_profile}")
        output_fn(f"Current theme: {_theme_label(selected_theme)}")
        output_fn("1) Quick Check (1-2 min)")
        output_fn("2) Meeting Check (10-15 min)")
        output_fn("3) Live Monitor")
        output_fn("4) Choose Call Profile")
        output_fn("5) Choose Theme")
        output_fn("6) Run Optional Speed Test")
        output_fn("7) View Recent History")
        output_fn("8) 🩺 WiFi Doctor — Diagnose & Fix")
        output_fn("9) Exit")
        choice = input_fn("Select option [1-9]: ").strip()
        if choice == "9":
            output_fn("Exit selected. Bye.")
            return
        handler = active_handlers.get(choice)
        if not handler:
            output_fn("Invalid option. Please choose 1-9.")
            continue
        handler()


def main() -> None:
    run_menu_tui()


if __name__ == "__main__":
    main()
