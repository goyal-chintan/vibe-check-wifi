from reporting import format_report


def test_report_contains_human_readable_sections():
    report = {
        "overall_verdict": "WARN",
        "profile": "video",
        "profile_result": {
            "verdict": "WARN",
            "issues": [
                {
                    "metric": "jitter_ms",
                    "severity": "WARN",
                    "observed": 34.0,
                    "limit": 30.0,
                    "recommendation": "Move closer to the router or use 5 GHz.",
                }
            ],
        },
        "metrics": {
            "latency_p95_ms": 110.0,
            "jitter_ms": 34.0,
            "packet_loss_pct": 0.9,
            "snr_db": 21.0,
            "congestion_delta_ms": 50.0,
            "dns_p95_ms": 42.0,
            "route_timeout_hops": 1,
        },
        "use_case_results": {
            "audio": {"verdict": "PASS"},
            "video": {"verdict": "WARN"},
            "video_share": {"verdict": "FAIL"},
        },
        "wifi_performance": {
            "rating": "Fair",
            "summary": "Usable, but signal quality is borderline for heavier meeting workloads.",
        },
        "speed_test": {
            "download_mbps": 120.5,
            "upload_mbps": 18.2,
            "error": None,
        },
    }

    text = format_report(report)

    assert "Overall verdict: WARN" in text
    assert "Vibe Check WiFi" in text
    assert "Profile verdict: WARN (video)" in text
    assert "Connection speed" in text
    assert "Wi-Fi performance" in text
    assert "Use-case readiness" in text
    assert "Audio call: PASS" in text
    assert "Video + Screen-share call: FAIL" in text
    assert "Top problems detected" in text
    assert "Detailed metrics" in text
    assert "What to do next" in text
