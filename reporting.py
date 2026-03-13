from __future__ import annotations

from typing import Any


PROFILE_LABELS = {
    "audio": "Audio call",
    "video": "Video call",
    "video_share": "Video + Screen-share call",
}


METRIC_LABELS = {
    "latency_p50_ms": "Latency p50 (ms)",
    "latency_p95_ms": "Latency p95 (ms)",
    "jitter_ms": "Jitter (ms)",
    "packet_loss_pct": "Packet loss (%)",
    "secondary_packet_loss_pct": "Secondary path packet loss (%)",
    "secondary_latency_p95_ms": "Secondary path latency p95 (ms)",
    "gateway_packet_loss_pct": "Gateway packet loss (%)",
    "gateway_latency_p95_ms": "Gateway latency p95 (ms)",
    "spike_count": "Latency spikes",
    "dropout_count": "Dropouts",
    "snr_db": "SNR (dB)",
    "rssi_dbm": "RSSI (dBm)",
    "noise_dbm": "Noise (dBm)",
    "tx_rate_mbps": "Tx rate (Mbps)",
    "dns_p95_ms": "DNS p95 (ms)",
    "dns_failures": "DNS failures",
    "route_timeout_hops": "Route timeout hops",
    "congestion_delta_ms": "Latency under load delta (ms)",
    "download_mbps": "Download throughput (Mbps)",
    "upload_mbps": "Upload throughput (Mbps)",
}


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _issue_line(issue: dict[str, Any]) -> str:
    return (
        f"- [{issue['severity']}] {issue['metric']}: observed {issue['observed']} "
        f"(target <= {issue['limit']})"
    )


def _format_use_case_results(use_case_results: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for key in ("audio", "video", "video_share"):
        result = use_case_results.get(key)
        if not result:
            continue
        lines.append(f"- {PROFILE_LABELS.get(key, key)}: {result.get('verdict', 'n/a')}")
    return lines


def format_report(report: dict[str, Any]) -> str:
    profile = report.get("profile", "video")
    profile_name = PROFILE_LABELS.get(profile, profile)
    profile_result = report.get("profile_result", {})
    metrics = report.get("metrics", {})
    issues = profile_result.get("issues", [])
    overall_verdict = report.get("overall_verdict", profile_result.get("verdict", "WARN"))
    use_case_results = report.get("use_case_results", {})
    wifi_performance = report.get("wifi_performance", {})

    lines: list[str] = []
    lines.append("Vibe Check WiFi")
    lines.append(f"Overall verdict: {overall_verdict}")
    lines.append(f"Profile verdict: {profile_result.get('verdict', 'WARN')} ({profile})")
    lines.append(f"Profile type: {profile_name}")

    lines.append("")
    lines.append("Connection speed")
    speed = report.get("speed_test")
    if speed:
        if speed.get("error"):
            lines.append(f"- Could not complete speed test: {speed['error']}")
        else:
            lines.append(f"- Download (Mbps): {_format_value(speed.get('download_mbps'))}")
            lines.append(f"- Upload (Mbps): {_format_value(speed.get('upload_mbps'))}")
    else:
        lines.append("- Not measured in this run.")

    lines.append("")
    lines.append("Wi-Fi performance")
    if wifi_performance:
        lines.append(f"- Rating: {wifi_performance.get('rating', 'Unknown')}")
        summary = wifi_performance.get("summary")
        if summary:
            lines.append(f"- {summary}")
    else:
        lines.append("- No Wi-Fi performance summary available.")

    lines.append("")
    lines.append("Use-case readiness")
    readiness_lines = _format_use_case_results(use_case_results)
    if readiness_lines:
        lines.extend(readiness_lines)
    else:
        lines.append("- No use-case readiness summary available.")

    lines.append("")
    lines.append("Top problems detected")
    if issues:
        for issue in issues[:5]:
            lines.append(_issue_line(issue))
    else:
        lines.append("- No major issues detected for this profile.")

    lines.append("")
    lines.append("Detailed metrics")
    for key, label in METRIC_LABELS.items():
        if key in metrics:
            lines.append(f"- {label}: {_format_value(metrics[key])}")

    lines.append("")
    lines.append("What to do next")
    if issues:
        seen: set[str] = set()
        for issue in issues:
            recommendation = issue.get("recommendation")
            if recommendation and recommendation not in seen:
                lines.append(f"- {recommendation}")
                seen.add(recommendation)
    else:
        lines.append("- Connection looks stable for this profile right now.")
        lines.append("- Keep live monitor running during calls to catch intermittent spikes.")

    return "\n".join(lines)
