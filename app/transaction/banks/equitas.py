import time
from multiprocessing import Queue, Value

from app.transaction.base import TransactionManager


class EquitasTransactionManager(TransactionManager):
    """
    Transaction manager for Equitas Small Finance Bank internet banking portal.
    URL: https://ib.equip.equitas.bank.in/login

    Login details available in self.login_details:
        username        — net banking User ID / Customer ID
        password        — net banking password
        mobile_number   — registered mobile number (for OTP)
        account_number  — bank account number

    Login flow:
      (steps to be filled in)

    Operations supported:
    - login_check  : login + scrape account number → report back
    - sync         : login + scrape transactions → report each to server
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        super().__init__(command, child_status, update_flag)
        self.home_url: str = "https://equitas.bank.in/personal-banking/ways-to-bank/internet-banking/"
        self.institution_name: str = "EQUITAS"
        self.balance: float = 0.0
        self.account_no = self.login_details.get("account_number", "")
        self.clear_cache: bool = True

    # HELPERS

    def _js_click(self, el) -> None:
        """JS click — bypasses overlay elements that intercept normal clicks."""
        self.driver.execute_script("arguments[0].click();", el)

    # LOGIN

    def login(self) -> bool:
        """
        Full login for Equitas Bank internet banking.
        Returns True on success, False on any failure.
        """
        try:
            self.debug("Starting Equitas login")
            self.update()

            # ── Step 1: Navigate to internet banking page ─────────────────────
            self.debug("Navigating to Equitas internet banking page")
            self.get(self.home_url)
            self.random_sleep(2, 3)

            # ── Step 2: Click Login button → opens new tab ────────────────────
            self.debug("Clicking Login button")
            login_btn = self.find_by_css('button img[src*="Polygon_5"]', timeout=15)
            if not login_btn:
                raise Exception("Login button not found")
            self._js_click(self.driver.execute_script("return arguments[0].closest('button');", login_btn))
            self.random_sleep(2, 3)

            # ── Step 3: Switch to newly opened tab ────────────────────────────
            self.debug("Switching to new tab")
            if len(self.driver.window_handles) > 1:
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.debug(f"Switched to new tab: {self.driver.current_url}")
            self.random_sleep(2, 3)

            # TODO: next steps

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
