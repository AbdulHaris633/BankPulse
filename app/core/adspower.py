import os
import re
import time
import subprocess
import psutil
import requests
from typing import Dict, Any, Optional

from app.core.settings import Settings
from app.services.logger import debug, info, warn, error

# Load settings once at module level
_settings = Settings()


class AdsPowerAPI:
    """
    HTTP API client for AdsPower browser profile manager.

    Key design decisions vs V1:
    - All API calls go through _get() / _post() with built-in retry logic
      and "Too many requests" handling (from V1's run() method)
    - Authorization header (Bearer token) included on every request (from V1)
    - Uses user_id param (GET) for browser start/stop — matches actual
      AdsPower API behaviour observed in V1
    - Driver connection logic moved to browser.py — this class only handles
      the AdsPower HTTP API, not Selenium

    Key design decisions vs V2's AdspowerAPI:
    - Renamed create_browser() → start_browser() to make clear we are
      starting an EXISTING persistent profile, never creating a new one
    - Added is_open(), restart(), create_profile(), delete_profile(),
      update_proxy(), update_fingerprint(), get_serial_number() from V1
    """

    def __init__(self):
        # Build base URL from settings — matches V1's local.adspower.net format
        self.base_url = f"http://local.adspower.net:{_settings.ADSPOWER_PORT}"
        # Auth header sent on every request — required by AdsPower API
        self.headers = {"Authorization": f"Bearer {_settings.ADSPOWER_API_KEY}"}
        # Number of retry attempts for each API call before giving up
        self.attempts = 5
        # AdsPower executable details — used only by restart()
        self.ads_power_exe = "AdsPower Global.exe"
        self.ads_power_dir = "C:\\Program Files\\AdsPower Global"
        self.sun_browser_exe = "SunBrowser.exe"
        # Flag to prevent double-restart if restart() is already in progress
        self.closing = False

    # INTERNAL HTTP HELPERS
    # All API calls go through these so retry + error handling is in one place

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        """
        Make a GET request to the AdsPower API with retry logic.
        Retries up to self.attempts times — on 'Too many requests' it sleeps
        5 seconds and retries. On any other API error it raises immediately.
        Returns the 'data' field of the response if present, else full response.
        """
        for i in range(self.attempts):
            try:
                resp = requests.get(
                    url=self.base_url + path,
                    params=params,
                    headers=self.headers,
                    timeout=_settings.REQUESTS_TIMEOUT
                )
            except Exception as e:
                raise Exception(f"Could not connect to AdsPower API: {e}")

            if not resp.ok:
                raise Exception(f"AdsPower API HTTP error: {resp.status_code}:{resp.text}")

            json_data = resp.json()

            if json_data.get("code") != 0:
                # Rate limit hit — sleep and retry
                if re.search(r'Too many request', json_data.get("msg", "")):
                    warn("Too many requests to AdsPower API, sleeping 5s")
                    time.sleep(5)
                    continue
                else:
                    raise Exception(f"AdsPower API error: {json_data.get('msg')}")

            # Success — return data payload if present, else full response
            return json_data.get("data", json_data)

        raise Exception("AdsPower API: exceeded max retry attempts")

    def _post(self, path: str, payload: Dict[str, Any]) -> dict:
        """
        Make a POST request to the AdsPower API with retry logic.
        Same retry behaviour as _get().
        """
        for i in range(self.attempts):
            try:
                resp = requests.post(
                    url=self.base_url + path,
                    json=payload,
                    headers=self.headers,
                    timeout=_settings.REQUESTS_TIMEOUT
                )
            except Exception as e:
                raise Exception(f"Could not connect to AdsPower API: {e}")

            if not resp.ok:
                raise Exception(f"AdsPower API HTTP error: {resp.status_code}:{resp.text}")

            json_data = resp.json()

            if json_data.get("code") != 0:
                if re.search(r'Too many request', json_data.get("msg", "")):
                    warn("Too many requests to AdsPower API, sleeping 5s")
                    time.sleep(5)
                    continue
                else:
                    raise Exception(f"AdsPower API error: {json_data.get('msg')}")

            return json_data.get("data", json_data)

        raise Exception("AdsPower API: exceeded max retry attempts")

    # -------------------------------------------------------------------------
    # BROWSER LIFECYCLE — existing persistent profiles
    # We NEVER create new profiles here. All profiles are pre-created and
    # managed externally. We only start/stop/check existing ones.
    # -------------------------------------------------------------------------

    def is_open(self, profile_id: str) -> bool:
        """
        Check if a profile's browser is currently running.
        Used before start_browser() to avoid double-opening the same profile,
        and in transaction_manager.init() to ensure a clean start.
        """
        try:
            data = self._get("/api/v1/browser/active", {"user_id": profile_id})
            return data.get("status") == "Active"
        except Exception as e:
            error(e)
            return False

    def start_browser(self, profile_id: str, clear_cache: bool = False) -> dict:
        """
        Start an existing persistent AdsPower profile browser.
        Returns the full data dict from AdsPower which contains the webdriver
        connection details used by browser.py to attach Selenium.

        clear_cache=True is used by most bank transaction managers to ensure
        no leftover cookies/sessions from previous runs interfere with login.

        NOTE: This starts an EXISTING profile — it does NOT create a new one.
        Use create_profile() to create a brand new profile.
        """
        params = {"user_id": profile_id, "open_tabs": 1}
        if clear_cache:
            # Clears browser cache when the browser is closed after this session
            params["clear_cache_after_closing"] = 1
        return self._get("/api/v1/browser/start", params)

    def stop_browser(self, profile_id: str) -> bool:
        """
        Stop a running profile browser.
        Called at the end of every transaction in TransactionManager.close()
        and in browser.py Browser.close().
        """
        try:
            self._get("/api/v1/browser/stop", {"user_id": profile_id})
            return True
        except Exception as e:
            error(e)
            return False

    # -------------------------------------------------------------------------
    # PROFILE MANAGEMENT — create / delete / update
    # Used by create_trader() (new accounts) and update_profile() (blocked
    # profiles that need to be replaced with a fresh browser fingerprint)
    # -------------------------------------------------------------------------

    def create_profile(self, profile_json: dict) -> str:
        """
        Create a brand new AdsPower browser profile.
        Returns the new profile ID string.

        Called from two places:
        1. parent.py create_trader() — when a new trader account is imported
        2. transaction/base.py update_profile() — when a profile is blocked
           and needs to be replaced with a fresh fingerprint

        profile_json example:
        {
            "name": "12345_KOTAK",
            "domain_name": "www.google.com",
            "group_id": "0",
            "fingerprint_config": { ... },
            "user_proxy_config": { ... }
        }
        """
        data = self._post("/api/v1/user/create", profile_json)
        profile_id = data.get("id")
        if not profile_id:
            raise Exception(f"Profile created but no ID returned: {data}")
        debug(f"New profile created: {profile_id}")
        return profile_id

    def delete_profile(self, profile_id: str) -> bool:
        """
        Stop and permanently delete an AdsPower profile.
        Used in update_profile() when replacing a blocked/banned profile
        with a new one to get a fresh browser fingerprint.

        Steps (from V1's standalone delete_profile() in functions.py):
        1. Stop the browser if it is running
        2. Poll /browser/active until status is no longer "Active"
           — AdsPower rejects delete requests while the browser is open
        3. Delete the profile

        V1 location: functions.py delete_profile()
        """
        try:
            # Step 1 — stop the browser (safe to call even if already stopped)
            self.stop_browser(profile_id)

            # Step 2 — wait until the browser process has fully closed
            # AdsPower needs the browser to be fully stopped before it allows deletion
            for _ in range(30):  # wait up to 30 seconds
                if not self.is_open(profile_id):
                    break
                time.sleep(1)

            # Step 3 — delete the profile
            self._post("/api/v1/user/delete", {"user_ids": [profile_id]})
            return True
        except Exception as e:
            error(e)
            return False

    def update_proxy(self, profile_id: str, proxy_config: dict) -> bool:
        """
        Update the proxy configuration for an existing profile.
        Called in TransactionManager.init() after fetching a fresh proxy
        from the proxy server before each transaction.

        proxy_config format (V1 style):
        {
            "proxy_soft": "other",
            "proxy_type": "socks5",
            "proxy_host": "1.2.3.4",
            "proxy_port": "8080",
            "proxy_user": "user",
            "proxy_password": "pass"
        }
        """
        try:
            # Strip internal ProxyManager fields (_provider, _rotate_url, etc.)
            # before sending to AdsPower — only the proxy_* keys are valid here
            clean = {k: v for k, v in proxy_config.items() if not k.startswith("_")}
            self._post("/api/v1/user/update", {
                "user_id": profile_id,
                "user_proxy_config": clean
            })
            return True
        except Exception as e:
            error(e)
            return False

    def update_fingerprint(self, profile_id: str) -> bool:
        """
        Update the browser fingerprint for a profile to randomise its
        browser identity. Used by bank transaction managers that have
        dynamic_fingerprint=True to make each session look like a
        different browser to the bank's fraud detection.
        """
        try:
            fingerprint_config = {
                "browser_kernel_config": {"version": "latest", "type": "chrome"},
                "random_ua": {"ua_system_version": ["Windows 10"]}
            }
            self._post("/api/v1/user/update", {
                "user_id": profile_id,
                "fingerprint_config": fingerprint_config
            })
            return True
        except Exception as e:
            error(e)
            return False

    def get_serial_number(self, profile_id: str) -> int:
        """
        Fetch the AdsPower serial number for a profile.
        The serial number is the internal numeric ID AdsPower assigns to each
        profile — it is used in main.py's cleanup logic to identify and kill
        the correct SunBrowser.exe process when a bot hangs.
        Returns 0 if the serial number cannot be fetched.
        """
        try:
            data = self._get("/api/v1/user/list", {"user_id": profile_id})
            profiles = data.get("list", [])
            if not profiles:
                raise Exception(f"No profile found for ID: {profile_id}")
            serial_no = int(profiles[0]["serial_number"])
            debug(f"Serial number for {profile_id}: {serial_no}")
            return serial_no
        except Exception as e:
            error(e)
            return 0

    # RESTART — emergency recovery
    # Called when AdsPower becomes unresponsive and needs a full process restart

    def restart(self) -> bool:
        """
        Force-kill all AdsPower and SunBrowser processes then relaunch AdsPower.
        This is an emergency recovery method — only called when normal API calls
        are consistently failing and a full restart is needed.

        Waits 30 seconds after launching for AdsPower to fully initialise
        before checking the status endpoint.
        """
        try:
            info("Shutting down AdsPower for restart")

            # Prevent double-restart if this is already running
            if self.closing:
                debug("AdsPower restart already in progress, skipping")
                return True
            self.closing = True

            this_cwd = os.getcwd()

            # Kill all AdsPower and SunBrowser processes
            for p in psutil.process_iter():
                if p.name() in [self.ads_power_exe, self.sun_browser_exe]:
                    p.kill()
            debug("AdsPower processes killed")

            # Launch AdsPower from its install directory
            os.chdir(self.ads_power_dir)
            debug("Launching AdsPower")
            subprocess.Popen([self.ads_power_exe])

            # Wait for AdsPower to fully start before checking API
            debug("Waiting 30s for AdsPower startup")
            time.sleep(30)
            os.chdir(this_cwd)

            # Verify the API is back online
            debug("Checking AdsPower API status")
            status_url = self.base_url + "/api/v1/status"
            resp = requests.get(url=status_url, timeout=_settings.REQUESTS_TIMEOUT)
            if not resp.ok:
                raise Exception("AdsPower API not responding after restart")
            if resp.json().get("code"):
                raise Exception(f"AdsPower API error after restart: {resp.json().get('msg')}")

            info("AdsPower API back online")
            self.closing = False
            return True
        except Exception as e:
            error(e)
            return False
