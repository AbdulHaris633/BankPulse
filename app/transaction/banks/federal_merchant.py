import re
import json
import hashlib
import time
from multiprocessing import Queue, Value

from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)


class FederalMerchantTransactionManager(TransactionManager):
    """
    Transaction manager for Federal Bank merchant portal.
    URL: https://portal.federalbank.co.in/AccountStmt/log.jsp

    Handles both FEDERAL_MERCHANT (current account) and FEDERALSAVINGS variants
    — account_type is set based on institution_name from the command.

    Operations supported:
    - login_check : account_number (MIS) + password + image CAPTCHA
    - sync        : scrapes paginated merchant transaction table, reports new
                    transactions to server with SHA-256 hash chaining

    Login credentials:
    - username → login_details["mis_username"]  (overrides base class default)
    - password → login_details["mis_pwd"]        (overrides base class default)
    - account_no → same as username (mis_username)
    - device_id → login_details["device_id"]     (stored as panel_device_id)

    Differences from FederalTransactionManager (fednetbank.com):
    - Different URL: portal.federalbank.co.in/AccountStmt/log.jsp
    - Login flow: account_number field → password → CAPTCHA → submit →
      #gobutton → transaction table (input[type="search"])
    - Transaction table columns differ: channel, device_id, ref_no,
      timestamp, remitter, amount, status, remarks, ticket
    - No add_beneficiary / payout support

    V1 location: federal_merchant_transaction_manager.py
    V2 location: app/transaction/banks/federal_merchant.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://portal.federalbank.co.in/AccountStmt/log.jsp"
        self.institution_name: str = self.command["institution_name"]
        self.account_type: str = "CURRENT"
        if self.institution_name == "FEDERALSAVINGS":
            self.account_type = "SAVINGS"
        self.captcha_image: str = "federal_captcha.png"
        self.panel_device_id: str = self.login_details["device_id"]
        # MIS credentials override base class username/password
        self.username: str = self.login_details["mis_username"]
        self.password: str = self.login_details["mis_pwd"]
        self.account_no: str = self.username
        self.data["account_number"] = self.username
        self.reached_tip: bool = False

    def login(self) -> bool:
        try:
            self.get(self.url)
            self.wait_for_element_by_css('#account_number', timeout=30)
            self.random_sleep(2, 3)
            self.maximize_window()
            self.send_keys(self.find_by_id('account_number'), self.username)
            self.random_sleep(2, 3)
            self.maximize_window()
            self.send_keys(self.find_by_id('password'), self.password)
            self.random_sleep(2, 3)
            captcha_text = self.solve_captcha()
            if not captcha_text:
                self.click(self.find_by_css('a[title="change captcha text"]'))
                self.random_sleep(5, 8)
                captcha_text = self.solve_captcha()
            self.maximize_window()
            self.send_keys(self.find_by_id('answer'), captcha_text)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('button[type="submit"]'))
            self.wait_for_element_by_css('#gobutton', timeout=30)
            self.random_sleep(2, 3)
            self.click(self.find_by_id('gobutton'))
            self.wait_for_element_by_css('input[type="search"]', timeout=30)
            self.random_sleep(2, 3)
            self.info("Login success")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def solve_captcha(self) -> str:
        """
        Screenshot the CAPTCHA element (#captcha_id) as base64 and solve it
        via self.solve() (2Captcha → AntiCaptcha fallback in base class).
        """
        try:
            captcha_b64 = self.find_by_id('captcha_id').screenshot_as_base64
            captcha_text = self.solve(captcha_b64)
            self.update()
            return captcha_text
        except Exception as e:
            self.error(e)
            return ""

    def logout(self) -> bool:
        try:
            self.update()
            self.debug("Starting logout")
            self.click(self.find_by_css('a[data-toggle="dropdown"]'))
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a[href="Logout"]'))
            self.wait_for_element_by_css('#account_number', timeout=30)
            self.random_sleep(2, 3)
            self.debug("Logout finished")
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_recent_transaction_list(self) -> bool:
        """
        Refresh the transaction list, select the correct device ID, then
        show up to 2000 entries. Reports no-transactions if none found.
        """
        try:
            self.debug("Opening transactions list")
            self.update()
            self.click(self.find_by_css('button[onclick*="refresh"]'))
            self.wait_for_element_by_css('input[type="search"]', timeout=30)
            self.random_sleep(2, 3)
            device_id_options = self.find_by_css('#deviceId option', multiple=True)
            if len(device_id_options) > 1:
                self.debug("Clicking device ID")
                try:
                    for device_id_option in device_id_options:
                        if device_id_option.text.strip() == self.panel_device_id.strip():
                            self.click(device_id_option)
                            self.random_sleep(2, 3)
                            break
                except Exception as e:
                    self.warn("Error clicking device ID")
                    self.warn(e)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('option[value="2000"]'))
            self.random_sleep(2, 3)
            if re.search(r'No Transactions Found', self.driver.page_source, re.M):
                self.debug("No transactions found")
                self.report_no_transactions_found()
            self.debug("Transactions list opened")
            return True
        except Exception as e:
            self.error(e)
            return False

    def sync_transactions(self) -> bool:
        try:
            self.info("Starting transaction sync")
            webdriver_errors = 0
            while True:
                try:
                    self.update()
                    sync_mode = fetch_sync_mode(self.instruction_id)
                    self.debug("Sync mode: " + str(sync_mode))
                    sync_mode_command = sync_mode["command"]
                    operation = sync_mode_command["operation"]
                    latest_tx_found = False
                    new_transactions = []
                    if operation == "sleep":
                        self.info("Sync mode changed, starting logout")
                        self.data["query"] = "sync"
                        self.data["action"] = "sleep"
                        if not self.logout():
                            if not report_sync_status(self.data, False):
                                raise Exception("Could not report sync status")
                            raise Exception("Could not logout of Federal Merchant, exiting sync")
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True
                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]
                    while not latest_tx_found:
                        self.update()
                        self.debug("Starting transaction search")
                        if not self.open_recent_transaction_list():
                            raise Exception("Could not open recent transaction list")

                        # Loop through all transactions (oldest first after reverse)
                        transactions = self.find_by_css('tbody tr', multiple=True)
                        if len(transactions) == 1:
                            tx_text = transactions[0].text.strip()
                            if tx_text == "No Transactions Found":
                                self.debug("No transactions found")
                                self.report_no_transactions_found()
                                transactions = []
                        transactions.reverse()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_data = self.find_by_tag('td', multiple=True, parent=transaction)
                                tx_channel = tx_data[1].text.lower().strip()
                                tx_device_id = tx_data[2].text.lower().strip()
                                tx_ref_no = tx_data[3].text.lower().strip()
                                tx_timestamp = tx_data[4].text.lower().strip()
                                tx_remitter = tx_data[5].text.lower().strip()
                                tx_amount = tx_data[6].text.lower().strip()
                                tx_status = tx_data[7].text.lower().strip()
                                tx_remarks = tx_data[8].text.lower().strip()
                                tx_ticket = tx_data[9].text.lower().strip()
                                tx_info = {
                                    "tx_channel": tx_channel,
                                    "tx_device_id": tx_device_id,
                                    "tx_ref_no": tx_ref_no,
                                    "tx_timestamp": tx_timestamp,
                                    "tx_remitter": tx_remitter,
                                    "tx_amount": tx_amount,
                                    "tx_status": tx_status,
                                    "tx_remarks": tx_remarks,
                                    "tx_ticket": tx_ticket,
                                }
                                tx_info_hash_str = json.dumps(tx_info, separators=(',', ':'))
                                tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()
                                if tx_hash == sync_mode_command["latest_tx_hash"]:
                                    self.info("Latest tx_hash found: " + tx_hash)
                                    latest_tx_found = True
                                    self.reached_tip = True
                                    break
                                transaction_json = {
                                    "instruction_id": self.instruction_id,
                                    "query": "sync",
                                    "action": "sync_tx",
                                    "success": 0,
                                    "trader_id": self.trader_id,
                                    "bot_id": self.bot_id,
                                    "credit_tx": 1,
                                    "tx_amount": tx_amount,
                                    "tx_desc": tx_remarks,
                                    "previous_hash": "",
                                    "tx_info": tx_info,
                                    "tx_hash": tx_hash,
                                    "new_state_hash": "",
                                }
                                self.info("New transaction: " + str(transaction_json))
                                new_transactions.append(transaction_json)
                            except Exception as e:
                                self.warn("Error parsing transaction")
                                self.warn(e)
                            if transaction == transactions[len(transactions) - 1]:
                                latest_tx_found = True
                                self.reached_tip = True
                                break

                    # Send new transactions to server with hash chaining
                    if new_transactions:
                        self.info("New transactions found")
                        new_transactions.reverse()
                        for transaction in new_transactions:
                            self.update()
                            latest_tx_hash = transaction["tx_hash"]
                            previous_hash_str = "{}::{}".format(previous_tx_hash, latest_tx_hash)
                            new_state_hash = hashlib.sha256(previous_hash_str.encode()).hexdigest()
                            transaction["new_state_hash"] = new_state_hash
                            transaction["previous_hash"] = previous_tx_hash
                            previous_tx_hash = new_state_hash
                            self.info("Sending new transaction: " + str(transaction))
                            if not report_sync_status(transaction, True):
                                raise Exception("Could not report sync status")
                    else:
                        self.info("No new transactions found")
                        self.update()
                        self.reached_tip = True
                        latest_tx_hash = sync_mode["command"]["latest_tx_hash"]
                        self.data["query"] = "sync"
                        self.data["action"] = "reached_tip"
                        self.data.update({"latest_tx_hash": latest_tx_hash})
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                    self.debug("Sleeping for: " + str(refresh_time))
                    time.sleep(refresh_time)
                except WebDriverException as e:
                    self.warn("Webdriver error detected during sync")
                    self.warn(e)
                    self.random_sleep(5, 8)
                    if webdriver_errors == 3:
                        self.error("Number of webdriver errors exceeds 3, exiting")
                        raise Exception(str(e))
                    webdriver_errors += 1
                    self.random_sleep(2, 3)
                    if not self.open_recent_transaction_list():
                        self.warn("Could not reopen transaction list after webdriver error")
        except Exception as e:
            self.error(e)
            return False
