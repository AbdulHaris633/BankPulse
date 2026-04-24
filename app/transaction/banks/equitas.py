import time
from multiprocessing import Queue, Value

from app.transaction.base import TransactionManager


class EquitasTransactionManager(TransactionManager):
    """
    Transaction manager for Equitas Small Finance Bank internet banking portal.
    URL: https://ib.equip.equitasbank.com/login

    Login details available in self.login_details:
        username        — net banking User ID / Customer ID
        password        — net banking password
        mobile_number   — registered mobile number (for OTP)
        account_number  — bank account number

    Login flow:
      1. Navigate to login portal
      2. Enter User ID char by char  (human_type)
      3. Enter password char by char (human_type)
      4. Extract inline base64 captcha → solve via 2Captcha → enter char by char
      5. Click Accounts button       (human_click)
      6. Click Login button          (human_click)

    Operations supported:
    - login_check  : login + scrape account number → report back
    - sync         : login + scrape transactions → report each to server
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        super().__init__(command, child_status, update_flag)
        self.home_url: str = "https://equitas.bank.in/personal-banking/ways-to-bank/internet-banking/"
        self.login_url: str = "https://ib.equip.equitasbank.com/login"
        self.institution_name: str = "EQUITAS"
        self.balance: float = 0.0
        self.account_no = self.login_details.get("account_number", "")
        self.clear_cache: bool = True

    # LOGIN

    def login(self) -> bool:
        """
        Full login for Equitas Bank internet banking.
        Returns True on success, False on any failure.
        """
        try:
            self.debug("Starting Equitas login")
            self.update()

            # ── Step 1: Navigate to login portal ─────────────────────────────
            self.debug("Navigating to Equitas login portal")
            self.get(self.login_url)
            self.random_sleep(13, 17)
            self.random_movements()

            # ── Step 2: Enter User ID char by char ────────────────────────────
            self.debug("Waiting for User ID field")
            if not self.wait_for_element_by_css('#login_userId_new', timeout=30):
                raise Exception("Login page did not load — User ID field not found")
            self.random_movements()
            user_field = self.find_by_css('#login_userId_new', timeout=10)
            self.human_click(user_field)
            self.random_sleep(0.3, 0.7)
            self.human_type(self.username, user_field)
            self.random_sleep(1, 2)

            # ── Step 3: Enter password char by char ───────────────────────────
            self.debug("Entering password")
            self.random_movements()
            pw_field = self.find_by_css('#login_password', timeout=10)
            self.human_click(pw_field)
            self.random_sleep(0.3, 0.7)
            self.human_type(self.password, pw_field)
            self.random_sleep(1, 2)

            # ── Step 4: Extract inline base64 captcha and solve ───────────────
            self.debug("Extracting captcha image")
            captcha_img = self.find_by_css('img[src^="data:image/png;base64"]', timeout=15)
            if not captcha_img:
                raise Exception("Captcha image not found")
            src = captcha_img.get_attribute("src")
            captcha_b64 = src.split(",", 1)[1] if "," in src else src

            self.debug("Solving captcha")
            captcha_text = self.solve(captcha_b64)
            if not captcha_text:
                raise Exception("Could not solve captcha")
            self.debug(f"Captcha solved: {captcha_text}")

            # ── Step 5: Enter captcha text char by char ───────────────────────
            self.random_movements()
            cap_field = self.find_by_css('.captcha-page__text-field input', timeout=10)
            self.human_click(cap_field)
            self.random_sleep(0.3, 0.6)
            self.human_type(captcha_text, cap_field)
            self.random_sleep(1, 2)

            # ── Step 6: Click Accounts button ─────────────────────────────────
            self.debug("Clicking Accounts button")
            self.random_movements()
            accounts_btn = self.find_by_xpath('//button[normalize-space(.)="Accounts"]', timeout=10)
            if not accounts_btn:
                raise Exception("Accounts button not found")
            self.human_click(accounts_btn)
            self.random_sleep(1, 2)

            # ── Step 7: Click Login button ────────────────────────────────────
            self.debug("Clicking Login button")
            self.random_movements()
            login_btn = self.find_by_css('button.login-page__login-button', timeout=10)
            if not login_btn:
                raise Exception("Login button not found")
            self.human_click(login_btn)
            self.random_sleep(3, 5)
            time.sleep(60)

            self.info("Equitas login success")
            return True

        except Exception as e:
            self.error(e)
            return False

    # LOGOUT

    def logout(self) -> bool:
        try:
            self.debug("Starting Equitas logout")
            self.update()

            # TODO: implement logout

            self.debug("Equitas logout success")
            return True
        except Exception as e:
            self.error(e)
            return False

    # SYNC

    def sync_transactions(self) -> bool:
        """
        Scrape and report transactions for Equitas Bank.
        TODO: Implement once login flow is confirmed working.
        """
        try:
            self.info("Starting Equitas sync")
            raise NotImplementedError("Equitas sync_transactions not yet implemented")
        except Exception as e:
            self.error(e)
            return False
