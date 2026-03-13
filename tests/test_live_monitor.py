import wifi_call_health


def test_live_monitor_uses_measured_metrics(monkeypatch):
    prompts = iter(["30", "1"])
    outputs = []
    captured_metrics = []

    monkeypatch.setattr(wifi_call_health, "get_gateway_ip", lambda: "192.168.1.1")
    monkeypatch.setattr(
        wifi_call_health,
        "ping_target",
        lambda target, count, interval: {
            "sent": 1,
            "latencies_ms": [20.0] if target != "192.168.1.1" else [12.0],
        },
    )
    monkeypatch.setattr(wifi_call_health, "get_wifi_info", lambda: {"snr_db": 11})
    monkeypatch.setattr(wifi_call_health, "dns_latency", lambda hosts=None, attempts=1: {"dns_p95_ms": 123.0})
    monkeypatch.setattr(wifi_call_health, "route_snapshot", lambda target: {"route_timeout_hops": 2})
    monkeypatch.setattr(
        wifi_call_health,
        "congestion_probe",
        lambda target, baseline: {
            "congestion_delta_ms": 77.0,
            "download_mbps": 5.0,
            "upload_mbps": 1.0,
        },
    )
    monkeypatch.setattr(wifi_call_health.time, "sleep", lambda _seconds: None)

    def fake_evaluate(metrics, profile):
        captured_metrics.append(metrics)
        return {"verdict": "WARN", "issues": []}

    monkeypatch.setattr(wifi_call_health, "evaluate_profile", fake_evaluate)

    wifi_call_health.run_live_monitor(
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
