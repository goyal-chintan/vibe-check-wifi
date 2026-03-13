from profiles import evaluate_profile


def test_video_profile_passes_good_metrics():
    metrics = {
        "latency_p95_ms": 90.0,
        "jitter_ms": 12.0,
        "packet_loss_pct": 0.2,
        "snr_db": 30.0,
        "congestion_delta_ms": 20.0,
        "dns_p95_ms": 40.0,
        "route_timeout_hops": 0,
    }
    report = evaluate_profile(metrics, "video")
    assert report["verdict"] == "PASS"
    assert report["issues"] == []


def test_video_share_profile_fails_bad_metrics():
    metrics = {
        "latency_p95_ms": 250.0,
        "jitter_ms": 60.0,
        "packet_loss_pct": 4.0,
        "snr_db": 10.0,
        "congestion_delta_ms": 180.0,
        "dns_p95_ms": 220.0,
        "route_timeout_hops": 4,
    }
    report = evaluate_profile(metrics, "video_share")
    assert report["verdict"] == "FAIL"
    assert len(report["issues"]) >= 4
    assert any("jitter" in issue["metric"] for issue in report["issues"])


def test_audio_profile_warns_on_route_instability():
    metrics = {
        "latency_p95_ms": 80.0,
        "jitter_ms": 9.0,
        "packet_loss_pct": 0.0,
        "snr_db": 28.0,
        "congestion_delta_ms": 10.0,
        "dns_p95_ms": 50.0,
        "route_timeout_hops": 2,
    }
    report = evaluate_profile(metrics, "audio")
    assert report["verdict"] == "WARN"
    assert any(issue["metric"] == "route_timeout_hops" for issue in report["issues"])


def test_video_share_profile_warns_on_single_route_timeout_hop():
    metrics = {
        "latency_p95_ms": 80.0,
        "jitter_ms": 12.0,
        "packet_loss_pct": 0.0,
        "snr_db": 28.0,
        "congestion_delta_ms": 20.0,
        "dns_p95_ms": 60.0,
        "route_timeout_hops": 1,
    }
    report = evaluate_profile(metrics, "video_share")
    assert report["verdict"] == "WARN"
    assert any(issue["metric"] == "route_timeout_hops" for issue in report["issues"])


def test_video_share_profile_fails_low_upload_throughput():
    metrics = {
        "latency_p95_ms": 70.0,
        "jitter_ms": 10.0,
        "packet_loss_pct": 0.0,
        "snr_db": 30.0,
        "congestion_delta_ms": 10.0,
        "dns_p95_ms": 40.0,
        "route_timeout_hops": 0,
        "download_mbps": 50.0,
        "upload_mbps": 0.9,
    }
    report = evaluate_profile(metrics, "video_share")
    assert report["verdict"] == "FAIL"
    assert any(issue["metric"] == "upload_mbps" for issue in report["issues"])


def test_video_profile_warns_on_gateway_loss():
    metrics = {
        "latency_p95_ms": 70.0,
        "jitter_ms": 10.0,
        "packet_loss_pct": 0.0,
        "snr_db": 30.0,
        "congestion_delta_ms": 10.0,
        "dns_p95_ms": 40.0,
        "route_timeout_hops": 0,
        "gateway_packet_loss_pct": 2.5,
        "gateway_latency_p95_ms": 20.0,
    }
    report = evaluate_profile(metrics, "video")
    assert report["verdict"] == "WARN"
    assert any(issue["metric"] == "gateway_packet_loss_pct" for issue in report["issues"])


def test_video_profile_warns_on_high_secondary_latency():
    metrics = {
        "latency_p95_ms": 70.0,
        "jitter_ms": 10.0,
        "packet_loss_pct": 0.0,
        "snr_db": 30.0,
        "congestion_delta_ms": 10.0,
        "dns_p95_ms": 40.0,
        "route_timeout_hops": 0,
        "secondary_packet_loss_pct": 0.0,
        "secondary_latency_p95_ms": 260.0,
        "gateway_packet_loss_pct": 0.0,
        "gateway_latency_p95_ms": 20.0,
    }
    report = evaluate_profile(metrics, "video")
    assert report["verdict"] == "WARN"
    assert any(issue["metric"] == "secondary_latency_p95_ms" for issue in report["issues"])
