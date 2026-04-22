"""
Equitas Bank — Login Page Inspector
=====================================
1.  Fetch proxy + push to AdsPower profile
2.  Start browser (maximized)
3.  Check outbound IP and geolocation
4.  Navigate to Equitas internet banking page (via driver.get)
5.  Find and click Login button (click-through, not direct URL)
6.  Switch to the new tab that opens
7.  Print browser console errors
8.  Wait 2 minutes so you can press F12 and inspect

Run:
    python scripts/test_equitas_inspect.py

Optional:
    --profile <id>   AdsPower profile ID (default: first available)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import time
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException

from app.core.adspower import AdsPowerAPI
from app.core.browser import Browser
from app.core.proxy import ProxyManager

HOME_URL     = "https://equitas.bank.in/personal-banking/ways-to-bank/internet-banking/"
INSPECT_WAIT = 120

GREEN = "\033[92m"
RED   = "\033[91m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def ok(msg):      print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg):    print(f"  {RED}✗{RESET}  {msg}"); sys.exit(1)
def info(msg):    print(f"  {CYAN}·{RESET}  {msg}")
def step(n, msg): print(f"\n{BOLD}{CYAN}[{n}]{RESET} {BOLD}{msg}{RESET}")


def get_first_profile_id():
    api = AdsPowerAPI()
    profiles = api._get("/api/v1/user/list").get("list", [])
    if not profiles:
        fail("No AdsPower profiles found")
    return profiles[0]["user_id"]


def safe_get(driver, url, timeout=15):
    """Navigate using driver.get() with a short page load timeout.
    Catches timeout and connection errors — browser keeps loading on its own."""
    driver.set_page_load_timeout(timeout)
    try:
        driver.get(url)
    except Exception:
        pass  # catches TimeoutException, ERR_SOCKS_CONNECTION_FAILED, etc.
    finally:
        driver.set_page_load_timeout(30)


def js_click(driver, el):
    driver.execute_script("arguments[0].click();", el)


def find_login_button(driver):
    # Strategy 1: exact confirmed img filename inside button
    for img in driver.find_elements(By.CSS_SELECTOR, 'img[src*="Polygon_5_86ad9c2638"]'):
        try:
            btn = driver.execute_script("return arguments[0].closest('button');", img)
            if btn:
                info("Found Login button via Polygon_5_86ad9c2638 image")
                return btn
        except Exception:
            pass

    # Strategy 2: any Polygon_5 image inside a button (filename variant)
    for img in driver.find_elements(By.CSS_SELECTOR, 'img[src*="Polygon_5"]'):
        try:
            btn = driver.execute_script("return arguments[0].closest('button');", img)
            if btn:
                info("Found Login button via Polygon_5 image")
                return btn
        except Exception:
            pass

    # Strategy 3: Equitas 2.0 direct login link
    for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="ib.equip.equitasbank.com/login"]'):
        info("Found Equitas 2.0 login link")
        return a

    # Strategy 4: button containing <p>Login</p>
    for btn in driver.find_elements(By.XPATH, '//button[.//p[text()="Login"]]'):
        try:
            if btn.is_displayed():
                info("Found Login button via <p>Login</p> child")
                return btn
        except Exception:
            pass

    # Strategy 5: any visible button with text "Login"
    for btn in driver.find_elements(By.XPATH, '//button[contains(., "Login")]'):
        try:
            if btn.is_displayed():
                info(f"Found button by text: '{btn.text.strip()}'")
                return btn
        except Exception:
            pass

    return None


def print_console_logs(driver):
    try:
        logs = driver.get_log('browser')
        if logs:
            info(f"Browser console logs ({len(logs)}):")
            for log in logs:
                print(f"    [{log.get('level','?')}] {log.get('message','')[:120]}")
        else:
            info("No browser console errors")
    except Exception as e:
        info(f"Could not read console logs: {e}")


def run(profile_id):
    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Equitas Bank — Login Page Inspector{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"  Profile : {profile_id}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")

    # ── Step 1: Proxy ─────────────────────────────────────────────────────────
    step(1, "Fetch proxy from Proxy302")
    proxy = ProxyManager().get_proxy()
    if not proxy:
        fail("ProxyManager returned no proxy")
    AdsPowerAPI().update_proxy(profile_id, proxy)
    ok(f"Proxy: {proxy['proxy_type']}://{proxy['proxy_host']}:{proxy['proxy_port']}")

    # ── Step 2: Browser ───────────────────────────────────────────────────────
    step(2, "Start AdsPower browser")
    browser_obj = Browser(AdsPowerAPI())
    browser_obj.open(profile_id)
    driver = browser_obj.driver
    time.sleep(2)
    driver.maximize_window()
    ok("Browser started")

    try:
        # ── Step 3: Navigate to main Equitas page ────────────────────────────
        step(3, "Navigate to Equitas internet banking page")
        safe_get(driver, HOME_URL)
        time.sleep(15)  # let SPA fully settle
        try:
            info(f"URL: {driver.current_url}")
        except Exception:
            pass
        ok("Main page loaded")

        # ── Step 4: Find and click Login button ───────────────────────────────
        step(4, "Find and click Login button")
        login_btn = find_login_button(driver)
        if not login_btn:
            all_buttons = driver.find_elements(By.CSS_SELECTOR, 'button')
            info(f"Login button not found. Buttons on page ({len(all_buttons)}):")
            for b in all_buttons:
                try:
                    info(f"  '{b.text.strip()[:60]}'")
                except Exception:
                    pass
            fail("Login button not found — see list above")

        original_handles = set(driver.window_handles)
        js_click(driver, login_btn)
        ok("Login button clicked")
        time.sleep(4)

        # ── Step 6: Switch to new tab ─────────────────────────────────────────
        step(5, "Switch to new tab")
        new_handles = set(driver.window_handles) - original_handles
        if new_handles:
            driver.switch_to.window(new_handles.pop())
            time.sleep(3)
            try:
                ok(f"New tab URL: {driver.current_url}")
            except Exception:
                ok("Switched to new tab")
        else:
            info("No new tab — navigation may have happened in-place")
            try:
                info(f"Current URL: {driver.current_url}")
            except Exception:
                pass

        # ── Step 7: Console logs ──────────────────────────────────────────────
        step(6, "Browser console errors")
        print_console_logs(driver)

        # ── Step 8: Wait for inspection ───────────────────────────────────────
        step(7, f"Waiting {INSPECT_WAIT}s — press F12 in AdsPower to inspect")
        info("Look for: User ID input, password input, button IDs/names")
        for remaining in range(INSPECT_WAIT, 0, -10):
            print(f"  {CYAN}·{RESET}  {remaining}s remaining...", flush=True)
            time.sleep(10)
        ok("Done")

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        print(f"\n  {RED}ERROR{RESET}  {e}")
        traceback.print_exc()
    finally:
        step("–", "Stopping browser")
        try:
            browser_obj.close()
        except Exception:
            pass
        ok("Browser stopped")

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Done{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Equitas Bank login page inspector.")
    parser.add_argument("--profile", default=None, help="AdsPower profile ID (default: first)")
    args = parser.parse_args()

    profile = args.profile or get_first_profile_id()
    run(profile_id=profile)
