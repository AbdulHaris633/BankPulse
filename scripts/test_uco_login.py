"""
UCO Bank Login — Single Automated Flow
=======================================
1.  Fetch proxy from Proxy302 via ProxyManager
2.  Push proxy onto AdsPower profile
3.  Start browser
4.  Verify outbound IP
5.  Navigate directly to UCO Retail login page
6.  Enter User ID
7.  Solve captcha via 2Captcha
8.  Submit stage 1
9.  Wait for password field
10. Type password on virtual keyboard (keyset-aware, Shift-aware)
11. Submit stage 2
12. Inspect post-login page

Run:
    python scripts/test_uco_login.py --username <id> --password <pw>

Optional:
    --profile <id>   AdsPower profile ID (default: first available)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import base64
import tempfile
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app.core.settings import Settings
from app.core.proxy import ProxyManager
from app.core.twocaptcha import TwoCaptchaClient

_settings = Settings()

RETAIL_LOGIN_URL = (
    "https://ebanking.uco.bank.in/corp/AuthenticationController"
    "?FORMSGROUP_ID__=AuthenticationFG"
    "&__START_TRAN_FLAG__=Y"
    "&FG_BUTTONS__=LOAD"
    "&ACTION.LOAD=Y"
    "&AuthenticationFG.LOGIN_FLAG=1"
    "&BANK_ID=028"
    "&LANG_ID=001"
    "&UserType=R"
)

# Virtual keyboard keyset maps (kept for reference — not used in current flow)
# KB_NORMAL_CHARS = set('abcdefghijklmnopqrstuvwxyz@._-')
# KB_META1_CHARS  = set('0123456789~`/!$=}\'\\^?#+%*{|&')

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET}  {msg}"); sys.exit(1)
def info(msg): print(f"  {CYAN}·{RESET}  {msg}")
def step(n, msg): print(f"\n{BOLD}{CYAN}[{n}]{RESET} {BOLD}{msg}{RESET}")

# AdsPower helpers

def _ap_get(path, params=None):
    headers = {"Authorization": f"Bearer {_settings.ADSPOWER_API_KEY}"}
    r = requests.get(
        f"http://local.adspower.net:{_settings.ADSPOWER_PORT}{path}",
        headers=headers, params=params or {}, timeout=30
    )
    r.raise_for_status()
    return r.json()


def _ap_post(path, payload):
    headers = {"Authorization": f"Bearer {_settings.ADSPOWER_API_KEY}"}
    r = requests.post(
        f"http://local.adspower.net:{_settings.ADSPOWER_PORT}{path}",
        headers=headers, json=payload, timeout=30
    )
    r.raise_for_status()
    return r.json()


def get_first_profile_id():
    profiles = _ap_get("/api/v1/user/list").get("data", {}).get("list", [])
    if not profiles:
        fail("No AdsPower profiles found — create one first")
    return profiles[0]["user_id"]


def push_proxy(profile_id, proxy):
    """Strip internal ProxyManager fields and push proxy to AdsPower profile."""
    clean = {k: v for k, v in proxy.items() if not k.startswith("_")}
    _ap_post("/api/v1/user/update", {"user_id": profile_id, "user_proxy_config": clean})


def start_browser(profile_id):
    data = _ap_get("/api/v1/browser/start", {"user_id": profile_id, "open_tabs": 1}).get("data", {})
    driver_url   = data.get("ws", {}).get("selenium")
    chrome_driver = data.get("webdriver")
    if not driver_url or not chrome_driver:
        fail(f"Browser start failed: {data}")
    options = Options()
    options.add_experimental_option("debuggerAddress", driver_url.replace("ws://", ""))
    service = webdriver.ChromeService(executable_path=chrome_driver)
    return webdriver.Chrome(service=service, options=options)


def stop_browser(profile_id):
    try:
        _ap_get("/api/v1/browser/stop", {"user_id": profile_id})
    except Exception:
        pass

# Selenium helpers

def wait_for(driver, css, timeout=20):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css))
        )
    except Exception:
        return None


def find(driver, css):
    els = driver.find_elements(By.CSS_SELECTOR, css)
    return els[0] if els else None

# Captcha solver

def js_click(driver, el):
    """Click via JavaScript — bypasses any overlay element intercepting the click."""
    driver.execute_script("arguments[0].click();", el)


def solve_captcha(driver):
    img = find(driver, 'img#IMAGECAPTCHA')
    if not img:
        fail("Captcha image not found (img#IMAGECAPTCHA)")

    # Get the image src (relative URL) and build the full URL
    src = img.get_attribute("src")
    info(f"Captcha img src: {src}")

    # Build absolute URL from the current page origin
    from urllib.parse import urljoin
    current_url = driver.current_url
    captcha_url = urljoin(current_url, src)
    info(f"Captcha full URL: {captcha_url}")

    # Fetch the image using the browser's cookies so the session is authenticated
    session_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    headers = {
        "Referer": current_url,
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
    }
    resp = requests.get(captcha_url, cookies=session_cookies, headers=headers, timeout=15)
    if not resp.ok:
        fail(f"Failed to download captcha image: HTTP {resp.status_code}")
    captcha_bytes = resp.content
    info(f"Captcha image downloaded: {len(captcha_bytes)} bytes, content-type: {resp.headers.get('content-type')}")

    # Save to a fixed path so you can inspect the exact image sent to 2Captcha
    captcha_save_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "uco_captcha_debug.png"
    )
    with open(captcha_save_path, "wb") as f:
        f.write(captcha_bytes)
    info(f"Captcha image saved for inspection → open: {captcha_save_path}")

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(captcha_bytes)
    tmp.close()

    try:
        client = TwoCaptchaClient(api_key=_settings.TWOCAPTCHA_API_KEY)
        result = client.solve_image(tmp.name, case_sensitive=True)
        print(f"\n  {CYAN}2Captcha raw response:{RESET}")
        print(f"    {result}")
        text = result.get("solution", {}).get("text", "").strip()
        info(f"Extracted captcha text: {repr(text)}")
        if not text:
            fail("2Captcha returned empty solution")
        return text
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# Main flow

def run(profile_id, username, password):
    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  UCO Bank — Automated Login Test{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"  Profile  : {profile_id}")
    print(f"  Username : {username}")
    print(f"  Password : {'*' * len(password)}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")

    # ── Step 1: Fetch proxy from Proxy302 ─────────────────────────────────────
    step(1, "Fetch proxy from Proxy302")
    proxy = ProxyManager().get_proxy()
    if not proxy:
        fail("ProxyManager returned no proxy — check .env credentials and Proxy302 balance")
    ok(f"Got proxy: {proxy['proxy_type']}://{proxy['proxy_host']}:{proxy['proxy_port']}")

    # ── Step 2: Push proxy to AdsPower profile ────────────────────────────────
    step(2, "Push proxy onto AdsPower profile")
    push_proxy(profile_id, proxy)
    ok("Proxy updated on profile")

    # ── Step 3: Start browser ─────────────────────────────────────────────────
    step(3, "Start AdsPower browser")
    driver = start_browser(profile_id)
    driver.maximize_window()
    ok("Browser started (maximized)")

    try:
        # ── Step 4: Verify outbound IP ────────────────────────────────────────
        step(4, "Verify outbound IP through proxy")
        driver.get("https://api.ipify.org?format=json")
        time.sleep(2)
        body = driver.find_element(By.TAG_NAME, "body").text
        ok(f"Outbound IP: {body}")

        # ── Step 5: Navigate to UCO Retail login ──────────────────────────────
        step(5, "Navigate to UCO Retail login page")
        driver.get(RETAIL_LOGIN_URL)
        if not wait_for(driver, 'input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=30):
            fail("Login page did not load — USER_PRINCIPAL input not found")
        ok(f"Login page loaded — {driver.current_url}")

        # ── Step 6: Enter User ID ─────────────────────────────────────────────
        step(6, "Enter User ID")
        user_field = find(driver, 'input[name="AuthenticationFG.USER_PRINCIPAL"]')
        user_field.send_keys(username)
        time.sleep(1)
        ok(f"User ID entered: {username}")

        # ── Step 7: Solve captcha via 2Captcha ────────────────────────────────
        step(7, "Solve captcha via 2Captcha")
        info("Sending captcha image to 2Captcha...")
        captcha_text = solve_captcha(driver)
        ok(f"Captcha solved: {captcha_text}")

        captcha_input = find(driver, 'input[name="AuthenticationFG.VERIFICATION_CODE"]')
        if not captcha_input:
            fail("Captcha input not found (AuthenticationFG.VERIFICATION_CODE)")
        captcha_input.clear()
        captcha_input.send_keys(captcha_text)
        ok("Captcha text entered")
        time.sleep(0.5)

        # ── Step 8: Submit stage 1 ────────────────────────────────────────────
        step(8, "Submit stage 1 (User ID + Captcha)")
        submit1 = find(driver, 'input#STU_VALIDATE_CREDENTIALS')
        if not submit1:
            fail("Login button not found (input#STU_VALIDATE_CREDENTIALS)")
        js_click(driver, submit1)
        ok("Stage 1 submitted")
        time.sleep(4)
        info(f"URL after stage 1: {driver.current_url}")

        # ── Step 9: Wait for password field ───────────────────────────────────
        step(9, "Wait for password field (stage 2)")
        pw_field = wait_for(driver, 'input[name="AuthenticationFG.ACCESS_CODE"]', timeout=25)
        if not pw_field:
            fail(
                "Password field did not appear — stage 1 likely rejected\n"
                f"  Page title: {driver.title}\n"
                f"  URL: {driver.current_url}"
            )
        ok("Password field appeared")

        # ── Step 10: Enter password one character at a time ──────────────────
        step(10, "Enter password character by character")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pw_field)
        time.sleep(0.5)
        js_click(driver, pw_field)
        time.sleep(1)
        for char in password:
            pw_field.send_keys(char)
            time.sleep(0.15)
        ok(f"Password entered character by character ({len(password)} chars)")
        time.sleep(1)

        # ── Step 11: Submit stage 2 ───────────────────────────────────────────
        step(11, "Submit stage 2 — click Login button")
        submit2 = find(driver, 'input#VALIDATE_STU_CREDENTIALS')
        if not submit2:
            fail("Stage 2 submit not found (input#VALIDATE_STU_CREDENTIALS)")
        js_click(driver, submit2)
        ok("Login button clicked")
        time.sleep(10)

        # ── Step 12: Open menu ────────────────────────────────────────────────
        step(12, "Open menu (toggle-left)")
        menu_toggle = wait_for(driver, 'a#toggle-left', timeout=15)
        if not menu_toggle:
            fail("Menu toggle not found (a#toggle-left)")
        js_click(driver, menu_toggle)
        ok("Menu opened")
        time.sleep(1.5)

        # ── Step 13: Click Dashboard ──────────────────────────────────────────
        step(13, "Click Dashboard")
        dashboard = wait_for(driver, 'a#Dashboard', timeout=15)
        if not dashboard:
            fail("Dashboard link not found (a#Dashboard)")
        js_click(driver, dashboard)
        ok("Dashboard clicked")
        time.sleep(3)
        info(f"Page title : {driver.title}")
        info(f"Current URL: {driver.current_url}")

        # ── Step 14: Scrape account number ────────────────────────────────────
        step(14, "Scrape account number")
        acct_el = wait_for(driver, 'a[name="HREF_OperativeAccountsWidgetFG.OPR_ACCOUNT_NUMBER_ARRAY[0]"]', timeout=15)
        if not acct_el:
            fail("Account number element not found")
        account_number = acct_el.text.strip()
        ok(f"Account number: {account_number}")
        # ── Step 15: Logout ───────────────────────────────────────────────────
        step(15, "Waiting 20s then logging out")
        time.sleep(20)
        logout_btn = wait_for(driver, 'a#HREF_Logout', timeout=10)
        if not logout_btn:
            fail("Logout button not found (a#HREF_Logout)")
        js_click(driver, logout_btn)
        ok("Logout clicked")
        time.sleep(3)
        info(f"Page after logout: {driver.title} — {driver.current_url}")
        ok("Flow complete")

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        print(f"\n  {RED}ERROR{RESET}  Unhandled exception: {e}")
        traceback.print_exc()
    finally:
        step("–", "Stopping browser")
        stop_browser(profile_id)
        ok("Browser stopped")

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Done{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UCO Bank automated login test.")
    parser.add_argument("--profile", default=None, help="AdsPower profile ID (default: first)")
    args = parser.parse_args()

    profile = args.profile or get_first_profile_id()
    run(profile_id=profile, username="227678906", password="Tinza@009988t")
