import re
import os
import json
import hashlib
import time
from datetime import datetime, timedelta
from multiprocessing import Queue, Value

import pandas
from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)

_settings = Settings()


class KarnatakaTransactionManager(TransactionManager):
    """
    Transaction manager for Karnataka Bank (KTK Bank) netbanking portal.
    URL: https://moneyclick.ktkbank.com/...

    Handles KARNATAKA (current account) and KARNATAKASAVINGS (savings account).

    Operations supported:
    - login_check    : username + image CAPTCHA (#IMAGECAPTCHA) + password
                       with confirm checkbox; account_no scraped after login
    - sync           : downloads Excel statement (yesterday→today), parses
                       with pandas, reports new transactions to server
    - scrape_balance : clicks home button, reads availBalOutput span

    Statement parsing:
    - Uses pandas.read_excel on the downloaded .xlsx file
    - Column layout: col[2]=date, col[5]=remarks, col[12]=debit,
      col[13]=credit, col[16]=balance
    - Date format in file: MM,DD,YYYY (e.g. "03,31,2026")

    V1 location: karnataka_transaction_manager.py
    V2 location: app/transaction/banks/karnataka.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - download_dir → _settings.DOWNLOAD_DIR
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = (
            "https://moneyclick.ktkbank.com/BankAwayRetail/AuthenticationController"
            "?__START_TRAN_FLAG__=Y&FORMSGROUP_ID__=AuthenticationFG&__EVENT_ID__=LOAD"
            "&FG_BUTTONS__=LOAD&ACTION.LOAD=Y&AuthenticationFG.LOGIN_FLAG=1"
            "&BANK_ID=KBL&LANGUAGE_ID=001&USR_TYPE=1"
        )
        self.institution_name: str = self.command["institution_name"]
        self.account_type: str = "CURRENT"
        if self.institution_name == "KARNATAKASAVINGS":
            self.account_type = "SAVINGS"
        self.reached_tip: bool = False

    def login(self) -> bool:
        username = 'input[title="User ID"]'
        captcha = 'input[placeholder="Enter Captcha"]'
        confirm_checkbox = 'label[for="AuthenticationFG.TARGET_CHECKBOX"]'
        password = 'input[title="Enter Password"]'
        account_no_css = 'a[title="View Details"]'
        try:
            for i in range(0, 3):
                self.update()
                self.debug("Starting login")
                self.get(self.url)
                self.wait_for_element_by_css(username, timeout=30)
                self.maximize_window()
                self.random_sleep(2, 3)
                self.debug("Sending username")
                self.send_keys(self.find_by_css(username), self.username)
                self.random_sleep(2, 3)
                captcha_solved = False
                captcha_text = ""
                for z in range(0, 5):
                    captcha_b64 = self.find_by_id('IMAGECAPTCHA').screenshot_as_base64
                    captcha_text = self.solve(captcha_b64)
                    if captcha_text:
                        captcha_solved = True
                        break
                    self.random_sleep(5, 8)
                if not captcha_solved:
                    raise Exception("Could not solve captcha")
                self.send_keys(self.find_by_css(captcha), captcha_text.upper())
                self.random_sleep(2, 3)
                self.click(self.find_by_css('input[title="Login"]'))
                self.update()
                if not self.wait_for_element_by_css(confirm_checkbox, timeout=30):
                    self.warn("Could not find password page, retrying")
                    continue
                break
            self.debug("Sending password")
            self.maximize_window()
            self.random_sleep(2, 3)
            self.click(self.find_by_css(confirm_checkbox))
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css(password), self.password)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('input[title="Login"]'))
            self.wait_for_element_by_css(account_no_css, timeout=30)
            self.random_sleep(2, 3)
            self.debug("Scraping account no.")
            self.update()
            account_no = self.find_by_css(account_no_css).text.strip()
            if not account_no:
                raise Exception("Could not scrape account no.")
            self.account_no = account_no
            self.debug("Account no. found: " + str(self.account_no))
            self.info("Login success")
            return True
        except Exception as e:
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            self.debug("Starting logout")
            self.update()
            self.click(self.find_by_css('a[title="Log out"] > i'))
            self.wait_for_element_by_css('#span_LOG_OUT', timeout=30)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('#span_LOG_OUT'))
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            self.debug("Logout success")
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_statement(self) -> bool:
        """
        Navigate to Account Statement via the nav menu, select the scraped
        account number, set date range yesterday→today, and click Search.
        Retries up to 5 times on WebDriverException.
        """
        inner_nav_btn = 'ul.dropdown-content a[id*="Opening_Account-Statement"]'
        statement_opened = False
        try:
            for i in range(0, 5):
                try:
                    self.debug("Opening statement")
                    self.debug("Clicking nav link")
                    self.update()
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css('#parentTop_RACTS > a'))
                    self.random_sleep(2, 3)
                    self.click(self.find_by_id('IL_RACTS_10'))
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css(inner_nav_btn))
                    self.wait_for_element_by_css('input[title="Account"] ~ a', timeout=30)
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    self.debug("Selecting account")
                    self.click(self.find_by_css('input[title="Account"] ~ a'))
                    self.random_sleep(2, 3)
                    self.update()
                    menu_items = self.find_by_css('li.ui-menu-item', multiple=True)
                    for menu_item in menu_items:
                        if re.search(self.account_no, menu_item.text):
                            self.click(menu_item)
                            break
                    self.random_sleep(2, 3)
                    self.debug("Selecting dates")
                    date_from = self.find_by_css('input[title="Date From"]')
                    date_to = self.find_by_css('input[title="Date To"]')
                    day_delta = timedelta(days=1)
                    today = datetime.today()
                    yesterday = today - day_delta
                    to_date_text = today.strftime('%m,%d,%Y')
                    from_date_text = yesterday.strftime('%m,%d,%Y')
                    date_from.clear()
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    date_from.send_keys(from_date_text)
                    self.click(self.find_by_css('input[name="Action.SEARCH"]'))
                    self.wait_for_element_by_css('input[title="Select Download Format"]', timeout=30)
                    self.random_sleep(2, 3)
                    self.debug("Statement opened")
                    self.update()
                    statement_opened = True
                    break
                except WebDriverException as ex:
                    self.warn(ex)
                    self.warn("Webdriver exception opening statement, retrying")
            if not statement_opened:
                raise Exception("Could not open statement")
            return True
        except Exception as e:
            self.error(e)
            return False

    def download_statement(self) -> bool:
        """
        Select the last download format from the dropdown, click Generate Report,
        and wait for the file to appear in the download directory.
        """
        download_format = 'input[title="Select Download Format"] ~ a'
        download_btn = 'input[name="Action.GENERATE_REPORT"]'
        try:
            self.debug("Downloading statement")
            self.update()
            self.maximize_window()
            self.random_sleep(2, 3)
            self.click(self.find_by_css(download_format))
            self.random_sleep(2, 3)
            menu_items = self.find_by_css('li.ui-menu-item', multiple=True)
            self.click(menu_items.pop())
            self.random_sleep(2, 3)
            self.click(self.find_by_css(download_btn))
            self.random_sleep(5, 8)
            for i in range(0, 3):
                self.update()
                if self.get_most_recent_download():
                    break
                self.random_sleep(5, 8)
            self.debug("Statement download finished")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """
        Parse the most recently downloaded Excel statement using pandas.
        Column layout: col[2]=date (MM,DD,YYYY), col[5]=remarks,
        col[12]=debit, col[13]=credit, col[16]=balance.
        Rows with no valid date are skipped (header/footer rows).
        """
        try:
            self.debug("Parsing transactions")
            self.update()
            statement_file_path = _settings.DOWNLOAD_DIR + os.sep + self.get_most_recent_download()
            statement_file_data = pandas.read_excel(statement_file_path)
            transactions = []
            for index, row in statement_file_data.iterrows():
                try:
                    self.update()
                    date_regex = r'^\s*\d\d,\d\d,\d\d\d\d\s*$'
                    date_col_data = str(row[2]).strip()
                    if not re.match(date_regex, date_col_data):
                        continue
                    tx_date = date_col_data
                    tx_remarks = str(row[5]).strip()
                    tx_debit = str(row[12]).replace(',', '').strip()
                    tx_credit = str(row[13]).replace(',', '').strip()
                    total_available_balance = str(row[16]).replace(',', '').strip()
                    if tx_debit == "nan":
                        tx_debit = ""
                        credit_tx = 1
                        tx_amount = tx_credit
                    else:
                        tx_credit = ""
                        credit_tx = 0
                        tx_amount = tx_debit
                    tx_data = {
                        "tx_date": tx_date,
                        "tx_remarks": tx_remarks,
                        "tx_debit": tx_debit,
                        "tx_credit": tx_credit,
                        "credit_tx": credit_tx,
                        "tx_amount": tx_amount,
                        "total_available_balance": total_available_balance,
                    }
                    transactions.append(tx_data)
                except Exception as e:
                    self.warn("Error parsing transaction: " + str(dict(row)))
                    self.warn(e)
            self.debug("Number of transactions found: " + str(len(transactions)))
            self.debug("Finished parsing transactions")
            return transactions
        except Exception as e:
            self.error(e)
            return []

    def download_and_parse_statement(self) -> list:
        try:
            if not self.download_statement():
                raise Exception("Could not download statement")
            return self.parse_statement()
        except Exception as e:
            self.error(e)
            return []

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
                            raise Exception("Could not logout of Karnataka Bank, exiting sync")
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True
                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]
                    while not latest_tx_found:
                        self.update()
                        self.debug("Starting transaction search")
                        if not self.open_statement():
                            raise Exception("Could not open recent transaction list")

                        # Loop through all transactions
                        transactions = self.download_and_parse_statement()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_info = transaction
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
                                    "credit_tx": tx_info["credit_tx"],
                                    "tx_amount": tx_info["tx_amount"],
                                    "tx_desc": tx_info["tx_remarks"],
                                    "total_available_balance": tx_info["total_available_balance"],
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
                                self.info("Last transaction, exiting")
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
                    if not self.open_statement():
                        self.warn("Could not switch tabs")
                    self.random_sleep(2, 3)
        except Exception as e:
            self.error(e)
            return False

    def scrape_balance(self) -> None:
        home_btn = '#parentTop_DASHAT #ID_DASHAT'
        balance_span = 'span[id*="availBalOutput"]'
        try:
            self.debug("Scraping balance")
            self.update()
            self.maximize_window()
            self.random_sleep(2, 3)
            self.click(self.find_by_css(home_btn))
            self.wait_for_element_by_css('span[id*="availBalOutput"]', timeout=30)
            self.random_sleep(2, 3)
            balance_text = self.find_by_css(balance_span).text.strip()
            balance = balance_text.split()[1].replace(',', '').strip()
            self.update_balance(balance)
            self.debug("Balance scraped: " + str(balance))
        except Exception as e:
            self.error(e)
