"""
UCO Bank Login — Automated Flow
=================================
1.  Fetch proxy + push to AdsPower profile
2.  Start browser (maximized)
3.  Navigate to UCO retail login page
4.  Enter User ID char by char + random mouse moves
5.  Solve captcha via 2Captcha, enter char by char
6.  Submit stage 1
7.  Enter password char by char
8.  Submit stage 2
9.  Scrape account number
10. Logout

Run:
    python scripts/test_uco_login.py

Optional:
    --profile <id>   AdsPower profile ID (default: first available)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import random
import tempfile
import time
import requests
from urllib.parse import urljoin

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app.core.settings import Settings
from app.core.adspower import AdsPowerAPI
from app.core.browser import Browser
from app.core.proxy import ProxyManager
from app.core.twocaptcha import TwoCaptchaClient

_settings = Settings()

USERNAME = "227678906"
PASSWORD = "Tinza@009988t"

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

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

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


def wait_for_css(driver, selector, timeout=30, clickable=False):
    condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
    try:
        return WebDriverWait(driver, timeout).until(
            condition((By.CSS_SELECTOR, selector))
        )
    except Exception:
        return None


def random_mouse_move(driver, moves=4):
    """Dispatch random mousemove events across the visible viewport."""
    try:
        width  = driver.execute_script("return window.innerWidth")
        height = driver.execute_script("return window.innerHeight")
        for _ in range(moves):
            x = random.randint(80, max(81, width - 80))
            y = random.randint(80, max(81, height - 80))
            driver.execute_script(
                "document.dispatchEvent(new MouseEvent('mousemove',"
                "{bubbles:true,clientX:arguments[0],clientY:arguments[1]}))",
                x, y
            )
            time.sleep(random.uniform(0.08, 0.25))
    except Exception:
        pass


def human_click(driver, el):
    """Scroll into view, move mouse over element, then JS-click (reliable on form inputs)."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(random.uniform(0.1, 0.2))
    try:
        ActionChains(driver)\
            .move_to_element_with_offset(el, random.randint(-4, 4), random.randint(-2, 2))\
            .pause(random.uniform(0.1, 0.3))\
            .perform()
    except Exception:
        pass
    time.sleep(random.uniform(0.1, 0.2))
    driver.execute_script("arguments[0].click();", el)


def human_type(field, text):
    """Type text character by character with random human-like delays."""
    for char in text:
        field.send_keys(char)
        time.sleep(random.uniform(0.15, 0.4))


