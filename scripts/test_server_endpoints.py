"""
Server Endpoint Connectivity Tester
====================================
Tests every URL defined in Settings to confirm the command server is reachable
and each PHP endpoint responds (even a 400/500 means the server is up and routing
correctly — only a connection error or timeout means something is wrong).

Run:
    python scripts/test_server_endpoints.py

Optional flags:
    --timeout 10        HTTP timeout in seconds (default: 10)
    --payout            Test payout-mode URLs (PAYOUT_SERVER=True)
    --test-server       Test test-server URLs (TEST_SERVER=True)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import requests
from dataclasses import dataclass
from typing import Optional

from app.core.settings import Settings


# ── Colours for terminal output ───────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


@dataclass
class EndpointResult:
    name: str
    url: str
    method: str
    status: Optional[int]
    reachable: bool
    response_preview: str
    error: str = ""


def _preview(text: str, limit: int = 120) -> str:
    """Truncate long response bodies for display."""
    text = text.strip()
    return text[:limit] + "…" if len(text) > limit else text


def test_get(name: str, url: str, timeout: int) -> EndpointResult:
    """Send a GET request and capture the result."""
    try:
        r = requests.get(url, timeout=timeout)
        return EndpointResult(
            name=name,
            url=url,
            method="GET",
            status=r.status_code,
            reachable=True,
            response_preview=_preview(r.text),
        )
    except requests.exceptions.ConnectionError as e:
        return EndpointResult(name=name, url=url, method="GET", status=None,
                              reachable=False, response_preview="", error=f"ConnectionError: {e}")
    except requests.exceptions.Timeout:
        return EndpointResult(name=name, url=url, method="GET", status=None,
                              reachable=False, response_preview="", error="Timeout")
    except Exception as e:
        return EndpointResult(name=name, url=url, method="GET", status=None,
                              reachable=False, response_preview="", error=str(e))


def test_post(name: str, url: str, timeout: int, payload: dict) -> EndpointResult:
    """Send a POST request with a minimal dummy payload and capture the result."""
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        return EndpointResult(
            name=name,
            url=url,
            method="POST",
            status=r.status_code,
            reachable=True,
            response_preview=_preview(r.text),
        )
    except requests.exceptions.ConnectionError as e:
        return EndpointResult(name=name, url=url, method="POST", status=None,
                              reachable=False, response_preview="", error=f"ConnectionError: {e}")
    except requests.exceptions.Timeout:
        return EndpointResult(name=name, url=url, method="POST", status=None,
                              reachable=False, response_preview="", error="Timeout")
    except Exception as e:
        return EndpointResult(name=name, url=url, method="POST", status=None,
                              reachable=False, response_preview="", error=str(e))


def _status_colour(result: EndpointResult) -> str:
    if not result.reachable:
        return RED
    if result.status and result.status < 400:
        return GREEN
    if result.status and result.status < 500:
        return YELLOW   # 4xx — server up, rejected our dummy payload (expected)
    return RED          # 5xx or None


def print_result(result: EndpointResult) -> None:
    colour = _status_colour(result)
    status_str = str(result.status) if result.status else "N/A"
    reachable_str = "OK " if result.reachable else "FAIL"
    print(f"  {colour}{reachable_str}{RESET}  [{result.method}]  {BOLD}{result.name}{RESET}")
    print(f"         URL    : {result.url}")
    print(f"         Status : {colour}{status_str}{RESET}")
    if result.error:
        print(f"         Error  : {RED}{result.error}{RESET}")
    if result.response_preview:
        print(f"         Body   : {result.response_preview}")
    print()


def run(timeout: int, payout_mode: bool, test_server_mode: bool) -> None:
    # Patch env so Settings reflects the mode we are testing
    os.environ["PAYOUT_SERVER"]  = "true"  if payout_mode      else "false"
    os.environ["TEST_SERVER"]    = "true"  if test_server_mode  else "false"

    s = Settings()

    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Command Server Endpoint Test{RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"  Base URL     : {s.COMMAND_BASE_URL}")
    print(f"  Payout mode  : {payout_mode}")
    print(f"  Test server  : {test_server_mode}")
    print(f"  Timeout      : {timeout}s")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}\n")

    results = []

    # ── 1. Base URL reachability ───────────────────────────────────────────────
    print(f"{BOLD}[ Base URL ]{RESET}")
    r = test_get("Base URL", s.COMMAND_BASE_URL, timeout)
    print_result(r)
    results.append(r)

    # ── 2. GET endpoints ──────────────────────────────────────────────────────
    print(f"{BOLD}[ GET Endpoints ]{RESET}")

    get_endpoints = [
        ("PARENT_INSTRUCTIONS_URL", s.PARENT_INSTRUCTIONS_URL),
        ("SYNC_MODE_URL",            s.SYNC_MODE_URL),
        ("SYNC_BALANCE_URL",         s.SYNC_BALANCE_URL),
        ("GET_OTP_URL",              s.GET_OTP_URL),
        # CONFIRM_INSTRUCTION_URL has a {} placeholder — fill it with a dummy ID
        ("CONFIRM_INSTRUCTION_URL",  s.CONFIRM_INSTRUCTION_URL.format("TEST-001")),
    ]

    for name, url in get_endpoints:
        r = test_get(name, url, timeout)
        print_result(r)
        results.append(r)

    # ── 3. POST endpoints ─────────────────────────────────────────────────────
    print(f"{BOLD}[ POST Endpoints ]{RESET}")

    # Minimal dummy payloads — just enough to show the server routes the request
    post_endpoints = [
        ("PARENT_REPORT_URL",    s.PARENT_REPORT_URL,    {"bot_id": "TEST", "status": "test"}),
        # ("CHILD_REPORT_URL",     s.CHILD_REPORT_URL,     {"bot_id": "TEST", "status": "test"}),  # not in use
        ("SYNC_REPORT_URL",      s.SYNC_REPORT_URL,      {"bot_id": "TEST", "status": "test"}),
        ("UPDATE_PROFILE_URL",   s.UPDATE_PROFILE_URL,   {"trader_id": "TEST"}),
    ]

    for name, url, payload in post_endpoints:
        r = test_post(name, url, timeout, payload)
        print_result(r)
        results.append(r)

    # ── 4. Screenshot server (server2 — separate host) ────────────────────────
    print(f"{BOLD}[ Screenshot Server (server2) ]{RESET}")
    r = test_get("UPLOAD_SCREENSHOT_URL (host check)", s.UPLOAD_SCREENSHOT_URL, timeout)
    print_result(r)
    results.append(r)

    # ── Summary ───────────────────────────────────────────────────────────────
    reachable = [r for r in results if r.reachable]
    failed    = [r for r in results if not r.reachable]

    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Summary: {GREEN}{len(reachable)} reachable{RESET}  /  {RED}{len(failed)} unreachable{RESET}  (out of {len(results)} endpoints)")
    if failed:
        print(f"\n  {RED}Unreachable endpoints:{RESET}")
        for r in failed:
            print(f"    - {r.name}: {r.error}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}\n")

    # Exit 1 if any endpoint is completely unreachable (connection failure)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test all command server endpoints.")
    parser.add_argument("--timeout",     type=int, default=10, help="HTTP timeout in seconds (default: 10)")
    parser.add_argument("--payout",      action="store_true",  help="Test payout-mode URLs")
    parser.add_argument("--test-server", action="store_true",  help="Test test-server URLs")
    args = parser.parse_args()

    run(
        timeout=args.timeout,
        payout_mode=args.payout,
        test_server_mode=args.test_server,
    )
