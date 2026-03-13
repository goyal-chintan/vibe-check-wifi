from __future__ import annotations

import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

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


EventCallback = Callable[[str, str, float], None]

PROFILE_CHOICES = {
    "1": ("audio", "Audio call"),
    "2": ("video", "Video call"),
    "3": ("video_share", "Video + Screen-share call"),
}


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


def _render_tui_panel(profile: str, state: dict[str, Any], logs: deque[str]) -> Any:
    menu = Table.grid(expand=True)
    menu.add_column(justify="left")
    menu.add_column(justify="right")
    menu.add_row("Profile", profile)
    menu.add_row("Current phase", state["phase"])
    menu.add_row("Progress", f"{state['progress']:.0f}%")

    body = Table.grid(expand=True)
    body.add_row(Panel(menu, title="Live Diagnostics", border_style="cyan"))
    body.add_row(ProgressBar(total=100, completed=state["progress"], width=70))

    log_text = "\n".join(logs) if logs else "Waiting for probes to start..."
    body.add_row(Panel(Text(log_text), title="Activity", border_style="magenta"))
    return Panel(body, title="Vibe Check WiFi", border_style="green")


def _make_tui_event_handler(
    state: dict[str, Any],
    logs: deque[str],
    update_fn: Callable[[Any], None],
    render_fn: Callable[[], Any],
    clock: Callable[[], str] = _current_clock_str,
) -> EventCallback:
    def event_cb(event_type: str, detail: str, progress: float) -> None:
        timestamp = clock()
        if event_type in {"start", "phase_start", "phase_done", "done"}:
            logs.append(f"[{timestamp}] {detail}")
        state["phase"] = detail
        state["progress"] = progress
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

    def emit(event_type: str, detail: str, progress: float) -> None:
        if event_cb is not None:
            event_cb(event_type, detail, max(0.0, min(progress, 100.0)))

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

    emit("start", "Starting diagnostics", 0.0)
    emit("phase_start", "Monitoring all core signals over time", 0.0)

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
            wifi_samples.append(_call_with_fallback(ops["get_wifi_info"]))

        if idx % dns_stride == 0:
            dns_result = _call_with_fallback(ops["dns_latency"], hosts=DNS_HOSTS[:2], attempts=1)
            dns_failures = dns_result.get("dns_failures")
            dns_p95 = dns_result.get("dns_p95_ms")
            if dns_failures is not None:
                dns_failure_count += int(dns_failures)
            if dns_p95 is not None:
                dns_p95_samples.append(float(dns_p95))

        if idx % route_stride == 0:
            route_result = _call_with_fallback(ops["route_snapshot"], primary_target)
            route_timeout = route_result.get("route_timeout_hops")
            if route_timeout is not None:
                route_timeout_samples.append(float(route_timeout))

        if idx % load_stride == 0:
            rolling_primary = ops["summarize_latency"](primary_latencies, sent=max(primary_sent, 1))
            load_result = _call_with_fallback(
                ops["congestion_probe"],
                primary_target,
                rolling_primary.get("latency_p95_ms"),
            )
            if load_result.get("congestion_delta_ms") is not None:
                congestion_delta_samples.append(float(load_result["congestion_delta_ms"]))
            if load_result.get("download_mbps") is not None:
                download_mbps_samples.append(float(load_result["download_mbps"]))
            if load_result.get("upload_mbps") is not None:
                upload_mbps_samples.append(float(load_result["upload_mbps"]))

        progress = ((idx + 1) / loop_count) * 90.0
        emit("phase_tick", f"Monitoring interval {idx + 1}/{loop_count}", progress)

        elapsed = time.time() - tick_start
        sleep_for = max(0.0, sample_interval_seconds - elapsed)
        if idx < loop_count - 1 and sleep_for > 0:
            time.sleep(sleep_for)

    emit("phase_done", "Monitoring all core signals over time", 90.0)

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
    latest_wifi = wifi_samples[-1] if wifi_samples else _call_with_fallback(ops["get_wifi_info"])

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

    emit("done", "Diagnostics complete", 100.0)
    return result


def run_check(
    profile: str,
    minutes: int,
    include_speed_test: bool,
    output_fn=print,
    event_cb: EventCallback | None = None,
) -> dict:
    output_fn("")
    output_fn(f"Running {minutes}-minute check for profile: {profile}")
    assessment = execute_assessment_streamed(
        profile=profile,
        minutes=minutes,
        include_speed_test=include_speed_test,
        event_cb=event_cb,
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
    output_fn("Live monitor mode")
    interval = _prompt_int(input_fn, "Sampling interval in seconds [default 5]: ", default=5, minimum=2, maximum=30)
    duration_minutes = _prompt_int(
        input_fn,
        "How many minutes to monitor [default 10]: ",
        default=10,
        minimum=1,
        maximum=180,
    )
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


def _run_check_tui(profile: str, minutes: int, include_speed_test: bool) -> dict:
    console = Console()
    state = {
        "phase": "Initializing",
        "progress": 0.0,
    }
    logs: deque[str] = deque(maxlen=8)

    render = lambda: _render_tui_panel(profile, state, logs)

    with Live(render(), console=console, refresh_per_second=8, screen=True) as live:
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
        )

    verdict = report.get("overall_verdict", "WARN")
    color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(verdict, "cyan")
    console.print(Panel(format_report(report), title="Result", border_style=color))
    return report


