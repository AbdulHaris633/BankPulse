import re
import os
import json
import hashlib
import time
from multiprocessing import Queue, Value

import pandas
from selenium.common.exceptions import WebDriverException

from app.transaction.banks.rbl_base import RBLBankTransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)

_settings = Settings()


class RBLCorporateTransactionManager(RBLBankTransactionManager):
    """
    Transaction manager for RBL Bank Corporate netbanking portal.
    URL: https://online.rblbank.com/CorpBank/AuthenticationController?...BANK_ID=176

    Inherits shared RBL utilities from RBLBankTransactionManager:
    - rbl_click(), select_date(), wait_for_loading_widget()
    - download_statement(), download_and_parse_statement()

    Operations supported:
    - login_check : same two-step flow as RBLSP; account_no scraped from
                    a[title="View Account Details"]
    - sync        : downloads XLS, parses with pandas, hash chaining;
                    handles self.no_transactions flag (set by open_recent_
                    transaction_list when portal returns no results)
    - scrape_balance : clicks #ID_DASHAT home, reads td.amountRightAlign

    open_recent_transaction_list differences vs RBLSP:
    - Uses #cssmenu_new #ID_CACTS nav + sub_submenu[1] click
    - Table selector: 'table.customStatementTable' (vs RBLSP's tbody tr)
    - Webdriver error threshold: 10 (vs RBLSP's 5)
    - On NoSuchElementException from rbl_click(search_btn): checks for
      no_transactions text, sets self.no_transactions=True and returns True
      immediately (sync loop then skips that cycle)

    parse_statement column layout (pandas, Excel) — differs from RBLSP:
    - row[0]=tx_date, row[1]=remarks, row[2]=ref_no, row[3]=value_date,
      row[4]=withdrawal, row[5]=deposit, row[6]=balance

    V1 location: rbl_corporate_transaction_manager.py
    V2 location: app/transaction/banks/rbl_corporate.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - Inherits from RBLBankTransactionManager (app/transaction/banks/rbl_base.py)
    - download_dir → _settings.DOWNLOAD_DIR
    - Triple-quoted / inline commented blocks converted to # comments
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = (
            "https://online.rblbank.com/CorpBank/AuthenticationController"
            "?__START_TRAN_FLAG__=Y&FORMSGROUP_ID__=AuthenticationFG"
            "&__EVENT_ID__=LOAD&FG_BUTTONS__=LOAD&ACTION.LOAD=Y"
            "&AuthenticationFG.LOGIN_FLAG=1&BANK_ID=176&LANGUAGE_ID=001"
        )
        self.institution_name: str = "RBLCORP"
        self.reached_tip: bool = False
        self.sync_last_month: bool = True   # reserved — not currently used
        self.no_transactions: bool = False  # set True when portal returns no results

    def login(self) -> bool:
        try:
            username_input = 'input[id="AuthenticationFG.CUSTOM_USER_PRINCIPAL"]'
            confirm_checkbox = 'input[id="AuthenticationFG.TARGET_CHECKBOX"] ~ span'
            password_input = 'input[id="AuthenticationFG.ACCESS_CODE"]'
            account_no_selector = 'a[title="View Account Details"]'
            self.debug("Starting login")
            self.update()
            self.get(self.url)
            self.wait_for_element_by_css(username_input, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Sending username")
            self.send_keys(self.find_by_css(username_input), self.username)
            self.random_sleep(1.5, 4)
            self.click(self.find_by_id('STU_VALIDATE_CREDENTIALS'))
            self.wait_for_element_by_css(confirm_checkbox, timeout=30)
            self.update()
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(confirm_checkbox))
            self.random_sleep(1.5, 4)
            self.debug("Sending password")
            self.send_keys(self.find_by_css(password_input), self.password)
            self.random_sleep(1.5, 4)
            self.click(self.find_by_id('VALIDATE_STU_CREDENTIALS_UX'))
            self.wait_for_element_by_css(account_no_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.update()
            self.debug("Scraping account no.")
            account_no = self.find_by_css(account_no_selector).text.strip()
            if not account_no:
                raise Exception("Could not scrape account no.")
            self.account_no = account_no
            self.debug("Account no. scraped: " + self.account_no)
            self.info("Login success")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            logout_link = 'span.logoutLink a[title="Log out"]'
            self.debug("Starting logout")
            self.update()
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(logout_link))
            self.wait_for_element_by_css('#LOG_OUT', timeout=30)
            self.random_sleep(1.5, 4)
            self.update()
            self.click(self.find_by_id('LOG_OUT'))
            self.wait_for_element_by_css('#captionGoToLogin', timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Logout success")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_recent_transaction_list(self) -> bool:
        """
        Navigate to Account Statement via corporate nav menu, apply date filter,
        and click Search. Retries up to 10 WebDriverExceptions.

        On "no transactions" after today: retries with yesterday (start_offset=1).
        Sets self.no_transactions=True so sync loop skips the download cycle.
        """
        accounts_nav_link = '#cssmenu_new #ID_CACTS[class]'
        account_no_btn = 'a[title="Account Number"]'
        more_info_btn = 'a[title="Click to Expand/Collapse"]'
        search_btn = 'input[name="Action.SEARCH"]'
        tx_date_checkbox = 'input[title="Select Transaction Date"] ~ span'
        date_inputs_selector = 'input[data-febatype="FEBADate"]'
        no_transactions_text = "The transactions do not exist"
        webdriver_errors = 0
        try:
            while True:
                try:
                    self.debug("Opening recent transactions list")
                    self.random_sleep(1.5, 4)
                    self.update()
                    self.maximize_window()
                    self.debug("Clicking nav link")
                    # self.driver.find_element(By.CSS_SELECTOR, accounts_nav_link).click()
                    # small_sleep()
                    self.rbl_click(accounts_nav_link, 'ul.sub_submenu > li')
                    self.debug("Clicking submenu link")
                    sub_menu_items = self.find_by_css('ul.sub_submenu > li', multiple=True)
                    self.click(sub_menu_items[1])
                    self.random_sleep(1.5, 4)
                    self.wait_for_element_by_css(account_no_btn, timeout=30)
                    self.maximize_window()
                    self.update()
                    self.random_sleep(1.5, 4)
                    self.debug("Clicking account no.")
                    # self.driver.find_element(By.CSS_SELECTOR, account_no_btn).click()
                    # wait_for_element(self.driver, more_info_btn)
                    # small_sleep()
                    self.rbl_click(account_no_btn, more_info_btn)
                    self.random_sleep(1.5, 4)
                    self.maximize_window()
                    # self.driver.find_element(By.CSS_SELECTOR, more_info_btn).click()
                    # wait_for_element(self.driver, tx_date_checkbox)
                    # small_sleep()
                    self.rbl_click(more_info_btn, tx_date_checkbox)
                    self.update()
                    self.select_date()

                    # Continue
                    self.debug("Clicking continue")
                    self.maximize_window()
                    self.rbl_click(search_btn, 'table.customStatementTable')
                    self.random_sleep(5, 8)
                    self.update()
                    self.wait_for_loading_widget()
                    if re.search(no_transactions_text, self.driver.page_source, re.I | re.M):
                        # Search previous day
                        self.debug("No transactions found, search yesterday")
                        self.no_transactions = True
                        self.rbl_click(more_info_btn, date_inputs_selector)
                        self.select_date(start_offset=1)
                        self.debug("Clicking continue")
                        self.rbl_click(search_btn, 'table.customStatementTable')
                        self.random_sleep(5, 8)
                        self.update()
                        self.wait_for_loading_widget()
                        self.random_sleep(5, 8)
                        if re.search(no_transactions_text, self.driver.page_source, re.I | re.M):
                            self.warn("No transactions found")
                            self.report_no_transactions_found()
                    else:
                        self.debug("Transactions found")
                        self.no_transactions = False
                    break
                except WebDriverException as e:
                    self.update()
                    self.warn(e)
                    self.warn("Webdriver error opening recent transactions list")
                    webdriver_errors += 1
                    if webdriver_errors > 10:
                        raise Exception("Could not open recent transaction list")
                    self.random_sleep(1.5, 4)
            self.debug("Recent transactions list opened")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """
        Parse the most recently downloaded XLS statement using pandas.
        Column layout (differs from RBLSP — one fewer leading column):
        row[0]=tx_date, row[1]=remarks, row[2]=ref_no, row[3]=value_date,
        row[4]=withdrawal, row[5]=deposit, row[6]=balance.
        Rows where col[0] is not a string matching DD/MM/YYYY are skipped.
        """
        try:
            self.update()
            self.debug("Parsing statement")
            statement_file_path = _settings.DOWNLOAD_DIR + os.sep + self.get_most_recent_download()
            self.debug("Statement file: " + statement_file_path)
            df = pandas.read_excel(statement_file_path)
            transactions = []
            for row in df.values:
                tx_date_item = row[0]
                if not (tx_date_item and type(tx_date_item) == str):
                    continue
                if not re.match(r'^\d+/\d+/\d+$', tx_date_item):
                    continue
                tx_date = str(row[0]).strip()
                tx_remarks = str(row[1]).strip()
                tx_ref_no = str(row[2]).strip()
                if tx_ref_no == "nan":
                    tx_ref_no = ""
                tx_value_date = str(row[3]).strip()
                tx_withdrawal = str(row[4]).strip()
                if tx_withdrawal == "nan":
                    tx_withdrawal = ""
                tx_deposit = str(row[5]).strip()
                if tx_deposit == "nan":
                    tx_deposit = ""
                total_available_balance = str(row[6]).strip()
                transaction = {
                    "tx_date": tx_date,
                    "tx_remarks": tx_remarks,
                    "tx_ref_no": tx_ref_no,
                    "tx_value_date": tx_value_date,
                    "tx_withdrawal": tx_withdrawal,
                    "tx_deposit": tx_deposit,
                    "total_available_balance": total_available_balance,
                }
                transactions.append(transaction)
            self.update()
            self.debug("Number of transactions: " + str(len(transactions)))
            self.debug("Statement file parsed")
            return transactions
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
                            raise Exception("Could not logout, exiting sync")
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
                            raise WebDriverException("Could not open recent transaction list")

                        # If portal returned no transactions, skip download and loop
                        if self.no_transactions:
                            self.no_transactions = False
                            break

                        # Loop through all transactions
                        transactions = self.download_and_parse_statement()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_date = transaction["tx_date"]
                                tx_remarks = transaction["tx_remarks"]
                                tx_ref_no = transaction["tx_ref_no"]
                                tx_withdrawal = transaction["tx_withdrawal"]
                                tx_deposit = transaction["tx_deposit"]
                                total_available_balance = transaction["total_available_balance"]
                                if tx_deposit:
                                    credit_tx = 1
                                    tx_amount = tx_deposit
                                else:
                                    credit_tx = 0
                                    tx_amount = tx_withdrawal
                                tx_info = {
                                    "tx_date": tx_date,
                                    "tx_remarks": tx_remarks,
                                    "tx_ref_no": tx_ref_no,
                                    "tx_withdrawal": tx_withdrawal,
                                    "tx_deposit": tx_deposit,
                                    "credit_tx": credit_tx,
                                    "total_available_balance": total_available_balance,
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
                                    "credit_tx": credit_tx,
                                    "tx_amount": tx_amount,
                                    "tx_desc": tx_remarks,
                                    "total_available_balance": total_available_balance,
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
                            # Check end condition (last transaction)
                            if transaction == transactions[len(transactions) - 1]:
                                self.info("Latest transaction reached")
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
                    self.random_sleep(1.5, 4)
                    if not self.open_recent_transaction_list():
                        self.warn("Could not switch tabs")
                    self.random_sleep(1.5, 4)
        except Exception as e:
            self.error(e)
            return False

    def scrape_balance(self) -> None:
        try:
            self.update()
            self.debug("Scraping balance")
            self.random_sleep(1.5, 4)
            balance_selector = 'td.amountRightAlign'
            self.click(self.find_by_css('div.menubar #ID_DASHAT'))
            self.wait_for_element_by_css('tbody tr', timeout=30)
            self.random_sleep(1.5, 4)
            self.update()
            account_rows = self.find_by_css('tbody tr', multiple=True)
            balance_found = False
            balance = ""
            for account_row in account_rows:
                if re.search(self.account_no, account_row.text, re.M):
                    balance_element = self.find_by_css(balance_selector, parent=account_row)
                    balance = balance_element.text.replace('₹', '').replace(',', '').strip()
                    if not balance:
                        raise Exception("Could not find balance")
                    balance_found = True
                    break
            if not balance_found:
                raise Exception("Could not find balance")
            self.debug("Balance found: " + str(balance))
            self.update()
            self.update_balance(balance)
            self.debug("Balance updated")
        except Exception as e:
            self.error(e)
