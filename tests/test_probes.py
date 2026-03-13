import math

import probes
from probes import (
    compute_jitter_ms,
    detect_spikes,
    optional_speed_test,
    parse_airport_output,
    parse_ping_output,
    parse_system_profiler_output,
    percentile,
    summarize_latency,
)


def test_percentile_handles_sorted_values():
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == 25.0
    assert percentile(values, 95) == 38.5


def test_compute_jitter_mean_absolute_delta():
    assert math.isclose(compute_jitter_ms([10.0, 20.0, 40.0]), 15.0, rel_tol=1e-6)


def test_parse_ping_output_extracts_loss_and_latencies():
    sample = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=58 time=14.123 ms
64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=16.111 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=58 time=40.500 ms

--- 1.1.1.1 ping statistics ---
4 packets transmitted, 3 packets received, 25.0% packet loss
round-trip min/avg/max/stddev = 14.123/23.578/40.500/12.345 ms
"""
    parsed = parse_ping_output(sample)
    assert parsed["sent"] == 4
    assert parsed["received"] == 3
    assert parsed["packet_loss_pct"] == 25.0
    assert parsed["latencies_ms"] == [14.123, 16.111, 40.5]


def test_parse_airport_output_extracts_wifi_metrics():
    sample = """     agrCtlRSSI: -55
     agrCtlNoise: -90
     lastTxRate: 867
     channel: 157,80
"""
    parsed = parse_airport_output(sample)
    assert parsed["rssi_dbm"] == -55
    assert parsed["noise_dbm"] == -90
    assert parsed["snr_db"] == 35
    assert parsed["tx_rate_mbps"] == 867
    assert parsed["channel"] == "157,80"


def test_summarize_latency_reports_percentiles_and_spikes():
    summary = summarize_latency([10.0, 12.0, 13.0, 50.0], sent=5)
    assert summary["packet_loss_pct"] == 20.0
    assert summary["latency_p95_ms"] > 40
    assert summary["spike_count"] >= 1


def test_detect_spikes_uses_dynamic_baseline():
    assert detect_spikes([10.0, 11.0, 12.0, 30.0], baseline_ms=10.0) == 1


def test_parse_system_profiler_output_extracts_wifi_metrics():
    sample = """Current Network Information:
            TestWifi:
              Channel: 157 (5GHz, 80MHz)
              Signal / Noise: -73 dBm / -95 dBm
              Transmit Rate: 195
"""
    parsed = parse_system_profiler_output(sample)
    assert parsed["rssi_dbm"] == -73
    assert parsed["noise_dbm"] == -95
    assert parsed["snr_db"] == 22
    assert parsed["tx_rate_mbps"] == 195
    assert parsed["channel"] == "157"
    assert parsed["band"] == "5GHz/6GHz"


def test_optional_speed_test_sends_user_agent_headers(monkeypatch):
    requests = []
    clock_values = iter([0.0, 1.0, 2.0, 3.0])

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self, *_args, **_kwargs):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout=None, context=None):
        requests.append(request)
        payload = b"x" * 1024 if getattr(request, "method", "GET") != "POST" else b""
        return FakeResponse(payload)

    monkeypatch.setattr(probes, "_build_ssl_context", lambda: object())
    monkeypatch.setattr(probes.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(probes.time, "perf_counter", lambda: next(clock_values))
    monkeypatch.setattr(probes.os, "urandom", lambda size: b"u" * size)

    result = optional_speed_test(download_bytes=1024, upload_bytes=512)

    assert result["download_mbps"] is not None
    assert result["upload_mbps"] is not None
    assert requests[0].headers["User-agent"] == "Wi-Fi Readiness/1.0"
    assert requests[1].headers["User-agent"] == "Wi-Fi Readiness/1.0"
