import os
import re
import time
import random
from typing import Optional, Any

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver

from app.core.adspower import AdsPowerAPI
from app.services.logger import debug, error, warn


class Browser:
    """
    Base class for all bank transaction bots. Every bank-specific
    TransactionManager extends this class.

    Responsibilities:
    - Connect to an existing AdsPower persistent profile via Selenium
    - Provide reusable element interaction methods (find, click, type, sleep)
    - Manage browser lifecycle (open, close, clean windows)

    What this class does NOT do:
    - Bank login logic (handled by each bank's TransactionManager subclass)
    - Captcha solving (handled by TransactionManager.solve())
    - Server reporting (handled by TransactionManager)
    - Proxy fetching (handled by functions.py get_p2p_proxy())

    Merged from:
    - V2's Browser class (element interaction methods, clean structure)
    - V1's TransactionManager.get_ads_power_driver() (full webdriver connection
      logic including chromedriver path detection and Chrome options)
    - V1's TransactionManager.clean_windows() (extra tab cleanup)

    V1 location: transaction_manager.py (mixed with domain logic)
    V2 location: app/core/browser.py (isolated infrastructure layer)
    """

    def __init__(self, api: AdsPowerAPI):
        # AdsPower API client — used to start/stop browser profiles
        self.api: AdsPowerAPI = api
        # Active profile ID — set in open(), cleared in close()
        self.profile_id: Optional[str] = None
        # Selenium WebDriver instance — None until open() is called
        self.driver: Optional[WebDriver] = None
        # Raw response from AdsPower start_browser() — stored for debugging
        self._start_resp: Optional[dict] = None
        # Handle of the main browser window — used by clean_windows() to
        # identify and close any extra tabs that open during bank operations
        self.main_window_handle: str = ""

    # BROWSER LIFECYCLE

    def open(self, profile_id: str, clear_cache: bool = False, wait: float = 2.0) -> bool:
        """
        Start an existing AdsPower persistent profile and attach Selenium to it.

        Steps:
        1. Check if the profile is already open — if so, close it first to
           ensure a clean state (V1 behaviour: raise if already open, but
           closing first is safer for production restarts)
        2. Call AdsPower API to start the browser profile
        3. Parse the response to find the webdriver connection details
        4. Find the chromedriver executable path from the webdriver path
        5. Connect Selenium with full Chrome options from V1
        6. Maximise window and switch to first tab

        profile_id  — AdsPower profile ID (bot_id from command)
        clear_cache — when True, clears browser cache on close. Used by most
                      banks to prevent stale session cookies from previous runs
                      interfering with the new login.
        wait        — seconds to sleep after connecting (allows page to settle)

        Returns True on success, raises Exception on failure.
        """
        try:
            self.profile_id = profile_id
            debug(f"Opening browser for profile: {profile_id}")

            # If the browser is already open, close it first for a clean start.
            # V1 raised an exception here — closing first is more resilient
            # since a previous crashed process may have left it open.
            if self.api.is_open(profile_id):
                debug(f"Profile {profile_id} already open, closing first")
                self.api.stop_browser(profile_id)
                time.sleep(2)

            # Start the AdsPower browser profile
            resp = self.api.start_browser(profile_id, clear_cache=clear_cache)
            self._start_resp = resp

            # WEBDRIVER CONNECTION
            # AdsPower returns webdriver connection info in different keys
            # depending on the version. We try all known key names.
            # V1 primarily uses ws.selenium — that is checked first.

            ws_selenium = resp.get("ws", {}).get("selenium", "")

            # Webdriver executable path — used to locate chromedriver
            webdriver_path = resp.get("webdriver") or resp.get("webDriver", "")

            if not ws_selenium and not webdriver_path:
                raise Exception(f"No webdriver info in AdsPower response: {resp}")

            
            # CHROMEDRIVER PATH DETECTION (from V1 get_ads_power_driver)
            # AdsPower stores the chromedriver alongside the Chrome binary in
            # a versioned folder. We extract the folder path from the webdriver
            # path and look for chromedriver.exe or driver.exe inside it.
            chrome_path = self._find_chromedriver(webdriver_path)

            # CHROME OPTIONS (from V1 get_ads_power_driver)
            # These match the exact options V1 uses — required for stable
            # operation with AdsPower's browser profiles.
            # debuggerAddress connects Selenium to the already-running Chrome
            # process that AdsPower launched (remote debugging attachment).

            chrome_options = Options()
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_argument("enable-automation")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--dns-prefetch-disable")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--page-load-strategy=normal")
            # This is the key option — attaches Selenium to the AdsPower
            # Chrome process via Chrome DevTools Protocol (CDP)
            chrome_options.add_experimental_option("debuggerAddress", ws_selenium)

            # Connect Selenium using the chromedriver found above
            service = Service(executable_path=chrome_path)
            self.driver = webdriver.Chrome(service=service, options=chrome_options)

            # Maximise and focus the first tab
            self.driver.maximize_window()
            self.driver.switch_to.window(self.driver.window_handles[0])

            if wait:
                time.sleep(wait)

            debug(f"Browser opened successfully for profile: {profile_id}")
            return True

        except Exception as e:
            error(e)
            raise

    def close(self) -> bool:
        """
        Quit the Selenium driver and stop the AdsPower browser profile.
        Called in TransactionManager.close() at the end of every transaction.
        Safe to call even if the driver was never opened — checks before acting.
        """
        try:
            # Quit Selenium driver first (disconnects CDP session)
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None

            # Stop the AdsPower browser process
            if self.profile_id:
                self.api.stop_browser(self.profile_id)
                self.profile_id = None

            return True
        except Exception as e:
            error(e)
            return False

    def clean_windows(self) -> bool:
        """
        Close all extra browser tabs, keeping only the main window.

        Some bank portals open popups or new tabs during login/transactions.
        These extra tabs can break automation if left open (wrong window
        context). This method closes all tabs except the first one and
        switches focus back to the main window.

        Called in TransactionManager.init() right after opening the browser,
        and can be called again after operations that might open popups.

        V1 location: TransactionManager.clean_windows()
        """
        try:
            # Track the main window on first call
            if not self.main_window_handle:
                self.main_window_handle = self.driver.window_handles[0]

            # Close all tabs that are not the main window
            for window_handle in self.driver.window_handles:
                if window_handle != self.main_window_handle:
                    self.driver.switch_to.window(window_handle)
                    self.driver.close()
                    # Small pause between closing tabs to avoid race conditions
                    time.sleep(random.uniform(1.5, 4))

            # Switch focus back to the main window
            self.driver.switch_to.window(self.main_window_handle)
            return True
        except Exception as e:
            error(e)
            return False

    # INTERNAL HELPER

    def _find_chromedriver(self, webdriver_path: str) -> str:
        """
        Locate the chromedriver executable from the webdriver path returned
        by AdsPower. AdsPower stores Chrome and its driver in a versioned
        folder matching this pattern:
            ...\\cwd_global\\chrome_<version>\\chromedriver.exe
            or
            ...\\cwd_global\\chrome_<version>\\driver.exe

        Raises Exception if neither executable is found.

        V1 location: TransactionManager.get_ads_power_driver() (inline logic)
        """
        if not webdriver_path:
            raise Exception("No webdriver path provided by AdsPower")

        match = re.match(r'^(.+\\cwd_global\\chrome_\d+\\).+$', webdriver_path, re.I)
        if not match:
            raise Exception(f"Could not parse Chrome path from webdriver: {webdriver_path}")

        base_dir = match.group(1)

        # Try chromedriver.exe first, then driver.exe
        for driver_name in ["chromedriver.exe", "driver.exe"]:
            candidate = os.path.join(base_dir, driver_name)
            if os.path.isfile(candidate):
                return candidate

        raise Exception(f"chromedriver not found in: {base_dir}")

    def _ensure_driver(self):
        """
        Guard method — raises if Selenium driver is not connected.
        Called at the start of every element interaction method to give a
        clear error if open() was not called first.
        """
        if not self.driver:
            raise RuntimeError("WebDriver not connected — call open() first")

    # ELEMENT INTERACTION METHODS (from V2 Browser)
    # Used by TransactionManager and all bank subclasses instead of calling
    # self.driver.find_element() directly — centralises timeout and error logic

    def wait_for_element_by_css(self, selector: str, timeout: float = 5.0, multiple: bool = False, poll: float = 0.2) -> Any:
        """
        Wait for element(s) matching a CSS selector to appear in the DOM.

        Polls every `poll` seconds until an element is found or `timeout`
        is reached. Returns None (or []) on timeout instead of raising —
        callers decide whether to raise based on their context.

        selector — CSS selector string (e.g. "#login-btn", ".error-msg")
        timeout  — max seconds to wait (default 5s)
        multiple — if True returns list of all matches, else first match only
        poll     — seconds between each DOM check (default 0.2s)

        Used extensively in all bank login and transaction flows.
        """
        self._ensure_driver()
        end = time.time() + timeout
        while time.time() < end:
            elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elems:
                return elems if multiple else elems[0]
            time.sleep(poll)
        return [] if multiple else None

    def find_by_css(self, selector: str, multiple: bool = False, timeout: float = 0.0, parent=None) -> Any:
        """
        Shorthand for wait_for_element_by_css().
        timeout=0 means check once immediately with no waiting.
        parent — search within a specific WebElement instead of self.driver.
                 When parent is set, timeout is ignored (single immediate check).
        """
        if parent is not None:
            elems = parent.find_elements(By.CSS_SELECTOR, selector)
            return elems if multiple else (elems[0] if elems else None)
        return self.wait_for_element_by_css(selector, timeout, multiple)

    def find_by_id(self, element_id: str, timeout: float = 0.0) -> Any:
        """
        Find a single element by its HTML id attribute.
        Equivalent to driver.find_element(By.ID, element_id).
        timeout=0 means check once immediately with no waiting.
        """
        self._ensure_driver()
        end = time.time() + timeout if timeout else None
        while True:
            elems = self.driver.find_elements(By.ID, element_id)
            if elems:
                return elems[0]
            if not end or time.time() >= end:
                return None
            time.sleep(0.2)

    def find_by_tag(self, tag: str, multiple: bool = False, parent=None) -> Any:
        """
        Find element(s) by HTML tag name.
        Equivalent to find_elements(By.TAG_NAME, tag).
        parent — search within a specific WebElement instead of self.driver.
        multiple=True returns all matches, False returns the first only.
        """
        self._ensure_driver()
        root = parent if parent is not None else self.driver
        elems = root.find_elements(By.TAG_NAME, tag)
        return elems if multiple else (elems[0] if elems else None)

    def find_by_xpath(self, xpath: str, multiple: bool = False, timeout: float = 0.0) -> Any:
        """
        Find element(s) by XPath with optional timeout.
        Some bank portals have deeply nested elements that are easier to
        target with XPath than CSS selectors.
        """
        self._ensure_driver()
        if timeout:
            end = time.time() + timeout
            while time.time() < end:
                elems = self.driver.find_elements(By.XPATH, xpath)
                if elems:
                    return elems if multiple else elems[0]
                time.sleep(0.1)
            return [] if multiple else None
        elems = self.driver.find_elements(By.XPATH, xpath)
        return elems if multiple else (elems[0] if elems else None)

    def click(self, element) -> None:
        """
        Click a web element.
        Use this instead of element.click() directly so all clicks go through
        one place — easier to add JS fallback click if needed in future.
        """
        self._ensure_driver()
        element.click()

    def send_keys(self, element, keys: str) -> None:
        """
        Send keystrokes to a web element (type into input field).
        Use this instead of element.send_keys() directly.
        For human-like typing with random delays use human_type() in
        TransactionManager instead.
        """
        self._ensure_driver()
        element.send_keys(keys)

    def get(self, url: str, wait: float = 0.0) -> None:
        """
        Navigate the browser to a URL.
        Optional wait gives the page time to load before the next action.
        """
        self._ensure_driver()
        self.driver.get(url)
        if wait:
            time.sleep(wait)

    def click_displayed(self, selector: str) -> None:
        """
        Find all elements matching a CSS selector and click the first one
        that is currently visible on the page.

        Used for buttons that may be present in the DOM but hidden (e.g.
        modal close buttons, multiple submit buttons where only one is shown).
        Includes small sleep before and after for stability.

        V1 location: TransactionManager.click_displayed()
        """
        self._ensure_driver()
        time.sleep(random.uniform(1.5, 4))
        btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
        for btn in btns:
            if btn.is_displayed():
                btn.click()
                break
        time.sleep(random.uniform(1.5, 4))

    def save_screenshot(self, path: str) -> str:
        """
        Save a screenshot of the current browser state to a file.
        Returns the file path on success, raises on failure.

        Note: TransactionManager.screenshot() uses get_screenshot_as_base64()
        instead of this method (to upload directly to server without saving
        to disk). This method is available as a lower-level utility for
        debugging or when a file path is needed.

        V2 location: app/core/browser.py
        """
        self._ensure_driver()
        ok = self.driver.save_screenshot(path)
        if not ok:
            raise RuntimeError(f"Failed to save screenshot to: {path}")
        return path

    def random_sleep(self, a: float = 0.5, b: float = 2.0) -> float:
        """
        Sleep for a random duration between a and b seconds.
        Used to simulate human-like pauses between actions so the bank portal
        does not detect automation from perfectly timed requests.
        Returns the actual sleep duration.

        V1 equivalent: small_sleep() = uniform(1.5, 4) / medium_sleep() = randint(5,8)
        Call as: self.random_sleep(1.5, 4) for small, self.random_sleep(5, 8) for medium
        """
        duration = random.uniform(a, b)
        time.sleep(duration)
        return duration
