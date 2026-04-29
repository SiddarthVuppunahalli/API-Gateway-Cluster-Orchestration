import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from load_spike import print_summary, run_load_test


def log(message: str) -> None:
    print(f"[benchmark] {message}", flush=True)


def extract_port(gateway_url: str) -> int:
    match = re.search(r":(\d+)", gateway_url)
    if not match:
        return 8080
    return int(match.group(1))


def fetch_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_gateway(base_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = None
    log(f"waiting for gateway health at {base_url}/healthz")
    while time.time() < deadline:
        if process.poll() is not None:
            output = read_gateway_log(process)
            raise RuntimeError(
                "gateway process exited before becoming ready. "
                f"Output:\n{output}"
            )
        try:
            fetch_json(f"{base_url}/healthz", timeout=2.0)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.25)

    output = read_gateway_log(process)
    raise RuntimeError(
        f"gateway did not become ready before timeout: {last_error}\n"
        "Gateway output:\n"
        f"{output}"
    )


def wait_for_strategy(base_url: str, expected_strategy: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_stats = None
    log(f"verifying gateway strategy is {expected_strategy}")
    while time.time() < deadline:
        try:
            stats = fetch_json(f"{base_url}/stats", timeout=2.0)
            last_stats = stats
            actual = stats.get("router", {}).get("strategy")
            if actual == expected_strategy:
                return
        except Exception:
            pass
        time.sleep(0.25)

    raise RuntimeError(
        f"gateway strategy did not match expected value {expected_strategy}. "
        f"Last observed stats: {last_stats}"
    )


def start_gateway(repo_root: Path, strategy: str, request_timeout: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["ROUTING_STRATEGY"] = strategy
    env["REQUEST_TIMEOUT_SECONDS"] = str(request_timeout)
    log_file = tempfile.NamedTemporaryFile(
        mode="w+",
        encoding="utf-8",
        prefix=f"gateway-{strategy}-",
        suffix=".log",
        delete=False,
    )
    log(
        f"starting gateway with strategy={strategy} "
        f"request_timeout_seconds={request_timeout}"
    )
    process = subprocess.Popen(
        ["go", "run", "./gateway/cmd/server"],
        cwd=repo_root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process.log_path = log_file.name  # type: ignore[attr-defined]
    log_file.close()
    return process


def stop_gateway(process: subprocess.Popen) -> str:
    if process.poll() is None:
        log("stopping gateway")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("gateway did not exit on terminate, killing process")
            process.kill()
            process.wait(timeout=5)

    return read_gateway_log(process)


def read_gateway_log(process: subprocess.Popen) -> str:
    log_path = getattr(process, "log_path", "")
    if not log_path:
        return ""
    try:
        return Path(log_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def cleanup_gateway_port(port: int) -> None:
    pid = find_listener_pid(port)
    if pid is None:
        log(f"no existing listener found on port {port}")
        return

    log(f"found existing listener on port {port} with pid={pid}, attempting cleanup")
    terminate_pid(pid)

    # Give the OS a moment to release the port.
    for _ in range(20):
        if find_listener_pid(port) is None:
            log(f"port {port} is clear")
            return
        time.sleep(0.25)

    raise RuntimeError(f"failed to clear existing listener on port {port}")


def find_listener_pid(port: int) -> int | None:
    if sys.platform == "win32":
        return find_listener_pid_windows(port)
    return find_listener_pid_unix(port)


def find_listener_pid_windows(port: int) -> int | None:
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        check=False,
    )
    pattern = f":{port}"
    for line in result.stdout.splitlines():
        if "LISTENING" not in line or pattern not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def find_listener_pid_unix(port: int) -> int | None:
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line)
        except ValueError:
            continue
    return None


def terminate_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def run_strategy(
    repo_root: Path,
    gateway_url: str,
    gateway_port: int,
    strategy: str,
    requests: int,
    concurrency: int,
    prompt_size: int,
    max_tokens: int,
    timeout: float,
    request_timeout: int,
    warmup_seconds: float,
    startup_timeout: float,
    skip_cleanup: bool,
    workload: str,
) -> dict:
    if not skip_cleanup:
        cleanup_gateway_port(gateway_port)
    log(f"running strategy benchmark for {strategy}")
    process = start_gateway(repo_root, strategy, request_timeout)
    try:
        wait_for_gateway(gateway_url, process=process, timeout=startup_timeout)
        log(f"gateway ready for strategy={strategy}")
        wait_for_strategy(gateway_url, expected_strategy=strategy, timeout=5.0)
        if warmup_seconds > 0:
            log(f"warming up for {warmup_seconds:.1f}s")
            time.sleep(warmup_seconds)

        log("capturing pre-run stats")
        before_stats = fetch_json(f"{gateway_url}/stats")
        log(
            f"starting load test: requests={requests} concurrency={concurrency} "
            f"prompt_size={prompt_size} max_tokens={max_tokens} workload={workload}"
        )
        summary = run_load_test(
            url=f"{gateway_url}/infer",
            requests=requests,
            concurrency=concurrency,
            prompt_size=prompt_size,
            max_tokens=max_tokens,
            timeout=timeout,
            workload=workload,
        )
        log("capturing post-run stats")
        after_stats = fetch_json(f"{gateway_url}/stats")
    finally:
        gateway_log = stop_gateway(process)

    log(f"completed strategy benchmark for {strategy}")
    return {
        "strategy": strategy,
        "load_summary": summary,
        "stats_before": before_stats,
        "stats_after": after_stats,
        "gateway_log": gateway_log,
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"writing benchmark report to {path}")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def write_markdown_summary(path: Path, report: dict) -> None:
    log(f"writing benchmark markdown summary to {path}")
    lines: list[str] = []
    lines.append("# Strategy Benchmark Summary")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{report['generated_at_utc']}`")
    lines.append(f"- Gateway URL: `{report['gateway_url']}`")
    lines.append(f"- Total requests: `{report['requests']}`")
    lines.append(f"- Concurrency: `{report['concurrency']}`")
    lines.append(f"- Prompt size: `{report['prompt_size']}`")
    lines.append(f"- Max tokens: `{report['max_tokens']}`")
    lines.append(f"- Workload: `{report['workload']}`")
    lines.append(f"- Client timeout seconds: `{report['timeout_seconds']}`")
    lines.append(f"- Gateway request timeout seconds: `{report['gateway_request_timeout_seconds']}`")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Strategy | Req/sec | P50 ms | P95 ms | P99 ms | 200s | 429s | 503s | Other | Completed | Failed | Available Workers | Saturated Workers |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")

    for result in report["results"]:
        summary = result["load_summary"]
        stats_after = result["stats_after"]
        router = stats_after["router"]
        status_counts = summary["status_counts"]
        ok = status_counts.get("200", status_counts.get(200, 0))
        too_many = status_counts.get("429", status_counts.get(429, 0))
        unavailable = status_counts.get("503", status_counts.get(503, 0))
        accounted = ok + too_many + unavailable
        other = sum(int(v) for k, v in status_counts.items()) - accounted
        lines.append(
            f"| `{result['strategy']}` | "
            f"{summary['requests_per_second']:.2f} | "
            f"{summary['p50_latency_ms']:.2f} | "
            f"{summary['p95_latency_ms']:.2f} | "
            f"{summary['p99_latency_ms']:.2f} | "
            f"{ok} | {too_many} | {unavailable} | {other} | "
            f"{stats_after['completed_requests']} | {stats_after['failed_requests']} | "
            f"{router['available_workers']} | {router['saturated_workers']} |"
        )

    lines.append("")
    lines.append("## Failure Reasons")
    lines.append("")
    for result in report["results"]:
        lines.append(f"### `{result['strategy']}`")
        lines.append("")
        failure_reasons = result["load_summary"].get("failure_reasons", {})
        for reason, count in sorted(failure_reasons.items()):
            lines.append(f"- `{reason}`: `{count}`")
        lines.append("")

    lines.append("")
    lines.append("## Interpretation Guide")
    lines.append("")
    lines.append("- Higher `Req/sec` is generally better if error rates stay controlled.")
    lines.append("- Lower `P95` and `P99` latency indicate better tail behavior under burst load.")
    lines.append("- `200s` are successful requests returned by the gateway.")
    lines.append("- `429s` would indicate explicit load shedding or worker saturation surfaced to the client.")
    lines.append("- `503s` indicate the gateway could not complete the request successfully.")
    lines.append("- `timeout` failures usually mean the client-side request deadline expired before the gateway finished draining the work.")
    lines.append("- connection-related failures suggest transport or process lifecycle issues rather than pure scheduling behavior.")
    lines.append("- `Completed` and `Failed` come from the gateway's internal counters after the run.")
    lines.append("- `Available Workers` and `Saturated Workers` show the cluster state at the stats snapshot taken after the load test.")
    lines.append("")
    lines.append("## Current vs Expected")
    lines.append("")
    lines.append("- Current behavior should show stable gateway operation, visible saturation handling, and enough metrics to explain why requests succeeded or failed.")
    lines.append("- Expected future behavior is stronger separation between `round_robin` and `cost`, especially in tail latency and success rate under mixed workloads.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def print_comparison(report: dict) -> None:
    print(f"Benchmark report written to {report['output_path']}")
    print()
    for result in report["results"]:
        print(f"Strategy: {result['strategy']}")
        print_summary(result["load_summary"])
        router_stats = result["stats_after"]["router"]
        print(
            "Router state after run: "
            f"healthy={router_stats['healthy_workers']} "
            f"available={router_stats['available_workers']} "
            f"saturated={router_stats['saturated_workers']}"
        )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run both gateway routing strategies and capture a comparison report.")
    parser.add_argument("--gateway-url", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--prompt-size", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workload", choices=("uniform", "mixed"), default="mixed")
    parser.add_argument("--request-timeout-seconds", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=float, default=0.5)
    parser.add_argument("--startup-timeout", type=float, default=20.0)
    parser.add_argument("--skip-cleanup", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    gateway_port = extract_port(args.gateway_url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = Path(args.output) if args.output else repo_root / "benchmarks" / f"strategy-comparison-{timestamp}.json"
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    markdown_path = output_path.with_suffix(".md")

    report = {
        "generated_at_utc": timestamp,
        "output_path": str(output_path),
        "gateway_url": args.gateway_url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "prompt_size": args.prompt_size,
        "max_tokens": args.max_tokens,
        "workload": args.workload,
        "timeout_seconds": args.timeout,
        "gateway_request_timeout_seconds": args.request_timeout_seconds,
        "results": [],
    }

    for strategy in ("round_robin", "cost"):
        result = run_strategy(
            repo_root=repo_root,
            gateway_url=args.gateway_url,
            gateway_port=gateway_port,
            strategy=strategy,
            requests=args.requests,
            concurrency=args.concurrency,
            prompt_size=args.prompt_size,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            request_timeout=args.request_timeout_seconds,
            warmup_seconds=args.warmup_seconds,
            startup_timeout=args.startup_timeout,
            skip_cleanup=args.skip_cleanup,
            workload=args.workload,
        )
        report["results"].append(result)

    write_report(output_path, report)
    write_markdown_summary(markdown_path, report)
    print_comparison(report)

    cleanup_gateway_port(gateway_port)


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
