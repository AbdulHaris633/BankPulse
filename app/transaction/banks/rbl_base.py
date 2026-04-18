import time
from datetime import datetime, timedelta
from multiprocessing import Queue, Value

from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager


class RBLBankTransactionManager(TransactionManager):
    """
    Shared base class for all RBL Bank transaction managers.
    Provides utility methods used by RBL, RBL_SP, and RBL_CORPORATE.

    Subclasses:
    - RBLTransactionManager      (app/transaction/banks/rbl.py)
    - RBLSPTransactionManager    (app/transaction/banks/rbl_sp.py)
    - RBLCorporateTransactionManager (app/transaction/banks/rbl_corporate.py)

    Shared utilities:
    - rbl_click()               : click while waiting for blockUI overlay to clear
    - select_date()             : set date range filter on statement page
    - wait_for_loading_widget() : wait for loading spinner to disappear
    - download_statement()      : download XLS statement
    - download_and_parse_statement() : template method (calls download + parse)
    - parse_statement()         : stub — returns [] — overridden in subclasses

    V1 location: rbl_bank_transaction_manager.py
    V2 location: app/transaction/banks/rbl_base.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - All shared logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)

    def rbl_click(self, click_element: str, next_element: str, parent_element=None) -> None:
        """
        Click an element while waiting for blockUI overlay to clear before and
        after the click. Raises WebDriverException if the overlay persists.
        Used throughout RBL flows to handle the bank's SPA loading states.
        """
        block_ui_element = 'div.blockUI.blockOverlay'
        self.debug("Clicking element: " + click_element)
        self.maximize_window()
        ui_block_exists = True
        for i in range(0, 5):
            if self.find_by_css(block_ui_element):
                self.debug("UI blocking element exists, waiting")
                self.random_sleep(5, 8)
            else:
                ui_block_exists = False
                break
        if ui_block_exists:
            raise WebDriverException("UI block exists after 5 checks")
        self.click(self.find_by_css(click_element, parent=parent_element))
        self.debug("Element clicked: " + click_element)
        self.random_sleep(1.5, 4)
        ui_block_exists = True
        for i in range(0, 12):
            if self.find_by_css(block_ui_element):
                self.debug("UI blocking element exists, waiting")
                self.random_sleep(5, 8)
            else:
                ui_block_exists = False
                break
        if ui_block_exists:
            raise WebDriverException("UI block exists after 5 checks")
        self.debug("Element clicked, waiting for next element: " + next_element)
        self.wait_for_element_by_css(next_element, timeout=30)
        self.random_sleep(1.5, 4)
        self.debug("Element click success: " + click_element)

    def select_date(self, start_offset: int = 0) -> bool:
        """
        Select the date range on the statement filter.
        start_offset: days back from today for the from-date.
        If is_interday_statement_time() is True, from-date is set to yesterday
        regardless of start_offset (catches overnight transactions).
        """
        try:
            tx_date_checkbox = 'input[title="Select Transaction Date"] ~ span'
            date_inputs_selector = 'input[data-febatype="FEBADate"]'
            self.debug("Selecting date")
            self.maximize_window()
            self.rbl_click(tx_date_checkbox, date_inputs_selector)
            date_inputs = self.find_by_css(date_inputs_selector, multiple=True)
            from_date_input = date_inputs[0]
            to_date_input = date_inputs[1]
            self.update()
            now = datetime.now()
            day_delta = timedelta(days=1)
            start_offset_delta = timedelta(days=start_offset)
            today = now.strftime('%d/%m/%Y')
            yesterday = (now - day_delta).strftime('%d/%m/%Y')
            start_offset_day = (now - start_offset_delta).strftime('%d/%m/%Y')
            start_date = start_offset_day
            end_date = today
            if self.is_interday_statement_time():
                self.debug("Inter-day statement time, start date: " + start_date)
                start_date = yesterday
            start_date_script = "arguments[0].value = '{}';".format(start_date)
            end_date_script = "arguments[0].value = '{}';".format(end_date)
            self.driver.execute_script(start_date_script, from_date_input)
            self.random_sleep(1.5, 4)
            self.driver.execute_script(end_date_script, to_date_input)
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def wait_for_loading_widget(self) -> None:
        """
        Wait for any visible loading spinner (img[src*="widget-loading"]) to
        disappear. Polls up to 3 × medium_sleep per widget. Silent on error.
        """
        try:
            loading_widget_selector = 'img[src*="widget-loading"]'
            self.debug("Checking for loading widget")
            loading_widgets = self.find_by_css(loading_widget_selector, multiple=True)
            if loading_widgets:
                self.debug("Loading widgets found")
            no_widgets_displayed = False
            for loading_widget in loading_widgets:
                if loading_widget.is_displayed():
                    self.debug("Loading widget is displayed")
                    for i in range(0, 3):
                        self.debug("Waiting for loading widget")
                        self.random_sleep(5, 8)
                        try:
                            if not loading_widget.is_displayed():
                                no_widgets_displayed = True
                                break
                        except Exception:
                            no_widgets_displayed = True
                            break
                if no_widgets_displayed:
                    break
            self.debug("No more loading widgets found")
        except Exception:
            pass

    def download_statement(self) -> bool:
        try:
            self.debug("Downloading statement")
            self.update()
            self.random_sleep(1.5, 4)
            xls_statement = 'input[title="Download as XLS"]'
            self.click(self.find_by_css(xls_statement))
            self.random_sleep(5, 8)
            for i in range(0, 3):
                self.update()
                self.random_sleep(5, 8)
                if self.get_most_recent_download():
                    break
            self.debug("Statement downloaded")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """Stub — overridden in each RBL subclass."""
        return []

    def download_and_parse_statement(self) -> list:
        try:
            if not self.download_statement():
                raise Exception("Could not download statement")
            return self.parse_statement()
        except Exception as e:
            self.error(e)
            return []
