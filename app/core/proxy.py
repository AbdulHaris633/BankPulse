import random
import time
import threading
import requests
from typing import Optional, Dict, List

from app.core.settings import Settings
from app.services.logger import debug, warn, error

_settings = Settings()

# Speed test target — Google's connectivity check endpoint.
# Returns HTTP 204 No Content instantly with zero body.
# Purpose-built for latency checks, never rate-limited, reachable through
# virtually any proxy (same endpoint Android uses for connectivity detection).
_SPEED_TEST_URL = "https://clients3.google.com/generate_204"

# Proxy302 REST API base URL
_PROXY302_API_BASE = "https://open.proxy302.com"


class ProxyManager:
    """
    Manages multiple proxy providers with round-robin rotation and speed tracking.

    Providers supported:
        - Kookeey     (https://www.kookeey.net/)
        - iProxy      (https://iproxy.online/)
        - AstroProxy  (https://astroproxy.com/)
        - Bright Data (https://brightdata.com/)  — session-based rotation
        - QuarkIP     (https://www.quarkip.com/)
        - Proxy302    (https://proxy302.apifox.cn/) — API-fetched credentials, auto-rotating IP

    How it works:
        1. On startup, builds a list of enabled providers (those with HOST set in .env)
        2. get_proxy() picks the next provider in round-robin order
        3. Returns proxy credentials in AdsPower format
        4. Measures and logs the proxy speed (latency in seconds)
        5. rotate_ip() calls the provider-specific rotation endpoint to get a fresh IP

    Round-robin is thread-safe — multiple child processes can call get_proxy()
    concurrently without collision.

    Usage:
        manager = ProxyManager()
        proxy = manager.get_proxy()          # fetches + measures speed
        manager.rotate_ip(proxy)             # rotate IP after transaction ends
    """

    def __init__(self):
        self._lock = threading.Lock()         # guards round-robin index
        self._rotate_lock = threading.Lock()  # prevents concurrent IP rotations
        self._index = random.randint(0, 1000)  # random start — spreads load across providers on restart
        self._providers = self._build_providers()
        # Maps provider name → unix timestamp when cooldown expires.
        # Providers in cooldown are skipped entirely until the timestamp passes.
        self._failed_until: Dict[str, float] = {}
        # Proxy302 — cached API token and proxy credentials.
        # Token is reused until it fails; credentials are reused until the
        # speed test fails, at which point both are cleared and re-fetched.
        self._proxy302_token: Optional[str] = None
        self._proxy302_creds: Optional[Dict] = None

        if not self._providers:
            warn("ProxyManager: no proxy providers configured — check .env credentials")
        else:
            names = [p["name"] for p in self._providers]
            debug(f"ProxyManager: {len(self._providers)} provider(s) active: {names}")

    def _build_providers(self) -> List[Dict]:
        """
        Build the list of enabled providers from settings.
        A provider is enabled when its HOST field is set in .env.
        Providers are added in a fixed order — round-robin follows this order.
        """
        providers = []

        # if _settings.KOOKEEY_HOST:
        #     providers.append({
        #         "name": "Kookeey",
        #         "host": _settings.KOOKEEY_HOST,
        #         "port": _settings.KOOKEEY_PORT,
        #         "user": _settings.KOOKEEY_USER,
        #         "pass": _settings.KOOKEEY_PASS,
        #         "rotate_url": _settings.KOOKEEY_ROTATE_URL,
        #         "rotate_mode": "url",
        #     })

        # if _settings.IPROXY_HOST:
        #     providers.append({
        #         "name": "iProxy",
        #         "host": _settings.IPROXY_HOST,
        #         "port": _settings.IPROXY_PORT,
        #         "user": _settings.IPROXY_USER,
        #         "pass": _settings.IPROXY_PASS,
        #         "rotate_url": _settings.IPROXY_ROTATE_URL,
        #         "rotate_mode": "url",
        #     })

        # if _settings.ASTROPROXY_HOST:
        #     providers.append({
        #         "name": "AstroProxy",
        #         "host": _settings.ASTROPROXY_HOST,
        #         "port": _settings.ASTROPROXY_PORT,
        #         "user": _settings.ASTROPROXY_USER,
        #         "pass": _settings.ASTROPROXY_PASS,
        #         "rotate_url": _settings.ASTROPROXY_ROTATE_URL,
        #         "rotate_mode": "url",
        #     })

        # if _settings.BRIGHTDATA_HOST:
        #     providers.append({
        #         "name": "BrightData",
        #         "host": _settings.BRIGHTDATA_HOST,
        #         "port": _settings.BRIGHTDATA_PORT,
        #         "user": _settings.BRIGHTDATA_USER,
        #         "pass": _settings.BRIGHTDATA_PASS,
        #         "rotate_url": "",
        #         # Bright Data rotates by appending a new random session ID to
        #         # the username on each get_proxy() call — no rotation URL needed
        #         "rotate_mode": "session",
        #     })

        # if _settings.QUARKIP_HOST:
        #     providers.append({
        #         "name": "QuarkIP",
        #         "host": _settings.QUARKIP_HOST,
        #         "port": _settings.QUARKIP_PORT,
        #         "user": _settings.QUARKIP_USER,
        #         "pass": _settings.QUARKIP_PASS,
        #         "rotate_url": _settings.QUARKIP_ROTATE_URL,
        #         "rotate_mode": "url",
        #     })

        if _settings.PROXY302_HOST:
            providers.append({
                "name": "Proxy302",
                "host": _settings.PROXY302_HOST,
                "port": _settings.PROXY302_PORT,
                "user": _settings.PROXY302_USER,
                "pass": _settings.PROXY302_PASS,
                "rotate_url": _settings.PROXY302_ROTATE_URL,
                "rotate_mode": "url",
            })

        return providers

    def _next_provider(self) -> Optional[Dict]:
        """Pick the next provider in round-robin order. Thread-safe."""
        if not self._providers:
            return None
        with self._lock:
            provider = self._providers[self._index % len(self._providers)]
            self._index += 1
        return provider

    # PROXY302 — API-based credential fetch

    def _get_proxy302_token(self) -> Optional[str]:
        """
        Fetch a Bearer token from the Proxy302 API using the API key credentials
        stored in settings. Result is cached — only fetched once per process
        lifetime (or after a cache clear on failure).
        """
        if self._proxy302_token:
            return self._proxy302_token
        try:
            r = requests.get(
                f"{_PROXY302_API_BASE}/open_api/v3/user/users/token",
                params={
                    "username": _settings.PROXY302_USER,
                    "password": _settings.PROXY302_PASS,
                },
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            data = r.json()
            if data.get("code") == 0:
                self._proxy302_token = data["data"]["token"]
                debug("Proxy302: token fetched and cached")
                return self._proxy302_token
            error(f"Proxy302: token fetch failed — {data.get('msg')}")
            return None
        except Exception as e:
            error(f"Proxy302: token fetch error: {e}")
            return None

    def _fetch_proxy302_credentials(self) -> Optional[Dict]:
        """
        Create a dynamic rotating proxy via the Proxy302 API and cache the
        returned credentials. Cached credentials are reused on subsequent calls.
        If credentials are cleared (after a speed-test failure) they are
        re-fetched transparently on the next call.

        Dynamic proxies on Proxy302 rotate the outbound IP automatically on
        every new TCP connection — no explicit rotation URL is needed.
        """
        if self._proxy302_creds:
            return self._proxy302_creds

        token = self._get_proxy302_token()
        if not token:
            return None

        try:
            # Geo-locked dynamic proxy — guarantees IPs from the configured
            # country on every connection (PROXY302_COUNTRY_ID=356 for India).
            params: Dict = {
                "protocol":   _settings.PROXY302_PROTOCOL,
                "country_id": _settings.PROXY302_COUNTRY_ID,
                "state_id":   _settings.PROXY302_STATE_ID,
                "city_id":    _settings.PROXY302_CITY_ID,
            }
            debug(f"Proxy302: fetching Indian IP (country_id={_settings.PROXY302_COUNTRY_ID})")

            r = requests.post(
                f"{_PROXY302_API_BASE}/open_api/v3/proxy/api/proxy/dynamic/ip/by_area",
                params=params,
                headers={"Authorization": token},
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            data = r.json()
            if data.get("code") == 0:
                d = data["data"]
                self._proxy302_creds = {
                    "host": d["host"],
                    "port": str(d["port"]),
                    "user": d["user_name"],
                    "pass": d["password"],
                }
                debug(f"Proxy302: credentials fetched — {d['host']}:{d['port']}")
                return self._proxy302_creds
            error(f"Proxy302: credentials fetch failed — {data.get('msg')}")
            return None
        except Exception as e:
            error(f"Proxy302: credentials fetch error: {e}")
            return None

    def _build_proxy_dict(self, provider: Dict) -> Optional[Dict]:
        """
        Build the AdsPower-compatible proxy credentials dict for a provider.

        - Bright Data  : appends a random session ID to the username so each
                         call gets a fresh IP without needing a rotation URL.
        - Proxy302     : fetches credentials from their REST API (cached).
        - All others   : reads static credentials from settings.

        Returns None if credential fetch fails (Proxy302 case only).
        """
        mode = provider["rotate_mode"]

        if mode == "proxy302":
            creds = self._fetch_proxy302_credentials()
            if not creds:
                return None
            return {
                "proxy_soft": "other",
                "proxy_type": _settings.PROXY302_PROTOCOL,
                "proxy_host": creds["host"],
                "proxy_port": creds["port"],
                "proxy_user": creds["user"],
                "proxy_password": creds["pass"],
                "_provider":    provider["name"],
                "_rotate_url":  "",
                "_rotate_mode": "proxy302",
            }

        user = provider["user"]

        if mode == "session":
            # Bright Data session rotation — random session suffix = new IP
            session_id = str(random.randint(100000, 999999))
            user = f"{user}-session-{session_id}"

        return {
            "proxy_soft": "other",
            "proxy_type": "socks5",
            "proxy_host": provider["host"],
            "proxy_port": provider["port"],
            "proxy_user": user,
            "proxy_password": provider["pass"],
            "_provider": provider["name"],       # internal — used by rotate_ip()
            "_rotate_url": provider["rotate_url"],
            "_rotate_mode": provider["rotate_mode"],
        }

    def _measure_speed(self, proxy: Dict) -> Optional[float]:
        """
        Measure proxy latency by timing a GET request through the proxy
        to a lightweight test endpoint. Returns latency in seconds, or
        None if the proxy is unreachable.
        """
        proxy_url = "socks5://{user}:{pw}@{host}:{port}".format(
            user=proxy["proxy_user"],
            pw=proxy["proxy_password"],
            host=proxy["proxy_host"],
            port=proxy["proxy_port"],
        )
        # Only "https" key needed — test URL is HTTPS, requests uses this entry
        proxies = {"https": proxy_url}
        try:
            start = time.time()
            requests.get(
                _SPEED_TEST_URL,
                proxies=proxies,
                timeout=(_settings.PROXY_CONNECT_TIMEOUT, _settings.PROXY_READ_TIMEOUT)
            )
            return round(time.time() - start, 2)
        except Exception:
            return None

    def _is_in_cooldown(self, provider: Dict) -> bool:
        """Return True if this provider is still in its failure cooldown period."""
        until = self._failed_until.get(provider["name"], 0)
        if time.time() < until:
            remaining = round(until - time.time())
            debug(f"ProxyManager: {provider['name']} in cooldown — skipping ({remaining}s remaining)")
            return True
        return False

    def _set_cooldown(self, provider: Dict) -> None:
        """Put a failed provider in cooldown for PROXY_COOLDOWN_SECONDS."""
        self._failed_until[provider["name"]] = time.time() + _settings.PROXY_COOLDOWN_SECONDS
        warn(f"ProxyManager: {provider['name']} placed in cooldown for {_settings.PROXY_COOLDOWN_SECONDS}s")

    def get_proxy(self) -> Optional[Dict]:
        """
        Get proxy credentials from the next provider in rotation.
        Measures speed via a real connection test through the proxy.

        Providers that failed recently are skipped for PROXY_COOLDOWN_SECONDS
        (default 60s) to avoid hammering dead providers on every transaction.
        After cooldown expires the provider is automatically retried.

        Falls back through all non-cooldown providers before giving up.
        Returns None only if ALL providers are either failed or in cooldown.

        Round-robin order: Kookeey → iProxy → AstroProxy → BrightData → QuarkIP
        (only enabled providers participate)
        """
        if not self._providers:
            error("ProxyManager: no providers configured")
            return None

        # Try each provider once — skip those in cooldown
        total = len(self._providers)
        for attempt in range(total):
            provider = self._next_provider()

            if self._is_in_cooldown(provider):
                continue

            proxy = self._build_proxy_dict(provider)

            if proxy is None:
                # Credential fetch failed (Proxy302 API down / bad credentials)
                self._set_cooldown(provider)
                continue

            debug(f"ProxyManager: trying {provider['name']} — {proxy['proxy_host']}:{proxy['proxy_port']}")

            latency = self._measure_speed(proxy)

            if latency is not None:
                debug(f"ProxyManager: {provider['name']} speed = {latency}s — OK")
                return proxy

            # Speed test failed — clear Proxy302 credential cache so fresh
            # credentials are fetched on the next attempt, then put in cooldown
            if provider["rotate_mode"] == "proxy302":
                self._proxy302_creds = None
                debug("Proxy302: cleared credential cache after speed-test failure")
            self._set_cooldown(provider)

        # All providers either failed or in cooldown
        error("ProxyManager: all providers failed or in cooldown — no proxy available")
        return None

    def rotate_ip(self, proxy: Dict) -> bool:
        """
        Rotate the IP for this proxy after a transaction completes.

        - url mode    : GET the rotation URL provided by the dashboard
        - session mode: no-op — Bright Data gets a new IP on the next
                        get_proxy() call via a new session ID in the username

        proxy — the dict returned by get_proxy() (must contain _rotate_* fields)
        Returns True on success or if no rotation is needed.
        """
        mode = proxy.get("_rotate_mode", "url")
        provider_name = proxy.get("_provider", "unknown")

        if mode == "session":
            # Session rotation happens automatically on next get_proxy() call
            debug(f"ProxyManager: {provider_name} uses session rotation — no hangup needed")
            return True

        if mode == "proxy302":
            # Proxy302 dynamic proxies rotate the outbound IP automatically on
            # every new TCP connection — no explicit rotation call needed
            debug(f"ProxyManager: {provider_name} uses dynamic rotation — no hangup needed")
            return True

        rotate_url = proxy.get("_rotate_url", "")
        if not rotate_url:
            debug(f"ProxyManager: {provider_name} has no rotation URL configured")
            return True

        try:
            with self._rotate_lock:
                r = requests.get(rotate_url, timeout=_settings.REQUESTS_TIMEOUT)
                if r.ok:
                    debug(f"ProxyManager: {provider_name} IP rotated successfully — waiting {_settings.PROXY_ROTATION_DELAY}s for new IP to be assigned")
                    time.sleep(_settings.PROXY_ROTATION_DELAY)
                    return True
            warn(f"ProxyManager: {provider_name} rotation returned {r.status_code}")
            return False
        except Exception as e:
            error(f"ProxyManager: {provider_name} rotation failed: {e}")
            return False

# Module-level singleton — shared across all calls in the same process
_proxy_manager = ProxyManager()


def get_proxy_manager() -> ProxyManager:
    """Return the shared ProxyManager instance."""
    return _proxy_manager
