from collections import deque

from rich.console import Console

from vibe_check_wifi import _render_tui_panel


def _render_text(theme: str) -> str:
    state = {
        "phase": "Monitoring interval 3/12",
        "progress": 42.0,
        "snapshot": {
            "sample_index": 3,
            "sample_count": 12,
            "verdict": "WARN",
            "top_issue_metric": "jitter_ms",
            "latency_p95_ms": 91.2,
            "jitter_ms": 35.5,
            "packet_loss_pct": 0.8,
            "snr_db": 21.0,
            "dns_p95_ms": 104.2,
            "route_timeout_hops": 1,
            "congestion_delta_ms": 46.0,
            "download_mbps": 78.6,
            "upload_mbps": 22.1,
        },
        "frame_index": 5,
        "event_count": 13,
        "clean_streak": 0,
    }
    logs = deque(
        [
            "[12:00:01] Starting diagnostics",
            "[12:00:03] Monitoring all core signals over time",
            "[12:00:08] Monitoring interval 3/12",
        ],
        maxlen=8,
    )
    console = Console(width=100, record=True)
    console.print(_render_tui_panel(theme, "video", state, logs))
    return console.export_text()


def test_theme_renderers_are_structurally_distinct():
    studio = _render_text("studio_board")
    radar = _render_text("signal_radar")
    arcade = _render_text("vibe_arcade")

    assert "Studio Board" in studio
    assert "Signal Radar" in radar
    assert "Vibe Arcade" in arcade
    assert "Latency p95" in studio
    assert "How's Your Connection?" in radar
    assert "VIBE METER" in arcade
    assert studio != radar
    assert radar != arcade
    assert studio != arcade


def test_theme_renderers_show_live_metrics():
    text = _render_text("studio_board")
    assert "91.20" in text
    assert "35.50" in text
    assert "104.20" in text
