import argparse
import json
import random
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def send_request(
    url: str,
    prompt_size: int,
    max_tokens: int,
    timeout: float,
    api_key: str | None = None,
) -> tuple[int, float, str]:
    payload = json.dumps(
        {
            "prompt": "x" * prompt_size,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    request = urllib.request.Request(
        url=url,
        data=payload,
        headers=headers,
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            latency_ms = (time.perf_counter() - started) * 1000
            return response.status, latency_ms, "ok"
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return exc.code, latency_ms, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        reason = classify_url_error(exc.reason)
        return 0, latency_ms, reason
    except TimeoutError:
        latency_ms = (time.perf_counter() - started) * 1000
        return 0, latency_ms, "timeout"
    except socket.timeout:
        latency_ms = (time.perf_counter() - started) * 1000
        return 0, latency_ms, "timeout"
    except Exception:
        latency_ms = (time.perf_counter() - started) * 1000
        return 0, latency_ms, "unknown_error"


def classify_url_error(reason: object) -> str:
    if isinstance(reason, socket.timeout):
        return "timeout"

    reason_text = str(reason).lower()
    if "timed out" in reason_text:
        return "timeout"
    if "connection refused" in reason_text:
        return "connection_refused"
    if "connection reset" in reason_text:
        return "connection_reset"
    if "remote end closed connection" in reason_text:
        return "connection_closed"
    return "url_error"


def build_workload(
    requests: int,
    prompt_size: int,
    max_tokens: int,
    workload: str,
    seed: int = 42,
) -> list[tuple[int, int]]:
    if workload == "mixed":
        shapes = [
            (64, 64),
            (128, 128),
            (256, 256),
            (512, 384),
            (1024, 512),
        ]
        items = [shapes[i % len(shapes)] for i in range(requests)]
        random.Random(seed).shuffle(items)
        return items

    return [(prompt_size, max_tokens) for _ in range(requests)]


def run_load_test(
    url: str,
    requests: int,
    concurrency: int,
    prompt_size: int,
    max_tokens: int,
    timeout: float,
    workload: str = "uniform",
    seed: int = 42,
    api_key_count: int = 0,
) -> dict:
    workload_items = build_workload(requests, prompt_size, max_tokens, workload, seed)
    started = time.perf_counter()
    status_counts: dict[int, int] = {}
    failure_reasons: dict[str, int] = {}
    successful_latencies: list[float] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for request_index, (request_prompt_size, request_max_tokens) in enumerate(workload_items):
            api_key = (
                f"benchmark-{request_index % api_key_count:02d}"
                if api_key_count > 0
                else None
            )
            futures.append(
                executor.submit(
                    send_request,
                    url,
                    request_prompt_size,
                    request_max_tokens,
                    timeout,
                    api_key,
                )
            )

        for future in as_completed(futures):
            status, latency_ms, reason = future.result()
            status_counts[status] = status_counts.get(status, 0) + 1
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            if status == 200:
                successful_latencies.append(latency_ms)

    total_duration = time.perf_counter() - started
    successful_latencies.sort()

    def percentile(p: float) -> float:
        if not successful_latencies:
            return 0.0
        index = int((len(successful_latencies) - 1) * p)
        return successful_latencies[index]

    successful_requests = status_counts.get(200, 0)

    return {
        "total_requests": requests,
        "concurrency": concurrency,
        "workload": workload,
        "workload_seed": seed,
        "api_key_count": api_key_count,
        "elapsed_seconds": total_duration,
        "requests_per_second": requests / total_duration if total_duration > 0 else 0.0,
        "successful_requests_per_second": successful_requests / total_duration if total_duration > 0 else 0.0,
        "successful_latency_samples": len(successful_latencies),
        "status_counts": status_counts,
        "failure_reasons": failure_reasons,
        "p50_latency_ms": percentile(0.50),
        "p95_latency_ms": percentile(0.95),
        "p99_latency_ms": percentile(0.99),
    }


def print_summary(summary: dict) -> None:
    print("Load spike summary")
    print(f"Total requests: {summary['total_requests']}")
    print(f"Concurrency: {summary['concurrency']}")
    print(f"Workload: {summary['workload']}")
    print(f"Elapsed seconds: {summary['elapsed_seconds']:.2f}")
    print(f"Requests/sec: {summary['requests_per_second']:.2f}")
    print(f"Successful requests/sec: {summary['successful_requests_per_second']:.2f}")
    print(f"Status counts: {summary['status_counts']}")
    print(f"Failure reasons: {summary['failure_reasons']}")
    print(f"Successful-response P50 latency ms: {summary['p50_latency_ms']:.2f}")
    print(f"Successful-response P95 latency ms: {summary['p95_latency_ms']:.2f}")
    print(f"Successful-response P99 latency ms: {summary['p99_latency_ms']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a burst of synthetic inference traffic.")
    parser.add_argument("--url", default="http://localhost:8080/infer")
    parser.add_argument("--requests", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--prompt-size", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workload", choices=("uniform", "mixed"), default="uniform")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--api-key-count",
        type=int,
        default=0,
        help="Rotate this many synthetic X-API-Key values; 0 uses the anonymous bucket.",
    )
    args = parser.parse_args()

    summary = run_load_test(
        url=args.url,
        requests=args.requests,
        concurrency=args.concurrency,
        prompt_size=args.prompt_size,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        workload=args.workload,
        seed=args.seed,
        api_key_count=args.api_key_count,
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
