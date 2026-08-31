#!/usr/bin/env python3
"""Per-request login latency against a live container, at increasing live-entry counts.

The card exists because the old eviction sweep was O(n) per login request:
0.139 ms at 1k live entries, 7.307 ms at 50k, so end-to-end login latency ran
from 0.43 ms/req at rest to 8.7 ms/req at 60k. That is a quadratic
interaction -- an attacker who inflates the live set inflates the per-request
cost for everyone.

Usage: preload-latency.py <base-url> <trusted-proxy-source-note>

Preloads N distinct X-Forwarded-For values (each creating one live throttle
entry), then measures per-request login latency at that size. Every request is
an unauthenticated failed login, exactly the shape an attacker drives.
"""
import http.client
import json
import sys
import time
from urllib.parse import urlparse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
USERNAME = "ci"
WRONG = "definitely-not-the-password-padding-value"

parsed = urlparse(BASE)


def conn():
    return http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)


def failed_login(c, xff):
    body = json.dumps({"username": USERNAME, "password": WRONG})
    c.request(
        "POST",
        "/api/auth/login",
        body=body,
        headers={"Content-Type": "application/json", "X-Forwarded-For": xff},
    )
    response = c.getresponse()
    response.read()
    return response.status


def xff(index):
    return f"10.{(index // 65536) % 256}.{(index // 256) % 256}.{index % 256}"


def preload(c, start, count):
    for index in range(start, start + count):
        failed_login(c, xff(index))


def measure(c, samples=400, offset=900_000):
    statuses = {}
    started = time.perf_counter()
    for index in range(samples):
        status = failed_login(c, xff(offset + index))
        statuses[status] = statuses.get(status, 0) + 1
    elapsed = time.perf_counter() - started
    return elapsed / samples * 1000, statuses


print(f"target: {BASE}")
print(f"{'live entries':>14} {'ms/req':>10}   statuses")

c = conn()
loaded = 0
for target in (0, 1_000, 10_000, 50_000):
    if target > loaded:
        preload(c, loaded, target - loaded)
        loaded = target
    ms, statuses = measure(c)
    print(f"{target:>14,} {ms:>10.3f}   {statuses}")
c.close()
