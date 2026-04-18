import base64
import time
import requests
from typing import Optional, Dict, Any

from app.core.settings import Settings

_settings = Settings()


class AntiCaptchaClient:
    """
    Custom AntiCaptcha API client using direct JSON API calls.

    Replaces the anticaptchaofficial pip package used in V1.

    V1 used:
        from anticaptchaofficial.imagecaptcha import imagecaptcha
        solver = imagecaptcha()
        solver.set_key(api_key)
        captcha_text = solver.solve_and_return_solution(filename)

    V2 uses (this class):
        solver = AntiCaptchaClient(api_key)
        result = solver.solve_image(image_path)
        text = result['solution']['text']

    Role: fallback solver — only called when 2Captcha fails.
    Full fallback logic lives in TransactionManager.solve() in base.py.

    Note: AntiCaptcha has separate report endpoints for image vs reCAPTCHA,
    unlike 2Captcha which uses one endpoint for all types.

    All endpoint URLs are loaded from Settings (settings.py).

    References:
        https://anti-captcha.com/apidoc/methods/createTask
        https://anti-captcha.com/apidoc/task-types/ImageToTextTask
    """

    def __init__(
        self,
        api_key: str,
        polling_interval: float = 5.0,
        timeout: float = 120.0
    ):
        """
        api_key          — AntiCaptcha API key from settings.ANTICAPTCHA_API_KEY
        polling_interval — seconds to wait between polls for the solved result
        timeout          — total seconds to wait before giving up on a captcha
        """
        self.api_key = api_key
        self.polling_interval = polling_interval
        self.timeout = timeout

    def _create_task(self, task: Dict[str, Any]) -> str:
        """
        Submit a task to AntiCaptcha and return the taskId.
        Raises on API error (errorId != 0).
        """
        payload = {"clientKey": self.api_key, "task": task}
        r = requests.post(_settings.ANTICAPTCHA_CREATE_TASK_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("errorId", 1) != 0:
            raise RuntimeError(f"AntiCaptcha createTask error: {data.get('errorDescription', data)}")

        return str(data["taskId"])

    def _poll_result(self, task_id: str) -> Dict[str, Any]:
        """
        Poll getTaskResult until status == 'ready' or timeout is reached.

        Returns the full response dict. On success:
            result['solution']['text']               — solved image captcha text
            result['solution']['gRecaptchaResponse'] — token for reCAPTCHA

        Raises TimeoutError if not solved within self.timeout seconds.
        Raises RuntimeError on API error.

        Reference: https://anti-captcha.com/apidoc/methods/getTaskResult
        """
        payload = {"clientKey": self.api_key, "taskId": int(task_id)}
        deadline = time.time() + self.timeout

        while time.time() < deadline:
            r = requests.post(_settings.ANTICAPTCHA_GET_RESULT_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()

            if data.get("errorId", 1) != 0:
                raise RuntimeError(f"AntiCaptcha getTaskResult error: {data.get('errorDescription', data)}")

            if data.get("status") == "ready":
                return data

            time.sleep(self.polling_interval)

        raise TimeoutError(f"Timed out waiting for AntiCaptcha solution (taskId={task_id})")

    # CAPTCHA SOLVING METHODS

    def solve_image(
        self,
        image_path: str,
        case_sensitive: bool = True,
        phrase: bool = False,
        numeric: int = 0,
        math: bool = False,
        min_length: int = 0,
        max_length: int = 0,
        comment: Optional[str] = None,
        language_pool: str = "en",
        website_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submit an image captcha file and wait for the solved text.

        Reads the image file, encodes it as base64, and submits to AntiCaptcha.
        Polls until solved. Returns the full API response.

        image_path     — path to the saved captcha image file (.png / .jpg)
        case_sensitive — True = case sensitive (default True — bank CAPTCHAs require it)
        phrase         — True if answer contains at least one space
        numeric        — 0=any, 1=numbers only, 2=letters only
        math           — True if answer is a math calculation result
        min_length     — minimum answer length (0 = no limit)
        max_length     — maximum answer length (0 = no limit)
        comment        — optional hint text for workers
        language_pool  — "en" (default) or "rn" (Russia/CIS)
        website_url    — optional source URL for statistics tracking

        Extract solved text: result['solution']['text']

        Reference: https://anti-captcha.com/apidoc/task-types/ImageToTextTask
        """
        with open(image_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode("utf-8")

        task: Dict[str, Any] = {
            "type": "ImageToTextTask",
            "body": image_b64,
            "case": case_sensitive,
            "phrase": phrase,
            "numeric": numeric,
            "math": math,
            "languagePool": language_pool,
        }
        if min_length:
            task["minLength"] = min_length
        if max_length:
            task["maxLength"] = max_length
        if comment:
            task["comment"] = comment
        if website_url:
            task["websiteURL"] = website_url

        task_id = self._create_task(task)
        return self._poll_result(task_id)

    def submit_recaptcha(
        self,
        site_key: str,
        page_url: str,
        version: str = "v2",
        invisible: bool = False,
        enterprise: bool = False,
        # v3 / v3_enterprise
        min_score: float = 0.7,
        page_action: Optional[str] = None,
        # v2_enterprise
        enterprise_payload: Optional[Dict[str, Any]] = None,
        # shared optional
        api_domain: Optional[str] = None,
        data_s: Optional[str] = None,
        soft_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Submit a Google reCAPTCHA v2, v2_enterprise, v3, or v3_enterprise task
        and wait for the token.

        version  — "v2" | "v2_enterprise" | "v3" | "v3_enterprise"
        site_key — reCAPTCHA site key from the page
        page_url — full URL of the page containing the reCAPTCHA

        v2 optional:
            invisible    — True for invisible reCAPTCHA
            data_s       — value of data-s attribute (Google sites only)

        v2_enterprise optional:
            enterprise_payload — object passed to grecaptcha.enterprise.render
            api_domain         — "www.google.com" or "www.recaptcha.net"

        v3 optional:
            min_score   — 0.3 | 0.7 | 0.9 (default 0.7)
            page_action — value of the action parameter
            api_domain  — "www.google.com" or "www.recaptcha.net"

        v3_enterprise:
            page_action — required for v3_enterprise
            min_score   — optional score threshold

        shared optional:
            soft_id — developer ID for 10% commission

        Extract token: result['solution']['gRecaptchaResponse']

        References:
            https://anti-captcha.com/apidoc/task-types/RecaptchaV2TaskProxyless
            https://anti-captcha.com/apidoc/task-types/RecaptchaV3TaskProxyless
            https://anti-captcha.com/apidoc/task-types/RecaptchaV2EnterpriseTaskProxyless
            https://anti-captcha.com/apidoc/task-types/RecaptchaV3Enterprise
        """
        if version == "v3_enterprise":
            # ── reCAPTCHA v3 Enterprise ──────────────────────────────────────
            task: Dict[str, Any] = {
                "type": "RecaptchaV3Enterprise",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "pageAction": page_action or "",
            }
            if min_score:
                task["minScore"] = min_score
            if api_domain:
                task["apiDomain"] = api_domain

        elif version == "v3":
            # ── reCAPTCHA v3 ────────────────────────────────────────────────
            task = {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "minScore": min_score,
            }
            if page_action:
                task["pageAction"] = page_action
            if enterprise:
                task["isEnterprise"] = True
            if api_domain:
                task["apiDomain"] = api_domain

        elif version == "v2_enterprise":
            # ── reCAPTCHA v2 Enterprise ──────────────────────────────────────
            task = {
                "type": "RecaptchaV2EnterpriseTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if enterprise_payload:
                task["enterprisePayload"] = enterprise_payload
            if api_domain:
                task["apiDomain"] = api_domain

        else:
            # ── reCAPTCHA v2 (default) ───────────────────────────────────────
            task = {
                "type": "RecaptchaV2TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if invisible:
                task["isInvisible"] = True
            if data_s:
                task["recaptchaDataSValue"] = data_s

        if soft_id:
            task["softId"] = soft_id

        task_id = self._create_task(task)
        return self._poll_result(task_id)

    # REPORTING
    # AntiCaptcha uses separate endpoints for image vs reCAPTCHA reporting.

    def report_bad_image(self, task_id: str) -> Dict[str, Any]:
        """
        Report an image captcha solution as incorrect.
        Must be sent within 60 seconds of task completion.

        Response: {"errorId": 0, "status": "success"}
        Reference: https://anti-captcha.com/apidoc/methods/reportIncorrectImageCaptcha
        """
        payload = {"clientKey": self.api_key, "taskId": int(task_id)}
        r = requests.post(_settings.ANTICAPTCHA_REPORT_INCORRECT_IMAGE_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def report_bad_recaptcha(self, task_id: str) -> Dict[str, Any]:
        """
        Report a reCAPTCHA solution as incorrect (v2, v3, enterprise variants).
        Must be sent within 60 seconds of task completion.

        Response: {"errorId": 0, "status": "success"}
        Reference: https://anti-captcha.com/apidoc/methods/reportIncorrectRecaptcha
        """
        payload = {"clientKey": self.api_key, "taskId": int(task_id)}
        r = requests.post(_settings.ANTICAPTCHA_REPORT_INCORRECT_RECAPTCHA_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def report_good_recaptcha(self, task_id: str) -> Dict[str, Any]:
        """
        Report a reCAPTCHA solution as correct.
        Sends positive feedback to the worker.
        Must be sent within 60 seconds of task completion.

        Response: {"errorId": 0, "status": "success"}
        Reference: https://anti-captcha.com/apidoc/methods/reportCorrectRecaptcha
        """
        payload = {"clientKey": self.api_key, "taskId": int(task_id)}
        r = requests.post(_settings.ANTICAPTCHA_REPORT_CORRECT_RECAPTCHA_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_queue_stats(self, queue_id: int) -> Dict[str, Any]:
        """
        Get current worker availability and load stats for a captcha queue.
        Useful for checking wait times before submitting tasks.
        Responses are cached for 10 seconds on AntiCaptcha's side.

        queue_id — queue identifier:
            1  = ImageToText (English)
            2  = ImageToText (Russian)
            5  = Recaptcha v2 (with proxy)
            6  = Recaptcha v2 (without proxy)
            18 = Recaptcha V3 (score 0.3)
            19 = Recaptcha V3 (score 0.7)
            20 = Recaptcha V3 (score 0.9)
            23 = Recaptcha Enterprise V2 (with proxy)
            24 = Recaptcha Enterprise V2 (without proxy)

        Response:
            waiting — idle workers currently online
            load    — queue load percentage
            bid     — average task cost in USD
            speed   — average solution time in seconds
            total   — total worker count

        Reference: https://anti-captcha.com/apidoc/methods/getQueueStats
        """
        payload = {"queueId": queue_id}
        r = requests.post(_settings.ANTICAPTCHA_QUEUE_STATS_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_balance(self) -> float:
        """
        Check the remaining AntiCaptcha account balance in USD.
        Returns balance as a float. Raises on API error.

        Note: Do not call more than once every 30 seconds — cache the value.
        Reference: https://anti-captcha.com/apidoc/methods/getBalance
        """
        payload = {"clientKey": self.api_key}
        r = requests.post(_settings.ANTICAPTCHA_GET_BALANCE_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("errorId", 1) != 0:
            raise RuntimeError(f"AntiCaptcha getBalance error: {data.get('errorDescription', data)}")

        return float(data.get("balance", 0.0))
