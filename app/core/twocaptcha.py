import base64
import time
import requests
from typing import Optional, Dict, Any

from app.core.settings import Settings

_settings = Settings()


class TwoCaptchaClient:
    """
    Custom 2Captcha API client using the new JSON-based API.

    New API (this class):
        solver = TwoCaptchaClient(api_key)
        result = solver.solve_image(image_path, case_sensitive=True)
        text = result['solution']['text']   <-- result key

    All endpoint URLs are loaded from Settings (settings.py).

    Captcha types supported:
    - Image captcha    → solve_image()      (most common — bank login CAPTCHAs)
    - reCAPTCHA v2/v3  → submit_recaptcha() (some bank portals use Google reCAPTCHA)

    Reference: https://2captcha.com/api-docs/normal-captcha
    GitHub:    https://github.com/2captcha/2captcha-python
    """

    def __init__(
        self,
        api_key: str,
        polling_interval: float = 5.0,
        timeout: float = 120.0
    ):
        """
        api_key          — 2Captcha API key from settings.TWOCAPTCHA_API_KEY
        polling_interval — seconds to wait between polls for the solved result
        timeout          — total seconds to wait before giving up on a captcha
        """
        self.api_key = api_key
        self.polling_interval = polling_interval
        self.timeout = timeout

    def _create_task(self, task: Dict[str, Any]) -> str:
        """
        Submit a task to 2Captcha and return the taskId.
        Raises on API error (errorId != 0).
        """
        payload = {"clientKey": self.api_key, "task": task}
        r = requests.post(_settings.TWOCAPTCHA_CREATE_TASK_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("errorId", 1) != 0:
            raise RuntimeError(f"2Captcha createTask error: {data.get('errorDescription', data)}")

        return str(data["taskId"])

    def _poll_result(self, task_id: str) -> Dict[str, Any]:
        """
        Poll getTaskResult until status == 'ready' or timeout is reached.

        Returns the full response dict. On success:
            result['solution']['text']               — solved image captcha text
            result['solution']['gRecaptchaResponse'] — token for reCAPTCHA/hCaptcha

        Raises TimeoutError if not solved within self.timeout seconds.
        Raises RuntimeError on API error.
        """
        payload = {"clientKey": self.api_key, "taskId": task_id}
        deadline = time.time() + self.timeout

        while time.time() < deadline:
            r = requests.post(_settings.TWOCAPTCHA_GET_RESULT_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()

            if data.get("errorId", 1) != 0:
                raise RuntimeError(f"2Captcha getTaskResult error: {data.get('errorDescription', data)}")

            if data.get("status") == "ready":
                return data

            time.sleep(self.polling_interval)

        raise TimeoutError(f"Timed out waiting for 2Captcha solution (taskId={task_id})")

    # CAPTCHA SOLVING METHODS

    def solve_image(
        self,
        image_path: str,
        case_sensitive: bool = False,
        phrase: bool = False,
        numeric: int = 0,
        math: bool = False,
        min_length: int = 0,
        max_length: int = 0,
        comment: Optional[str] = None,
        img_instructions: Optional[str] = None,
        language_pool: str = "en"
    ) -> Dict[str, Any]:
        """
        Submit an image captcha file and wait for the solved text.

        Reads the image file, encodes it as base64, and submits to 2Captcha.
        Polls until solved. Returns the full API response.

        image_path       — path to the saved captcha image file (.png / .jpg)
        case_sensitive   — True when bank requires exact case (case=true in API)
        phrase           — True if answer is expected to be multiple words
        numeric          — 0=any, 1=numbers only, 2=letters only,
                           3=numbers OR letters, 4=numbers AND letters
        math             — True if the captcha requires a math calculation
        min_length       — minimum expected answer length (0 = no limit)
        max_length       — maximum expected answer length (0 = no limit)
        comment          — optional hint text shown to the solver worker
        img_instructions — optional instruction image (base64) shown to worker
        language_pool    — "en" (default) or "rn" (Cyrillic)

        Extract solved text: result['solution']['text']

        Reference: https://2captcha.com/api-docs/normal-captcha
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
        }
        if min_length:
            task["minLength"] = min_length
        if max_length:
            task["maxLength"] = max_length
        if comment:
            task["comment"] = comment
        if img_instructions:
            task["imgInstructions"] = img_instructions

        # languagePool is a top-level field, not inside task
        full_payload = {
            "clientKey": self.api_key,
            "task": task,
            "languagePool": language_pool
        }
        r = requests.post(_settings.TWOCAPTCHA_CREATE_TASK_URL, json=full_payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("errorId", 1) != 0:
            raise RuntimeError(f"2Captcha createTask error: {data.get('errorDescription', data)}")

        return self._poll_result(str(data["taskId"]))

    def submit_recaptcha(
        self,
        site_key: str,
        page_url: str,
        version: str = "v2",
        invisible: bool = False,
        enterprise: bool = False,
        # v3-only
        min_score: float = 0.7,
        page_action: Optional[str] = None,
        # shared optional
        api_domain: Optional[str] = None,
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        data_s: Optional[str] = None,
        enterprise_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Submit a Google reCAPTCHA v2, v2-enterprise, or v3 task and wait for
        the token.

        version    — "v2" | "v2_enterprise" | "v3" | "v3_enterprise"
        site_key   — reCAPTCHA site key (data-sitekey attribute on the page)
        page_url   — full URL of the page containing the reCAPTCHA

        v2 / v2_enterprise optional:
            invisible          — True for invisible reCAPTCHA variant
            data_s             — recaptchaDataSValue (rare, some sites need it)
            enterprise_payload — extra payload dict for v2 enterprise
            user_agent         — custom user agent string
            cookies            — cookies string ("name=value;name2=value2")

        v3 / v3_enterprise optional:
            min_score   — 0.3 | 0.7 | 0.9  (default 0.7)
            page_action — action name from data-action attribute

        shared optional:
            api_domain  — "google.com" (default) or "recaptcha.net"

        Note: 2Captcha uses the same task type for v3 and v3_enterprise
        (RecaptchaV3TaskProxyless) — enterprise is enabled via isEnterprise: true.
        Identify enterprise by grecaptcha.enterprise.execute call or enterprise.js.

        Extract token: result['solution']['gRecaptchaResponse']

        References:
            https://2captcha.com/api-docs/recaptcha-v2
            https://2captcha.com/api-docs/recaptcha-v3
            https://2captcha.com/api-docs/recaptcha-v2-enterprise
        """
        if version in ("v3", "v3_enterprise"):
            # ── reCAPTCHA v3 / v3 Enterprise ────────────────────────────────
            # Same task type for both — enterprise variant uses isEnterprise: true
            task: Dict[str, Any] = {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "minScore": min_score,
            }
            if version == "v3_enterprise":
                task["isEnterprise"] = True
            elif enterprise:
                task["isEnterprise"] = True
            if page_action:
                task["pageAction"] = page_action
            if api_domain:
                task["apiDomain"] = api_domain

        elif version == "v2_enterprise":
            # ── reCAPTCHA v2 Enterprise ──────────────────────────────────────
            task = {
                "type": "RecaptchaV2EnterpriseTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if invisible:
                task["isInvisible"] = True
            if enterprise_payload:
                task["enterprisePayload"] = enterprise_payload
            if user_agent:
                task["userAgent"] = user_agent
            if cookies:
                task["cookies"] = cookies
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
            if user_agent:
                task["userAgent"] = user_agent
            if cookies:
                task["cookies"] = cookies
            if api_domain:
                task["apiDomain"] = api_domain

        task_id = self._create_task(task)
        return self._poll_result(task_id)

    def report_bad(self, task_id: str) -> Dict[str, Any]:
        """
        Report a captcha solution as incorrect to 2Captcha.
        2Captcha refunds the cost and penalises the worker who solved it badly.
        Call this when a bank rejects the captcha text we submitted.

        Response: {"errorId": 0, "status": "success"}
        Reference: https://2captcha.com/api-docs/report-incorrect
        """
        payload = {"clientKey": self.api_key, "taskId": task_id}
        r = requests.post(_settings.TWOCAPTCHA_REPORT_INCORRECT_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def report_good(self, task_id: str) -> Dict[str, Any]:
        """
        Report a captcha solution as correct to 2Captcha.
        Sends positive feedback — rewards the worker who solved it.
        Call this when the bank accepts the captcha text we submitted.

        Response: {"errorId": 0, "status": "success"}
        Reference: https://2captcha.com/api-docs/report-correct
        """
        payload = {"clientKey": self.api_key, "taskId": task_id}
        r = requests.post(_settings.TWOCAPTCHA_REPORT_CORRECT_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_balance(self) -> float:
        """
        Check the remaining 2Captcha account balance.
        Returns the balance as a float. Raises on API error.
        """
        payload = {"clientKey": self.api_key}
        r = requests.post(_settings.TWOCAPTCHA_GET_BALANCE_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("errorId", 1) != 0:
            raise RuntimeError(f"2Captcha getBalance error: {data.get('errorDescription', data)}")

        return float(data.get("balance", 0.0))
