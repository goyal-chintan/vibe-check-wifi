import vibe_check_wifi


def test_live_monitor_uses_measured_metrics(monkeypatch):
    prompts = iter(["30", "1"])
    outputs = []
    captured_metrics = []

    monkeypatch.setattr(vibe_check_wifi, "get_gateway_ip", lambda: "192.168.1.1")
    monkeypatch.setattr(
        vibe_check_wifi,
        "ping_target",
        lambda target, count, interval: {
            "sent": 1,
            "latencies_ms": [20.0] if target != "192.168.1.1" else [12.0],
        },
    )
    monkeypatch.setattr(vibe_check_wifi, "get_wifi_info", lambda: {"snr_db": 11})
    monkeypatch.setattr(vibe_check_wifi, "dns_latency", lambda hosts=None, attempts=1: {"dns_p95_ms": 123.0})
    monkeypatch.setattr(vibe_check_wifi, "route_snapshot", lambda target: {"route_timeout_hops": 2})
    monkeypatch.setattr(
        vibe_check_wifi,
        "congestion_probe",
        lambda target, baseline: {
            "congestion_delta_ms": 77.0,
            "download_mbps": 5.0,
            "upload_mbps": 1.0,
        },
    )
    monkeypatch.setattr(vibe_check_wifi.time, "sleep", lambda _seconds: None)

    def fake_evaluate(metrics, profile):
        captured_metrics.append(metrics)
        return {"verdict": "WARN", "issues": []}

    monkeypatch.setattr(vibe_check_wifi, "evaluate_profile", fake_evaluate)

    vibe_check_wifi.run_live_monitor(
        profile="video",
        input_fn=lambda _prompt: next(prompts),
        output_fn=outputs.append,
    )

    assert captured_metrics
    first = captured_metrics[0]
    assert first["snr_db"] == 11
    assert first["dns_p95_ms"] == 123.0
    assert first["route_timeout_hops"] == 2
    assert first["congestion_delta_ms"] == 77.0
    assert first["download_mbps"] == 5.0
    assert first["upload_mbps"] == 1.0
