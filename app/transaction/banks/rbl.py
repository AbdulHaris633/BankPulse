import re
import json
import hashlib
import time
from multiprocessing import Queue, Value

from selenium.common.exceptions import WebDriverException

from app.transaction.banks.rbl_base import RBLBankTransactionManager
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)


class RBLTransactionManager(RBLBankTransactionManager):
    """
    Transaction manager for RBL Bank PTE netbanking portal.
    URL: https://online.rblbank.com/corp
    Institution: RBLPTE

    Inherits shared RBL utilities from RBLBankTransactionManager:
    - rbl_click(), select_date(), wait_for_loading_widget()
    - download_statement(), download_and_parse_statement()

    Operations supported:
    - login_check : username → confirm checkbox → password (two-step login)
    - sync        : DOM scraping of statementPeriod-table, amount parsed from
                    "1234.56 Cr/Dr" format, hash chaining
    - logout      : nav logout link + confirm

    open_statement flow:
    1. Click first nav link (#flexiselDemo4 > li)
    2. Click "Account Statement" dashboard button
    3. Select Current account from tbody
    4. Click advanced filters, set transaction period via JS (txn_period_options[1])
    5. Find and click Search button via span > i.btn

    Sync notes:
    - No self.update() calls inside the sync loop (V1 style — kept as-is)
    - On error in sync_transactions, calls self.logout() before returning False

    V1 location: rbl_transaction_manager.py
    V2 location: app/transaction/banks/rbl.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - Inherits from RBLBankTransactionManager (app/transaction/banks/rbl_base.py)
      instead of V1's rbl_bank_transaction_manager.RBLBankTransactionManager
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://online.rblbank.com/corp"
        self.institution_name: str = "RBLPTE"

    def login(self) -> bool:
        try:
            self.debug("Starting login")
            self.get(self.url)
            username_selector = 'div[id="LoginHDisplay.RaW10.C1"] input'
            password_selector = 'input[name="AuthenticationFG.ACCESS_CODE"]'
            confirm_checkbox_selector = 'input[name="AuthenticationFG.TARGET_CHECKBOX"] ~ span'
            login_btn_selector = 'i.login_button'
            login_success_selector = 'img.profile-picture'
            self.wait_for_element_by_css(username_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Sending username")
            self.send_keys(self.find_by_css(username_selector), self.username)
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(login_btn_selector))
            self.wait_for_element_by_css(confirm_checkbox_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(confirm_checkbox_selector))
            self.random_sleep(1.5, 4)
            self.debug("Sending password")
            self.send_keys(self.find_by_css(password_selector), self.password)
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(login_btn_selector))
            self.wait_for_element_by_css(login_success_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.info("Login success")
            return True
        except Exception as e:
            self.error("Error during login")
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            self.debug("Starting logout")
            logout_selector = 'span.logoutLink #HREF_Logout'
            confirm_logout_selector = '#span_LOG_OUT > a'
            logout_success_selector = 'div.logoutSuccessIcon'
            self.debug("Clicking logout nav button")
            self.click(self.find_by_css(logout_selector))
            self.wait_for_element_by_css(confirm_logout_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Clicking confirm logout")
            self.click(self.find_by_css(confirm_logout_selector))
            self.wait_for_element_by_css(logout_success_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.info("Logout success")
            return True
        except Exception as e:
            self.error("Error during logout")
            self.error(e)
            return False

    def open_statement(self) -> bool:
        """
        Navigate to Account Statement, select the Current account, apply
        advanced filters with transaction period set via JavaScript, then
        click Search.

        JavaScript used to set transaction period (txn_period_options[1] = selected):
            var txn_period_selector = 'select[name="TransactionHistoryFG.TXN_PERIOD"] > option';
            txn_period_options = document.querySelectorAll(txn_period_selector);
            txn_period_options.forEach((opt) => { opt.removeAttribute('selected'); });
            txn_period_options[1].setAttribute('selected', '');
        """
        nav_links_selector = '#flexiselDemo4 > li'
        dashboard_statement_btn_selector = 'a[title="Account Statement"]:not(.menu__link)'
        advanced_filters_selector = 'a[name="HREF_MoreDetails"]'
        transaction_period_selector = 'input[name="TransactionHistoryFG.SELECTED_RADIO_INDEX"][value="1"] ~ span'
        transactions_selector = 'table.statementPeriod-table tbody tr'
        try:
            self.debug("Starting open statement")
            self.wait_for_element_by_css(nav_links_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Clicking nav link")
            self.click(self.find_by_css(nav_links_selector))
            self.wait_for_element_by_css(dashboard_statement_btn_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Clicking dashboard statements button")
            self.click(self.find_by_css(dashboard_statement_btn_selector))
            self.wait_for_element_by_css('tbody > tr', timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Selecting account")
            account_rows = self.find_by_css('tbody > tr', multiple=True)
            current_account_found = False
            for account_row in account_rows:
                row_data = self.find_by_tag('td', multiple=True, parent=account_row)
                if row_data[2].text.strip() == 'Current':
                    self.click(self.find_by_tag('a', parent=row_data[0]))
                    current_account_found = True
                    break
            if not current_account_found:
                raise Exception("Could not find current account in list")
            self.wait_for_element_by_css(advanced_filters_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Selecting advanced filters")
            self.click(self.find_by_css(advanced_filters_selector))
            self.random_sleep(1.5, 4)
            self.debug("Setting transaction period")
            self.click(self.find_by_css(transaction_period_selector))
            self.random_sleep(1.5, 4)
            set_txn_period_script = """
                var txn_period_selector = 'select[name="TransactionHistoryFG.TXN_PERIOD"] > option';
                txn_period_options = document.querySelectorAll(txn_period_selector);
                txn_period_options.forEach((txn_period_option) => {
                    txn_period_option.removeAttribute('selected');
                });
                txn_period_options[1].setAttribute('selected', '');
            """
            self.driver.execute_script(set_txn_period_script)
            self.random_sleep(1.5, 4)
            self.debug("Clicking search button")
            ui_btns = self.find_by_css('span > i.btn', multiple=True)
            search_btn_clicked = False
            for ui_btn in ui_btns:
                if self.find_by_css('input[value="Search"]', parent=ui_btn):
                    self.click(ui_btn)
                    search_btn_clicked = True
                    break
            if not search_btn_clicked:
                raise Exception("Could not find search button")
            self.wait_for_element_by_css(transactions_selector, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Statement opened")
            return True
        except Exception as e:
            self.error("Error opening statement")
            self.error(e)
            return False

    def sync_transactions(self) -> bool:
        try:
            self.info("Starting transaction sync")
            transactions_selector = 'table.statementPeriod-table tbody tr'
            webdriver_errors = 0
            while True:
                try:
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
                            raise Exception("Could not logout of RBL, exiting sync")
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True
                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]
                    while not latest_tx_found:
                        self.debug("Starting transaction search")
                        if not self.open_statement():
                            raise WebDriverException("Could not open transaction list")

                        # Loop through all transactions
                        transactions = self.find_by_css(transactions_selector, multiple=True)
                        for transaction in transactions:
                            try:
                                table_data = self.find_by_css('td', multiple=True, parent=transaction)
                                tx_date = table_data[0].text.strip()
                                remarks = table_data[1].text.strip()
                                ref_no = table_data[2].text.strip()
                                tx_value_date = table_data[3].text.strip()
                                tx_amount_text = table_data[4].text.strip()
                                tx_amount_match = re.match(r'^\s*([0-9,.]+)\s*(Cr|Dr)\s*$', tx_amount_text)
                                if not tx_amount_match or not tx_amount_match.lastindex:
                                    raise Exception("TX amount regex does not match: " + tx_amount_text)
                                tx_amount = tx_amount_match.group(1).replace(',', '').strip()
                                tx_type = tx_amount_match.group(2).strip()
                                if tx_type == 'Dr':
                                    credit_tx = 0
                                else:
                                    credit_tx = 1
                                total_available_balance = table_data[5].text.replace(',', '').strip()
                                tx_info = {
                                    "tx_date": tx_date,
                                    "remarks": remarks,
                                    "ref_no": ref_no,
                                    "tx_value_date": tx_value_date,
                                    "tx_amount": tx_amount,
                                    "credit_tx": credit_tx,
                                    "total_available_balance": total_available_balance,
                                }
                                tx_info_hash_str = json.dumps(tx_info, separators=(',', ':'))
                                tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()
                                if tx_hash == sync_mode_command["latest_tx_hash"]:
                                    self.info("Latest tx_hash found: " + tx_hash)
                                    latest_tx_found = True
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
                                    "tx_desc": remarks,
                                    "total_available_balance": total_available_balance,
                                    "previous_hash": "",
                                    "tx_info": tx_info,
                                    "tx_hash": tx_hash,
                                    "new_state_hash": "",
                                }
                                self.info("New transaction: " + str(transaction_json))
                                new_transactions.append(transaction_json)
                                if transaction == transactions[len(transactions) - 1]:
                                    self.info("Last transaction in the list, exiting")
                                    latest_tx_found = True
                            except Exception as e:
                                self.error(e)
                                self.error("Error parsing transaction")

                    # Send new transactions to server with hash chaining
                    if new_transactions:
                        self.info("New transactions found")
                        new_transactions.reverse()
                        for transaction in new_transactions:
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
                    if not self.open_statement():
                        self.warn("Could not switch tabs")
                    self.random_sleep(1.5, 4)
        except Exception as e:
            self.error(e)
            self.logout()
            return False