def solve_captcha(driver, proxy=None):
    """Download captcha via browser session, solve via 2Captcha, return text."""
    img = driver.find_elements(By.CSS_SELECTOR, 'img#IMAGECAPTCHA')
    if not img:
        fail("Captcha image not found (img#IMAGECAPTCHA)")
    src = img[0].get_attribute("src")
    captcha_url = urljoin(driver.current_url, src)
    session_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    headers = {
        "Referer": driver.current_url,
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
    }
    proxies = None
    if proxy:
        proxy_url = (
            f"{proxy['proxy_type']}://"
            f"{proxy['proxy_user']}:{proxy['proxy_password']}@"
            f"{proxy['proxy_host']}:{proxy['proxy_port']}"
        )
        proxies = {"http": proxy_url, "https": proxy_url}
    resp = requests.get(captcha_url, cookies=session_cookies, headers=headers, proxies=proxies, timeout=15)
    if not resp.ok:
        fail(f"Failed to download captcha: HTTP {resp.status_code}")

    # Save for inspection
    debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uco_captcha_debug.png")
    with open(debug_path, "wb") as f:
        f.write(resp.content)
    info(f"Captcha saved → {debug_path}")

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        tmp.write(resp.content)
        tmp.flush()
        tmp.close()
        client = TwoCaptchaClient(api_key=_settings.TWOCAPTCHA_API_KEY)
        result = client.solve_image(tmp.name, case_sensitive=True)
        text = result.get("solution", {}).get("text", "").strip()
        if not text:
            fail("2Captcha returned empty solution")
        return text
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def run(profile_id):
    username = USERNAME
    password = PASSWORD

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  UCO Bank — Automated Login Test{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"  Profile  : {profile_id}")
    print(f"  Username : {username}")
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

        # ── Step 3: Navigate to UCO login page ────────────────────────────────
        step(3, "Navigate to UCO retail login page")
        driver.get(RETAIL_LOGIN_URL)
        if not wait_for_css(driver, 'input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=30):
            fail("Login page did not load")
        random_mouse_move(driver, 3)
        ok(f"Login page loaded")

        # ── Step 4: Enter User ID ─────────────────────────────────────────────
        step(4, "Enter User ID")
        user_field = wait_for_css(driver, 'input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=10, clickable=True)
        if not user_field:
            fail("User ID field not found")
        random_mouse_move(driver, 2)
        human_click(driver, user_field)
        time.sleep(random.uniform(0.3, 0.7))
        human_type(user_field, username)
        time.sleep(1)
        ok(f"User ID entered: {username}")

        # ── Step 5: Solve captcha ─────────────────────────────────────────────
        step(5, "Solve captcha via 2Captcha")
        random_mouse_move(driver, 2)
        info("Sending captcha to 2Captcha...")
        captcha_text = solve_captcha(driver, proxy)
        ok(f"Captcha solved: {captcha_text}")

        cap_field = wait_for_css(driver, 'input[name="AuthenticationFG.VERIFICATION_CODE"]', timeout=10, clickable=True)
        if not cap_field:
            fail("Captcha input field not found")
        random_mouse_move(driver, 2)
        human_click(driver, cap_field)
        time.sleep(random.uniform(0.3, 0.6))
        human_type(cap_field, captcha_text)
        time.sleep(1)
        ok("Captcha text entered")

        # ── Step 6: Submit stage 1 ────────────────────────────────────────────
        step(6, "Submit stage 1 (User ID + Captcha)")
        random_mouse_move(driver, 3)
        submit1 = wait_for_css(driver, 'input#STU_VALIDATE_CREDENTIALS', timeout=10)
        if not submit1:
            fail("Stage 1 submit button not found")
        human_click(driver, submit1)
        time.sleep(4)
        ok("Stage 1 submitted")

        # ── Step 7: Wait for and enter password ───────────────────────────────
        step(7, "Enter password")
        pw_field = wait_for_css(driver, 'input[name="AuthenticationFG.ACCESS_CODE"]', timeout=25, clickable=True)
        if not pw_field:
            fail("Password field did not appear — stage 1 may have failed (wrong captcha/User ID)")
        random_mouse_move(driver, 2)
        human_click(driver, pw_field)
        time.sleep(random.uniform(0.5, 1))
        human_type(pw_field, password)
        time.sleep(1)
        ok(f"Password entered ({len(password)} chars)")

        # ── Step 8: Submit stage 2 ────────────────────────────────────────────
        step(8, "Submit stage 2 (Password)")
        random_mouse_move(driver, 3)
        submit2 = wait_for_css(driver, 'input#VALIDATE_STU_CREDENTIALS_UX', timeout=10)
        if not submit2:
            fail("Stage 2 submit button not found")
        human_click(driver, submit2)
        time.sleep(10)
        ok("Stage 2 submitted")

        # ── Step 9: Scrape account number ─────────────────────────────────────
        step(9, "Scrape account number")
        acct_el = wait_for_css(
            driver,
            'a[name="HREF_OperativeAccountsWidgetFG.OPR_ACCOUNT_NUMBER_ARRAY[0]"]',
            timeout=15
        )
        if not acct_el:
            fail("Account number element not found — login may have failed")
        account_number = acct_el.text.strip()
        ok(f"Account number: {account_number}")
        time.sleep(3)

        # ── Step 10: Logout ───────────────────────────────────────────────────
        step(10, "Logout")
        random_mouse_move(driver, 2)
        logout_btn = wait_for_css(driver, 'a#HREF_Logout', timeout=10)
        if not logout_btn:
            fail("Logout button not found")
        human_click(driver, logout_btn)
        time.sleep(2)
        confirm_btn = wait_for_css(driver, 'a#LOG_OUT', timeout=10)
        if not confirm_btn:
            fail("Confirm logout button not found")
        human_click(driver, confirm_btn)
        time.sleep(3)
        ok("Logged out")

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
    parser = argparse.ArgumentParser(description="UCO Bank automated login test.")
    parser.add_argument("--profile", default=None, help="AdsPower profile ID (default: first)")
    args = parser.parse_args()

    profile = args.profile or get_first_profile_id()
    run(profile_id=profile)
