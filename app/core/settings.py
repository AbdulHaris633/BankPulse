from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Central configuration class for the entire application.
    All values are loaded from the .env file in the project root.
    """

    # ADSPOWER
    ADSPOWER_API_KEY: str
    ADSPOWER_PORT: int = 50325

    # CAPTCHA SOLVERS
    # The bot uses two captcha services. 2Captcha is tried first.
    # AntiCaptcha is the fallback if 2Captcha fails.
    TWOCAPTCHA_API_KEY: str = ""
    ANTICAPTCHA_API_KEY: str = ""

    # COMMAND SERVER
    # The remote server that sends instructions to this bot and receives reports.
    # All endpoint URLs are built from this base URL.
    COMMAND_BASE_URL: str = "https://server1.tradingbot.top/"


    # APP BEHAVIOUR FLAGS
    # SERVER_ID      — identifies which machine sent a log (useful when running
    #                  multiple bot instances across servers)
    # PAYOUT_SERVER  — when True, uses payout-specific server endpoints instead
    #                  of the default sync/login endpoints
    # DEBUG_APP      — when True, prints logs to console only (no Datadog).
    #                  Always set True during development/testing.
    # TEST_SERVER    — when True, uses test sync endpoints on the command server
    # VERBOSE        — when True, DEBUG level messages are included in logs
    # REMOTE_LOGGING — when True, logs are shipped to Datadog. Should only be
    #                  True in production (requires DEBUG_APP=False)
    SERVER_ID: int = 0
    PAYOUT_SERVER: bool = False
    DEBUG_APP: bool = True
    TEST_SERVER: bool = False
    VERBOSE: bool = True
    REMOTE_LOGGING: bool = True

    # REQUEST TIMEOUT
    # Applied to every outbound HTTP request made by the bot (proxy fetch,
    # server reporting, screenshot upload, OTP requests, etc.)
    REQUESTS_TIMEOUT: int = 30

    # APPLICATION NAME
    # Used as a tag in Datadog logs to identify this application.
    APP_NAME: str = "BP2PB"

    # DATADOG LOGGING
    # DD_API_KEY is required only when REMOTE_LOGGING=True.
    # Logs are batched and shipped to Datadog's HTTP intake endpoint.
    DD_API_KEY: str = ""

    # MYSQL DATABASE
    # Used to store browser info (profile ID, last run time, last failed reason)
    # and to save command history for auditing.
    MYSQL_HOST: str = ""
    MYSQL_PORT: int = 3306
    MYSQL_DB_NAME: str = ""
    MYSQL_USER: str = ""
    MYSQL_PASS: str = ""

    # GOLOGIN (OPTIONAL)
    # GoLogin is an alternative browser profile manager. Only enabled if
    # ENABLE_GOLOGIN=True. When enabled, both AdsPower and GoLogin profiles
    # are created together for each trader.
    ENABLE_GOLOGIN: bool = False
    GL_TOKEN: str = ""

    # PROXY PROVIDERS
    # Credentials are obtained from each provider's dashboard and set in .env.
    # Leave HOST blank to disable a provider — ProxyManager skips it automatically.
    # Rotation URL is called after each transaction to get a fresh IP.
    # Bright Data rotates via session ID in username (no rotation URL needed).
    #
    # PROXY_ROTATION_DELAY — seconds to wait after calling a provider's rotation
    # URL before the next transaction starts. Providers need time to assign the
    # new IP. Too short = old IP reused or broken connection. Recommended: 5-15s.
    # iProxy (physical phone) may need up to 15s. Default 5s suits most providers.
    PROXY_ROTATION_DELAY: int = 5

    # PROXY_CONNECT_TIMEOUT — seconds to wait to establish a connection through
    # the proxy. Short value intentional — a dead proxy should fail fast.
    PROXY_CONNECT_TIMEOUT: int = 5

    # PROXY_READ_TIMEOUT — seconds to wait for the test server to respond after
    # connecting. generate_204 responds instantly so 10s is very generous.
    PROXY_READ_TIMEOUT: int = 10

    # PROXY_COOLDOWN_SECONDS — how long a failed provider is skipped before
    # being retried. Prevents hammering a dead provider on every transaction.
    # During cooldown the provider is excluded from round-robin entirely.
    # Default 60s. Increase if a provider is flaky and recovers slowly.
    PROXY_COOLDOWN_SECONDS: int = 60

    # Kookeey — https://www.kookeey.net/
    KOOKEEY_HOST: str = ""
    KOOKEEY_PORT: str = ""
    KOOKEEY_USER: str = ""
    KOOKEEY_PASS: str = ""
    KOOKEEY_ROTATE_URL: str = ""  # Dashboard → API → Change IP URL

    # iProxy — https://iproxy.online/
    # Physical Android phone + SIM card proxy. Rotation URL is provided per
    # connection in the iProxy dashboard under each proxy's settings.
    IPROXY_HOST: str = ""
    IPROXY_PORT: str = ""
    IPROXY_USER: str = ""
    IPROXY_PASS: str = ""
    IPROXY_ROTATE_URL: str = ""  # Dashboard → Connection → Change IP URL

    # AstroProxy — https://astroproxy.com/
    ASTROPROXY_HOST: str = ""
    ASTROPROXY_PORT: str = ""
    ASTROPROXY_USER: str = ""
    ASTROPROXY_PASS: str = ""
    ASTROPROXY_ROTATE_URL: str = ""  # Dashboard → Proxy List → Rotation URL

    # Bright Data — https://brightdata.com/
    # Rotation is done by changing the session ID embedded in the username.
    # Format: BRIGHTDATA_USER should be the base username without session suffix.
    # ProxyManager appends -session-RANDOM to generate a new IP each time.
    BRIGHTDATA_HOST: str = ""
    BRIGHTDATA_PORT: str = ""
    BRIGHTDATA_USER: str = ""  # e.g. "brd-customer-XXXX-zone-mobile_india"
    BRIGHTDATA_PASS: str = ""

    # QuarkIP — https://www.quarkip.com/
    QUARKIP_HOST: str = ""
    QUARKIP_PORT: str = ""
    QUARKIP_USER: str = ""
    QUARKIP_PASS: str = ""
    QUARKIP_ROTATE_URL: str = ""  # Dashboard → API → Change IP URL

    # Proxy302 — https://proxy302.apifox.cn/
    # Static rotating proxy — credentials are fixed and taken from the dashboard.
    # IP rotates automatically on every new connection through the proxy.
    # PROXY302_ROTATE_URL — leave blank if no rotation URL is provided.
    PROXY302_HOST: str = ""
    PROXY302_PORT: str = ""
    PROXY302_USER: str = ""
    PROXY302_PASS: str = ""
    PROXY302_ROTATE_URL: str = ""

    # COMPUTED PATHS
    # These are derived at runtime — not stored in .env.
    # DOWNLOAD_DIR is where the browser saves downloaded bank statements.
    @property
    def DOWNLOAD_DIR(self) -> str:
        return str(Path.home() / "Downloads")

    # COMPUTED SERVER URLS
    # Built from COMMAND_BASE_URL + PAYOUT_SERVER flag.
    # When PAYOUT_SERVER=True, different endpoints are used so that payout
    # instructions and sync instructions don't mix on the same queue.
    @property
    def PARENT_INSTRUCTIONS_URL(self) -> str:
        if self.PAYOUT_SERVER:
            return self.COMMAND_BASE_URL + "payout_parent_instructions.php"
        return self.COMMAND_BASE_URL + "parent_instructions.php"

    @property
    def CONFIRM_INSTRUCTION_URL(self) -> str:
        if self.PAYOUT_SERVER:
            return self.COMMAND_BASE_URL + "payout_confirm_parent_action.php?instruction_id={}"
        return self.COMMAND_BASE_URL + "confirm_parent_action.php?instruction_id={}"

    @property
    def PARENT_REPORT_URL(self) -> str:
        if self.PAYOUT_SERVER:
            return self.COMMAND_BASE_URL + "payout_parent_report.php"
        return self.COMMAND_BASE_URL + "parent_report.php"

    @property
    def CHILD_REPORT_URL(self) -> str:
        return self.COMMAND_BASE_URL + "child_report.php"

    @property
    def SYNC_MODE_URL(self) -> str:
        # When TEST_SERVER=True, uses a separate test endpoint so live sync
        # data is not affected during testing
        if self.TEST_SERVER:
            return self.COMMAND_BASE_URL + "test_sync_mode.php"
        return self.COMMAND_BASE_URL + "sync_mode.php"

    @property
    def SYNC_REPORT_URL(self) -> str:
        if self.TEST_SERVER:
            return self.COMMAND_BASE_URL + "test_sync_report.php"
        return self.COMMAND_BASE_URL + "sync_report.php"

    @property
    def SYNC_BALANCE_URL(self) -> str:
        return self.COMMAND_BASE_URL + "sync_balance.php"

    @property
    def GET_OTP_URL(self) -> str:
        # Payout operations use a separate OTP endpoint so that OTP codes
        # for payouts don't collide with OTP codes for sync/login
        if self.PAYOUT_SERVER:
            return self.COMMAND_BASE_URL + "get_payout_sms_otp.php"
        return self.COMMAND_BASE_URL + "get_sms_otp.php"

    @property
    def UPDATE_PROFILE_URL(self) -> str:
        return self.COMMAND_BASE_URL + "profile_update_report.php"

    @property
    def UPLOAD_SCREENSHOT_URL(self) -> str:
        # Screenshot upload endpoint — on a separate server (server2) from
        # the command server. Used by TransactionManager.screenshot() to
        # upload base64 images on error conditions.
        return "https://server2.tradingbot.top/upload.php"

    # ANTICAPTCHA API URLS
    # Separate report endpoints per captcha type (unlike 2Captcha which uses one).
    @property
    def ANTICAPTCHA_BASE_URL(self) -> str:
        return "https://api.anti-captcha.com"

    @property
    def ANTICAPTCHA_CREATE_TASK_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/createTask"

    @property
    def ANTICAPTCHA_GET_RESULT_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/getTaskResult"

    @property
    def ANTICAPTCHA_GET_BALANCE_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/getBalance"

    @property
    def ANTICAPTCHA_REPORT_INCORRECT_IMAGE_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/reportIncorrectImageCaptcha"

    @property
    def ANTICAPTCHA_REPORT_INCORRECT_RECAPTCHA_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/reportIncorrectRecaptcha"

    @property
    def ANTICAPTCHA_REPORT_CORRECT_RECAPTCHA_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/reportCorrectRecaptcha"

    @property
    def ANTICAPTCHA_QUEUE_STATS_URL(self) -> str:
        return f"{self.ANTICAPTCHA_BASE_URL}/getQueueStats"

    # 2CAPTCHA API URLS
    # All endpoints use the same base. TwoCaptchaClient builds full URLs from
    # TWOCAPTCHA_BASE_URL + the endpoint name (e.g. /createTask, /getBalance).
    @property
    def TWOCAPTCHA_BASE_URL(self) -> str:
        return "https://api.2captcha.com"

    @property
    def TWOCAPTCHA_CREATE_TASK_URL(self) -> str:
        return f"{self.TWOCAPTCHA_BASE_URL}/createTask"

    @property
    def TWOCAPTCHA_GET_RESULT_URL(self) -> str:
        return f"{self.TWOCAPTCHA_BASE_URL}/getTaskResult"

    @property
    def TWOCAPTCHA_GET_BALANCE_URL(self) -> str:
        return f"{self.TWOCAPTCHA_BASE_URL}/getBalance"

    @property
    def TWOCAPTCHA_REPORT_INCORRECT_URL(self) -> str:
        return f"{self.TWOCAPTCHA_BASE_URL}/reportIncorrect"

    @property
    def TWOCAPTCHA_REPORT_CORRECT_URL(self) -> str:
        return f"{self.TWOCAPTCHA_BASE_URL}/reportCorrect"

    class Config:
        # Resolve .env relative to the project root (two levels up from this file)
        # so Settings() works regardless of the working directory
        env_file = str(Path(__file__).parent.parent.parent / ".env")
        env_file_encoding = "utf-8"