def run_menu_tui() -> None:
    if not HAS_RICH:
        run_menu()
        return

    console = Console()
    selected_profile = "video"

    while True:
        table = Table.grid(expand=True)
        table.add_row("1) Quick Check (1-2 min)")
        table.add_row("2) Meeting Check (10-15 min)")
        table.add_row("3) Live Monitor")
        table.add_row("4) Choose Call Profile")
        table.add_row("5) Run Optional Speed Test")
        table.add_row("6) View Recent History")
        table.add_row("7) Exit")
        table.add_row("")
        table.add_row(f"Current profile: {selected_profile}")

        console.print(Panel(table, title="Vibe Check WiFi", border_style="blue"))
        choice = Prompt.ask("Select option", choices=["1", "2", "3", "4", "5", "6", "7"], default="1")

        if choice == "7":
            console.print("Exit selected. Bye.")
            return
        if choice == "1":
            minutes = _prompt_int(input, "Quick check minutes [default 2]: ", default=2, minimum=1, maximum=5)
            include_speed = _prompt_yes_no(input, "Include optional speed test? [y/N]: ", default=False)
            _run_check_tui(selected_profile, minutes=minutes, include_speed_test=include_speed)
            continue
        if choice == "2":
            minutes = _prompt_int(input, "Meeting check minutes [default 15]: ", default=15, minimum=10, maximum=60)
            include_speed = _prompt_yes_no(input, "Include optional speed test? [y/N]: ", default=False)
            _run_check_tui(selected_profile, minutes=minutes, include_speed_test=include_speed)
            continue
        if choice == "3":
            run_live_monitor(selected_profile)
            continue
        if choice == "4":
            selected_profile = choose_profile(selected_profile)
            continue
        if choice == "5":
            _run_check_tui(selected_profile, minutes=1, include_speed_test=True)
            continue
        if choice == "6":
            console.print(Panel(read_recent_history(), title="Recent history", border_style="magenta"))


def run_menu(input_fn=input, output_fn=print, handlers: dict[str, Callable[[], None]] | None = None) -> None:
    selected_profile = "video"

    def quick_handler() -> None:
        minutes = _prompt_int(input_fn, "Quick check minutes [default 2]: ", default=2, minimum=1, maximum=5)
        include_speed = _prompt_yes_no(input_fn, "Include optional speed test? [y/N]: ", default=False)
        run_check(selected_profile, minutes=minutes, include_speed_test=include_speed, output_fn=output_fn)

    def meeting_handler() -> None:
        minutes = _prompt_int(
            input_fn,
            "Meeting check minutes [default 15]: ",
            default=15,
            minimum=10,
            maximum=60,
        )
        include_speed = _prompt_yes_no(input_fn, "Include optional speed test? [y/N]: ", default=False)
        run_check(selected_profile, minutes=minutes, include_speed_test=include_speed, output_fn=output_fn)

    def live_handler() -> None:
        run_live_monitor(selected_profile, input_fn=input_fn, output_fn=output_fn)

    def profile_handler() -> None:
        nonlocal selected_profile
        selected_profile = choose_profile(selected_profile, input_fn=input_fn, output_fn=output_fn)

    def speed_handler() -> None:
        run_optional_speed_test(selected_profile, output_fn=output_fn)

    def history_handler() -> None:
        output_fn("")
        output_fn("Recent history")
        output_fn(read_recent_history())

    active_handlers = handlers or {
        "1": quick_handler,
        "2": meeting_handler,
        "3": live_handler,
        "4": profile_handler,
        "5": speed_handler,
        "6": history_handler,
    }

    while True:
        output_fn("")
        output_fn("Vibe Check WiFi")
        output_fn(f"Current profile: {selected_profile}")
        output_fn("1) Quick Check (1-2 min)")
        output_fn("2) Meeting Check (10-15 min)")
        output_fn("3) Live Monitor")
        output_fn("4) Choose Call Profile")
        output_fn("5) Run Optional Speed Test")
        output_fn("6) View Recent History")
        output_fn("7) Exit")
        choice = input_fn("Select option [1-7]: ").strip()
        if choice == "7":
            output_fn("Exit selected. Bye.")
            return
        handler = active_handlers.get(choice)
        if not handler:
            output_fn("Invalid option. Please choose 1-7.")
            continue
        handler()


def main() -> None:
    run_menu_tui()


if __name__ == "__main__":
    main()
