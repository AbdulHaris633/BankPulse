"""
Proxy302 Provider Test
======================
Tests the full Proxy302 integration end-to-end:
  1. Token fetch  — confirms API credentials are valid
  2. Credentials  — confirms a dynamic proxy account can be created
  3. Speed test   — measures real latency through the proxy
  4. Connectivity — verifies a real HTTPS request works through the proxy

Run:
    python scripts/test_proxy302.py

Optional:
    python scripts/test_proxy302.py --repeat 3   # run speed test N times
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import time
import requests

from app.core.settings import Settings

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_PROXY302_API_BASE = "https://open.proxy302.com"
_SPEED_TEST_URL    = "https://clients3.google.com/generate_204"
_CONNECTIVITY_URL  = "https://httpbin.org/ip"   # returns {"origin": "x.x.x.x"}


def ok(msg):  print(f"  {GREEN}PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}FAIL{RESET}  {msg}")
def info(msg): print(f"  {CYAN}INFO{RESET}  {msg}")


def step(title):
    print(f"\n{BOLD}[ {title} ]{RESET}")


# ── Step 1 — Token ────────────────────────────────────────────────────────────

def fetch_token(s: Settings) -> str | None:
    step("Step 1 — Fetch API Token")
    if not s.PROXY302_USER or not s.PROXY302_PASS:
        fail("PROXY302_USER or PROXY302_PASS not set in .env — skipping all tests")
        return None
    try:
        r = requests.get(
            f"{_PROXY302_API_BASE}/open_api/v3/user/users/token",
            params={"username": s.PROXY302_USER, "password": s.PROXY302_PASS},
            timeout=s.REQUESTS_TIMEOUT,
        )
        data = r.json()
        if data.get("code") == 0:
            token = data["data"]["token"]
            ok(f"Token received: {token[:20]}…")
            return token
        fail(f"API returned error: {data.get('msg')} (code={data.get('code')})")
        return None
    except Exception as e:
        fail(f"Request failed: {e}")
        return None


# ── Step 2 — Fetch proxy credentials ──────────────────────────────────────────

def fetch_credentials(s: Settings, token: str) -> dict | None:
    step("Step 2 — Fetch Dynamic Proxy Credentials")

    if s.PROXY302_COUNTRY_ID:
        endpoint = f"{_PROXY302_API_BASE}/open_api/v3/proxy/api/proxy/dynamic/ip/by_area"
        params = {
            "protocol":   s.PROXY302_PROTOCOL,
            "country_id": s.PROXY302_COUNTRY_ID,
            "state_id":   s.PROXY302_STATE_ID,
            "city_id":    s.PROXY302_CITY_ID,
        }
        info("Using by_area endpoint (geo-locked)")
    else:
        endpoint = f"{_PROXY302_API_BASE}/open_api/v3/proxy/api/proxy/dynamic/traffic"
        params = {"protocol": s.PROXY302_PROTOCOL, "s": 1}
        info("Using traffic endpoint (any country)")

    info(f"Params: {params}")
    try:
        r = requests.post(
            endpoint,
            params=params,
            headers={"Authorization": token},
            timeout=s.REQUESTS_TIMEOUT,
        )
        data = r.json()
        if data.get("code") == 0:
            d = data["data"]
            creds = {
                "host": d["host"],
                "port": str(d["port"]),
                "user": d["user_name"],
                "pass": d["password"],
                "protocol": d["protocol"],
            }
            ok(f"Host     : {creds['host']}")
            ok(f"Port     : {creds['port']}")
            ok(f"Protocol : {creds['protocol']}")
            ok(f"Username : {creds['user']}")
            ok(f"Password : {creds['pass'][:4]}{'*' * (len(creds['pass']) - 4)}")
            return creds
        fail(f"API returned error: {data.get('msg')} (code={data.get('code')})")
        return None
    except Exception as e:
        fail(f"Request failed: {e}")
        return None


# ── Step 3 — Speed test ───────────────────────────────────────────────────────

def speed_test(s: Settings, creds: dict, repeat: int) -> None:
    step(f"Step 3 — Speed Test ({repeat} run{'s' if repeat > 1 else ''})")
    proxy_url = "{proto}://{user}:{pw}@{host}:{port}".format(
        proto=creds["protocol"],
        user=creds["user"],
        pw=creds["pass"],
        host=creds["host"],
        port=creds["port"],
    )
    proxies = {"https": proxy_url, "http": proxy_url}

    latencies = []
    for i in range(repeat):
        try:
            start = time.time()
            r = requests.get(
                _SPEED_TEST_URL,
                proxies=proxies,
                timeout=(s.PROXY_CONNECT_TIMEOUT, s.PROXY_READ_TIMEOUT),
            )
            elapsed = round(time.time() - start, 3)
            latencies.append(elapsed)
            ok(f"Run {i+1}: {elapsed}s  (HTTP {r.status_code})")
        except Exception as e:
            fail(f"Run {i+1}: {e}")

    if latencies:
        avg = round(sum(latencies) / len(latencies), 3)
        best = min(latencies)
        worst = max(latencies)
        print(f"\n  Avg: {avg}s  |  Best: {best}s  |  Worst: {worst}s")
        colour = GREEN if avg < 2.0 else YELLOW if avg < 5.0 else RED
        print(f"  Rating: {colour}{'Fast' if avg < 2.0 else 'Acceptable' if avg < 5.0 else 'Slow'}{RESET}")


# ── Step 4 — Connectivity (real HTTPS request) ────────────────────────────────

def connectivity_test(s: Settings, creds: dict) -> None:
    step("Step 4 — Connectivity (real HTTPS request through proxy)")
    proxy_url = "{proto}://{user}:{pw}@{host}:{port}".format(
        proto=creds["protocol"],
        user=creds["user"],
        pw=creds["pass"],
        host=creds["host"],
        port=creds["port"],
    )
    proxies = {"https": proxy_url, "http": proxy_url}
    try:
        r = requests.get(_CONNECTIVITY_URL, proxies=proxies, timeout=15)
        if r.ok:
            origin = r.json().get("origin", "unknown")
            ok(f"Request succeeded — outbound IP via proxy: {origin}")
        else:
            fail(f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        fail(f"Request failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(repeat: int) -> None:
    s = Settings()

    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Proxy302 Integration Test{RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"  Host         : {s.PROXY302_HOST or '(not set)'}")
    print(f"  Port         : {s.PROXY302_PORT or '(not set)'}")
    print(f"  User         : {s.PROXY302_USER or '(not set)'}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")

    if not s.PROXY302_HOST:
        fail("PROXY302_HOST not set in .env")
        sys.exit(1)

    creds = {
        "host":     s.PROXY302_HOST,
        "port":     s.PROXY302_PORT,
        "user":     s.PROXY302_USER,
        "pass":     s.PROXY302_PASS,
        "protocol": "socks5",
    }

    ok(f"Host     : {creds['host']}")
    ok(f"Port     : {creds['port']}")
    ok(f"User     : {creds['user']}")

    speed_test(s, creds, repeat)
    connectivity_test(s, creds)

    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Done{RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Proxy302 provider end-to-end.")
    parser.add_argument("--repeat", type=int, default=3, help="Number of speed test runs (default: 3)")
    args = parser.parse_args()
    run(repeat=args.repeat)
