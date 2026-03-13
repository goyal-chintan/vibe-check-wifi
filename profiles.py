from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfileThresholds:
    max_latency_p95_ms: float
    max_jitter_ms: float
    max_packet_loss_pct: float
    min_snr_db: float
    max_congestion_delta_ms: float
    max_dns_p95_ms: float
    max_route_timeout_hops: int
    max_secondary_packet_loss_pct: float
    max_secondary_latency_p95_ms: float
    max_gateway_packet_loss_pct: float
    max_gateway_latency_p95_ms: float
    min_download_mbps: float
    min_upload_mbps: float


PROFILES: dict[str, ProfileThresholds] = {
    "audio": ProfileThresholds(
        max_latency_p95_ms=200.0,
        max_jitter_ms=40.0,
        max_packet_loss_pct=2.0,
        min_snr_db=15.0,
        max_congestion_delta_ms=120.0,
        max_dns_p95_ms=180.0,
        max_route_timeout_hops=1,
        max_secondary_packet_loss_pct=3.0,
        max_secondary_latency_p95_ms=250.0,
        max_gateway_packet_loss_pct=3.0,
        max_gateway_latency_p95_ms=80.0,
        min_download_mbps=2.0,
        min_upload_mbps=1.0,
    ),
    "video": ProfileThresholds(
        max_latency_p95_ms=150.0,
        max_jitter_ms=30.0,
        max_packet_loss_pct=1.0,
        min_snr_db=20.0,
        max_congestion_delta_ms=80.0,
        max_dns_p95_ms=120.0,
        max_route_timeout_hops=1,
        max_secondary_packet_loss_pct=2.0,
        max_secondary_latency_p95_ms=180.0,
        max_gateway_packet_loss_pct=2.0,
        max_gateway_latency_p95_ms=60.0,
        min_download_mbps=5.0,
        min_upload_mbps=2.5,
    ),
    "video_share": ProfileThresholds(
        max_latency_p95_ms=120.0,
        max_jitter_ms=20.0,
        max_packet_loss_pct=0.7,
        min_snr_db=25.0,
        max_congestion_delta_ms=60.0,
        max_dns_p95_ms=100.0,
        max_route_timeout_hops=0,
        max_secondary_packet_loss_pct=1.5,
        max_secondary_latency_p95_ms=150.0,
        max_gateway_packet_loss_pct=1.5,
        max_gateway_latency_p95_ms=50.0,
        min_download_mbps=8.0,
        min_upload_mbps=4.5,
    ),
}


RECOMMENDATIONS = {
    "latency_p95_ms": "If possible, use Ethernet or move closer to the router.",
    "jitter_ms": "Reduce interference: switch to 5 GHz and avoid crowded channels.",
    "packet_loss_pct": "Restart router/modem and check for packet loss on other devices.",
    "snr_db": "Improve signal quality by reducing distance/walls or changing router position.",
    "congestion_delta_ms": "Limit background uploads/downloads during meetings.",
    "dns_p95_ms": "Try reliable DNS resolvers (for example 1.1.1.1 or 8.8.8.8).",
    "route_timeout_hops": "Route instability detected; retry test later or contact ISP.",
    "secondary_packet_loss_pct": "Secondary route is unstable; this suggests upstream fluctuations.",
    "secondary_latency_p95_ms": "Secondary route latency is too high; upstream route may be congested.",
    "gateway_packet_loss_pct": "Local Wi-Fi to router looks unstable; improve signal or router placement.",
    "gateway_latency_p95_ms": "Router response is slow; check local interference and router load.",
    "download_mbps": "Download throughput is low for this use case.",
    "upload_mbps": "Upload throughput is low for this use case, especially for screen sharing.",
}


SEVERITY_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _severity_for_max(observed: float, limit: float, fail_multiplier: float = 1.5) -> str:
    if observed <= limit:
        return "PASS"
    if limit == 0:
        return "WARN" if observed <= 1 else "FAIL"
    if observed <= limit * fail_multiplier:
        return "WARN"
    return "FAIL"


def _severity_for_min(observed: float, minimum: float) -> str:
    if observed >= minimum:
        return "PASS"
    if observed >= minimum * 0.75:
        return "WARN"
    return "FAIL"


def _add_issue(
    issues: list[dict[str, Any]],
    metric: str,
    severity: str,
    observed: float,
    limit: float,
) -> None:
    if severity == "PASS":
        return
    issues.append(
        {
            "metric": metric,
            "severity": severity,
            "observed": round(observed, 2),
            "limit": limit,
            "recommendation": RECOMMENDATIONS.get(metric, "Investigate this metric."),
        }
    )


def evaluate_profile(metrics: dict[str, Any], profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    thresholds = PROFILES[profile]
    issues: list[dict[str, Any]] = []

    max_checks = [
        ("latency_p95_ms", thresholds.max_latency_p95_ms, 1.5),
        ("jitter_ms", thresholds.max_jitter_ms, 1.5),
        ("packet_loss_pct", thresholds.max_packet_loss_pct, 2.0),
        ("congestion_delta_ms", thresholds.max_congestion_delta_ms, 1.5),
        ("dns_p95_ms", thresholds.max_dns_p95_ms, 1.5),
        ("route_timeout_hops", float(thresholds.max_route_timeout_hops), 2.5),
        ("secondary_packet_loss_pct", thresholds.max_secondary_packet_loss_pct, 1.5),
        ("secondary_latency_p95_ms", thresholds.max_secondary_latency_p95_ms, 1.5),
        ("gateway_packet_loss_pct", thresholds.max_gateway_packet_loss_pct, 1.5),
        ("gateway_latency_p95_ms", thresholds.max_gateway_latency_p95_ms, 1.5),
    ]
    for metric, limit, fail_multiplier in max_checks:
        observed = metrics.get(metric)
        if observed is None:
            continue
        severity = _severity_for_max(float(observed), float(limit), fail_multiplier=fail_multiplier)
        _add_issue(issues, metric, severity, float(observed), float(limit))

    snr_value = metrics.get("snr_db")
    if snr_value is not None:
        snr_severity = _severity_for_min(float(snr_value), thresholds.min_snr_db)
        _add_issue(issues, "snr_db", snr_severity, float(snr_value), thresholds.min_snr_db)

    download_mbps = metrics.get("download_mbps")
    if download_mbps is not None:
        download_severity = _severity_for_min(float(download_mbps), thresholds.min_download_mbps)
        _add_issue(issues, "download_mbps", download_severity, float(download_mbps), thresholds.min_download_mbps)

    upload_mbps = metrics.get("upload_mbps")
    if upload_mbps is not None:
        upload_severity = _severity_for_min(float(upload_mbps), thresholds.min_upload_mbps)
        _add_issue(issues, "upload_mbps", upload_severity, float(upload_mbps), thresholds.min_upload_mbps)

    verdict = "PASS"
    for issue in issues:
        if SEVERITY_RANK[issue["severity"]] > SEVERITY_RANK[verdict]:
            verdict = issue["severity"]

    return {
        "profile": profile,
        "verdict": verdict,
        "issues": issues,
    }
