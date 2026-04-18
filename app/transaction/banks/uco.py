import base64
import tempfile
import os
import time
import requests
from multiprocessing import Queue, Value
from urllib.parse import urljoin

from app.transaction.base import TransactionManager


class UCOTransactionManager(TransactionManager):
    """
    Transaction manager for UCO Bank internet banking portal.
    URL: https://www.ucoebanking.in/

    Login details available in self.login_details:
        username        — net banking User ID
        password        — net banking password
        mobile_number   — registered mobile number (for OTP)
        account_number  — bank account number

    Login flow (two-stage):
      Stage 1 — User ID + Captcha
        1. Navigate directly to retail login URL
        2. Enter User ID
        3. Download captcha image via requests (session cookies) → solve via 2Captcha
        4. Enter captcha text
        5. JS-click Submit (input#STU_VALIDATE_CREDENTIALS)

      Stage 2 — Password
        6. Wait for password field
        7. JS-click password field, type password char by char via send_keys
        8. JS-click Submit (input#VALIDATE_STU_CREDENTIALS)

      Post-login
        9.  Scrape account number (visible directly on post-login page)

    Operations supported:
    - login_check  : login + scrape account number → report back
    - sync         : login + scrape transactions → report each to server
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        super().__init__(command, child_status, update_flag)
        self.retail_login_url: str = (
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
        self.institution_name: str = "UCO"
        self.balance: float = 0.0
        self.account_no = self.login_details.get("account_number", "")
        self.clear_cache: bool = False  # navigating directly to login URL — no cache clearing needed

    # HELPERS
    
    def _js_click(self, el) -> None:
        """JS click — bypasses overlay elements that intercept normal clicks."""
        self.driver.execute_script("arguments[0].click();", el)

    def _download_captcha_b64(self) -> str:
        """
        Download the captcha image using the browser's session cookies so the
        server returns the actual image (not a redirect/error).
        Returns base64-encoded PNG string ready for self.solve().
        """
        img = self.find_by_css('img#IMAGECAPTCHA', timeout=10)
        if not img:
            raise Exception("Captcha image not found (img#IMAGECAPTCHA)")

        src = img.get_attribute("src")
        captcha_url = urljoin(self.driver.current_url, src)
        session_cookies = {c["name"]: c["value"] for c in self.driver.get_cookies()}
        headers = {
            "Referer": self.driver.current_url,
            "User-Agent": self.driver.execute_script("return navigator.userAgent;"),
        }
        resp = requests.get(captcha_url, cookies=session_cookies, headers=headers, timeout=15)
        if not resp.ok:
            raise Exception(f"Failed to download captcha image: HTTP {resp.status_code}")

        return base64.b64encode(resp.content).decode("utf-8")

    # LOGIN

    def login(self) -> bool:
        """
        Full two-stage login for UCO Bank internet banking.
        Returns True on success, False on any failure.
        """
        try:
            self.debug("Starting UCO login")
            self.update()

            # ── Step 1: Navigate directly to retail login page ────────────────
            self.debug("Navigating to UCO retail login page")
            self.get(self.retail_login_url)
            if not self.wait_for_element_by_css('input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=30):
                raise Exception("Login page did not load")
            self.random_sleep(2, 3)

            # ── Step 2: Enter User ID ─────────────────────────────────────────
            self.debug("Entering User ID")
            self.send_keys(
                self.find_by_css('input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=10),
                self.username
            )
            self.random_sleep(1, 2)

            # ── Step 3: Solve captcha ─────────────────────────────────────────
            self.debug("Downloading and solving captcha")
            captcha_b64 = self._download_captcha_b64()
            captcha_text = self.solve(captcha_b64)
            if not captcha_text:
                raise Exception("Could not solve captcha")
            self.debug(f"Captcha solved: {captcha_text}")

            # ── Step 4: Enter captcha text ────────────────────────────────────
            self.send_keys(
                self.find_by_css('input[name="AuthenticationFG.VERIFICATION_CODE"]', timeout=10),
                captcha_text
            )
            self.random_sleep(1, 2)

            # ── Step 5: Submit stage 1 ────────────────────────────────────────
            self.debug("Submitting stage 1 (User ID + Captcha)")
            self._js_click(self.find_by_css('input#STU_VALIDATE_CREDENTIALS', timeout=10))
            self.random_sleep(3, 5)

            # ── Step 6: Wait for password field ───────────────────────────────
            self.debug("Waiting for password field")
            if not self.wait_for_element_by_css('input[name="AuthenticationFG.ACCESS_CODE"]', timeout=30):
                raise Exception("Password field not found — stage 1 may have failed (wrong captcha/User ID)")

            # ── Step 7: Enter password character by character ─────────────────
            self.debug("Entering password")
            pw_field = self.find_by_css('input[name="AuthenticationFG.ACCESS_CODE"]', timeout=10)
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pw_field)
            self.random_sleep(0.5, 1)
            self._js_click(pw_field)
            self.random_sleep(1, 1.5)
            for char in self.password:
                pw_field.send_keys(char)
                time.sleep(0.15)
            self.random_sleep(0.5, 1)

            # ── Step 8: Submit stage 2 ────────────────────────────────────────
            self.debug("Submitting stage 2 (Password)")
            self._js_click(self.find_by_css('input#VALIDATE_STU_CREDENTIALS_UX', timeout=10))
            self.random_sleep(3, 5)

            # ── Step 9: Scrape account number (visible on post-login page) ───────
            self.debug("Waiting for account number")
            acct_el = self.find_by_css(
                'a[name="HREF_OperativeAccountsWidgetFG.OPR_ACCOUNT_NUMBER_ARRAY[0]"]',
                timeout=15,
            )
            if acct_el:
                self.account_no = acct_el.text.strip()
                self.debug(f"Account number scraped: {self.account_no}")

            self.info("UCO login success")
            return True

        except Exception as e:
            self.error(e)
            return False

    # LOGOUT

    def logout(self) -> bool:
        try:
            self.debug("Starting UCO logout")
            self.update()
            self._js_click(self.find_by_css('a#HREF_Logout', timeout=10))
            self.random_sleep(2, 3)
            self._js_click(self.find_by_css('a#LOG_OUT', timeout=10))
            self.random_sleep(2, 3)
            self.debug("UCO logout success")
            return True
        except Exception as e:
            self.error(e)
            return False

    # SYNC

    def sync_transactions(self) -> bool:
        """
        Scrape and report transactions for UCO Bank.

        TODO: Implement once login flow is confirmed working.
        Follow the same pattern as kvb.py sync_transactions():
        - Call fetch_sync_mode() to get previous_hash + refresh_time
        - Loop: scrape transactions, hash each, report new ones
        - Call report_no_transactions_found() when at tip
        - Sleep refresh_time between loops
        - Return True on clean exit (sleep mode received)
        """
        try:
            self.info("Starting UCO sync")
            raise NotImplementedError("UCO sync_transactions not yet implemented")
        except Exception as e:
            self.error(e)
            return False
