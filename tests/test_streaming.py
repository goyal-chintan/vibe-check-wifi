from collections import deque

from vibe_check_wifi import _make_tui_event_handler, execute_assessment_streamed


def test_execute_assessment_streamed_emits_progress_and_builds_metrics():
    events = []

    def cb(event_type, detail, progress):
        events.append((event_type, detail, progress))

    probe_ops = {
        "duration_seconds_override": 2,
        "sample_interval_seconds": 1,
        "primary_target": "1.1.1.1",
        "secondary_target": "8.8.8.8",
        "get_wifi_info": lambda: {"snr_db": 30, "rssi_dbm": -60, "noise_dbm": -90, "tx_rate_mbps": 500},
        "get_gateway_ip": lambda: "192.168.1.1",
        "ping_target": lambda target, count, interval: {
            "target": target,
            "sent": count,
            "latencies_ms": [20.0, 25.0, 30.0],
            "packet_loss_pct": 0.0,
        },
        "summarize_latency": lambda latencies, sent: {
            "sent": sent,
            "received": len(latencies),
            "packet_loss_pct": 0.0,
            "latency_p50_ms": 25.0,
            "latency_p95_ms": 30.0,
            "jitter_ms": 5.0,
            "spike_count": 0,
            "dropout_count": 0,
        },
        "dns_latency": lambda: {"dns_p95_ms": 40.0, "dns_failures": 0},
        "route_snapshot": lambda target: {"route_timeout_hops": 0},
        "congestion_probe": lambda target, baseline: {"congestion_delta_ms": 5.0},
        "optional_speed_test": lambda: {"download_mbps": 120.0, "upload_mbps": 20.0, "error": None},
    }

    report = execute_assessment_streamed(
        profile="video",
        minutes=1,
        include_speed_test=False,
        event_cb=cb,
        probe_ops=probe_ops,
    )

    assert report["metrics"]["snr_db"] == 30
    assert report["metrics"]["dns_p95_ms"] == 40.0
    assert report["metrics"]["latency_p95_ms"] == 30.0
    assert "secondary_packet_loss_pct" in report["metrics"]
    assert "gateway_packet_loss_pct" in report["metrics"]
    assert any(event[0] == "phase_start" for event in events)
    assert events[-1][0] == "done"


def test_tui_event_handler_refreshes_live_renderable():
    state = {"phase": "Initializing", "progress": 0.0}
    logs = deque(maxlen=8)
    updates = []

    def render():
        return {"phase": state["phase"], "progress": state["progress"], "logs": list(logs)}

    handler = _make_tui_event_handler(
        state=state,
        logs=logs,
        update_fn=lambda renderable: updates.append(renderable),
        render_fn=render,
        clock=lambda: "12:00:00",
    )

    handler("phase_start", "Scanning Wi-Fi link quality", 10.0)

    assert state["phase"] == "Scanning Wi-Fi link quality"
    assert state["progress"] == 10.0
    assert list(logs) == ["[12:00:00] Scanning Wi-Fi link quality"]
    assert updates[-1]["phase"] == "Scanning Wi-Fi link quality"
