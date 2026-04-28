import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def send_request(url: str, prompt_size: int, max_tokens: int, timeout: float) -> tuple[int, float]:
    payload = json.dumps(
        {
            "prompt": "x" * prompt_size,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url=url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            latency_ms = (time.perf_counter() - started) * 1000
            return response.status, latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return exc.code, latency_ms
    except Exception:
        latency_ms = (time.perf_counter() - started) * 1000
        return 0, latency_ms


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a burst of synthetic inference traffic.")
    parser.add_argument("--url", default="http://localhost:8080/infer")
    parser.add_argument("--requests", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--prompt-size", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()

    started = time.perf_counter()
    status_counts: dict[int, int] = {}
    latencies: list[float] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                send_request,
                args.url,
                args.prompt_size,
                args.max_tokens,
                args.timeout,
            )
            for _ in range(args.requests)
        ]

        for future in as_completed(futures):
            status, latency_ms = future.result()
            status_counts[status] = status_counts.get(status, 0) + 1
            latencies.append(latency_ms)

    total_duration = time.perf_counter() - started
    latencies.sort()

    def percentile(p: float) -> float:
        if not latencies:
            return 0.0
        index = int((len(latencies) - 1) * p)
        return latencies[index]

    print("Load spike summary")
    print(f"Total requests: {args.requests}")
    print(f"Concurrency: {args.concurrency}")
    print(f"Elapsed seconds: {total_duration:.2f}")
    print(f"Requests/sec: {args.requests / total_duration:.2f}")
    print(f"Status counts: {status_counts}")
    print(f"P50 latency ms: {percentile(0.50):.2f}")
    print(f"P95 latency ms: {percentile(0.95):.2f}")
    print(f"P99 latency ms: {percentile(0.99):.2f}")


if __name__ == "__main__":
    main()
