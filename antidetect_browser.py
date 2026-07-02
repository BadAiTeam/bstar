#!/usr/bin/env python3
"""
Anti-Detect Browser Integration Module — v9.8 (WebGL Hardened)
================================================

REVISION HISTORY (webgl fix):
  v9.7   : Changed `webgl` from dict to integer 1 (Custom mode)
  v9.7.1 : Added `webgl_image` field + defensive sanitization + payload logging
  v9.8   : REMOVED `webgl_image` (not a real AdsPower v2 field — was causing
           the residual "webgl must be 0,1,2,3" error). Added retry-with-
           fallback logic: try webgl=1, then webgl=0, then omit webgl entirely.
           Added MODULE_VERSION stamp printed at import time so user can
           verify they are running the patched file.

v9.8 also adds:
  - Multi-format webgl value attempts (int, then string, then omitted)
  - update_profile() now retries with fallback webgl values on error
  - create_profile() now retries with fallback webgl values on error
  - Error logging is UNCONDITIONAL — always prints full payload on error

Rekomendasi #1: Gunakan Peramban Anti-Deteksi Asli (Anti-detect Browser)
Alih-alih memalsukan properti secara manual via proksi JS yang meninggalkan
banyak jejak, modul ini mengintegrasikan otomatisasi dengan peramban
anti-deteksi terpercaya (AdsPower, Multilogin, Dolphin{anty}) melalui API mereka.

Peramban tersebut memodifikasi kode sumber mesin C++ Chromium secara langsung
sehingga properti seperti navigator.webdriver atau emulasi WebGL bersifat alami
dan tidak dapat dideteksi lewat prototype chain.

Supported Anti-Detect Browsers:
  - AdsPower (Local API v2 — http://127.0.0.1:50325)
  - Multilogin (local API on port 45001)
  - Dolphin{anty} (local API on port 3001)

v9.6 changes:
  - Fix: 3-tab issue — close AdsPower start page & duplicates after CDP connect
  - After CDP connect, close ALL existing pages then create ONE clean tab
  - Only 1 tab will open in browser: the target URL

v9.5 changes:
  - Auto-cleanup old bot profiles when profile limit is reached
  - WebSocket CDP connection: 3s wait + 3x retry with backoff
  - v2 API only — removed all v1 endpoint fallbacks (faster startup)
  - Navigator.webdriver=false confirmed working with AdsPower

v9.4 changes:
  - Fix: group_id is required for profile creation — auto-detect default group
  - group_id parameter added to AdsPowerClient and AntiDetectManager
  - group_id field (from proxy.json or Proxy API)
  - profile_id support: gunakan profil yang sudah ada jika create tidak tersedia
  - Retry logic: 3x percobaan dengan jeda 5 detik jika koneksi gagal

v9.3 changes:
  - AdsPower: Updated to API v2 endpoints (from official local-api-mcp-typescript docs)
  - Create/Start/Stop/List/Delete now use /api/v2/browser-profile/* endpoints
  - Authentication via Authorization: Bearer {API_KEY} header (not ?key= param)
  - Base URL changed to http://127.0.0.1:{PORT} (not local.adspower.net)
  - All mutating endpoints use POST with JSON body (not GET with query params)
  - Profile creation uses new v2 schema: user_proxy_config, fingerprint_config, etc.

Modes:
  - "antidetect" : Uses anti-detect browser via API (RECOMMENDED)
  - "patchright" : Falls back to Patchright + enhanced stealth (legacy)

Usage in bot_v6.py:
  from antidetect_browser import AntiDetectManager

  # Local AdsPower with API key
  manager = AntiDetectManager(mode="antidetect", browser_type="adspower",
                              api_key="62a501557b09c8444a57c3318943a0910092c5c0d322e39b")
  session = manager.create_profile(proxy_config, profile_config)
  ...
  manager.close_profile(session)
"""

import os
import sys
import time
import json
import random
import logging
import re
import requests as http_requests
from typing import Optional, Dict, Any, List

# v9.3: Defensive suppress InsecureRequestWarning.
# Saat ini antidetect_browser.py tidak memakai verify=False, tapi jika
# ada API call yang melewati proxy SSL-intercepting di masa depan,
# warning tidak akan mengganggu log.
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

logger = logging.getLogger('antidetect_browser')

# v9.3: Cascade fix + dead-code fix + defensive InsecureRequestWarning suppress.
#   v9.8   : webgl retry with 3 sequential variants (SLOW: +3-6s per profile)
#   v9.9   : Tentukan webgl value yang berhasil SEKALI saat startup, cache
#            di class-level. Hapus sequential retry. Kurangi time.sleep(3)
#            menjadi time.sleep(1) setelah start_profile. Cache profile_id
#            yang sudah dibuat untuk reuse cepat di user berikutnya.
#   v9.3   : Fix dead-code bug di _try_use_existing_profile — `if` dan `elif`
#            sebelumnya punya kondisi identik, sehingga branch Multilogin/
#            Dolphin (clients tanpa _build_update_body) tidak pernah
#            dijalankan. Sekarang `elif` mengecek keberadaan
#            _build_update_body secara eksplisit.
MODULE_VERSION = 'v9.8-api-proxy'
_MODULE_FILE = os.path.abspath(__file__)
print(
    f"[antidetect_browser] LOADED {MODULE_VERSION} from {_MODULE_FILE}",
    file=sys.stderr,
    flush=True,
)
logger.info(f"antidetect_browser {MODULE_VERSION} loaded from {_MODULE_FILE}")


# ====================================================================
# Proxy API Loader — Fetch proxies from external API (replaces proxy.json)
# ====================================================================

PROXY_API_URL = 'https://nodejsclusters-213001-0.cloudclusters.net/api/external/proxies/range'
PROXY_API_KEY = 'pm_957328ec98d6ecbd6da178b77c8282fb'
PROXY_API_DEFAULT_PAGE_SIZE = 30
PROXY_API_DEFAULT_FORMAT = 'txt'

# Default AdsPower credentials (can be overridden via load_proxy_config args or env vars)
ADSPOWER_DEFAULTS = {
    'api_key': '0a9a0ceae5ea9e56065395e58ce43ace0092eda5d96b6ecd',
    'mode': 'local',
    'base_url': 'http://127.0.0.1',
    'port': 50325,
    'profile_id': '',
    'group_id': '',
}


def parse_proxy_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single proxy line from the API txt response into a proxy dict.

    Supported formats:
      - protocol://host:port             (e.g. http://1.2.3.4:8080)
      - protocol://user:pass@host:port   (e.g. http://user:pass@1.2.3.4:8080)
      - host:port                        (assumes http)
      - user:pass@host:port              (assumes http)
      - host:port:user:pass              (assumes http)

    Returns:
        Dict with keys: proxy_host, proxy_port, proxy_user, proxy_password, proxy_type
        Or None if the line cannot be parsed.
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    proxy_type = 'http'  # default
    proxy_host = ''
    proxy_port = 0
    proxy_user = ''
    proxy_password = ''

    # Format: protocol://user:pass@host:port  or  protocol://host:port
    url_match = re.match(
        r'^(https?|socks[45]?):\/\/(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)$',
        line, re.IGNORECASE
    )
    if url_match:
        proxy_type = url_match.group(1).lower()
        proxy_user = url_match.group(2) or ''
        proxy_password = url_match.group(3) or ''
        proxy_host = url_match.group(4)
        try:
            proxy_port = int(url_match.group(5))
        except ValueError:
            return None
        return {
            'proxy_host': proxy_host,
            'proxy_port': proxy_port,
            'proxy_user': proxy_user,
            'proxy_password': proxy_password,
            'proxy_type': proxy_type,
        }

    # Format: user:pass@host:port (no protocol prefix, assume http)
    auth_match = re.match(
        r'^(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)$',
        line
    )
    if auth_match:
        proxy_user = auth_match.group(1) or ''
        proxy_password = auth_match.group(2) or ''
        proxy_host = auth_match.group(3)
        try:
            proxy_port = int(auth_match.group(4))
        except ValueError:
            return None
        return {
            'proxy_host': proxy_host,
            'proxy_port': proxy_port,
            'proxy_user': proxy_user,
            'proxy_password': proxy_password,
            'proxy_type': proxy_type,
        }

    # Format: host:port:user:pass (colon-separated 4 fields, assume http)
    parts = line.split(':')
    if len(parts) == 4:
        proxy_host = parts[0]
        try:
            proxy_port = int(parts[1])
        except ValueError:
            return None
        proxy_user = parts[2]
        proxy_password = parts[3]
        return {
            'proxy_host': proxy_host,
            'proxy_port': proxy_port,
            'proxy_user': proxy_user,
            'proxy_password': proxy_password,
            'proxy_type': proxy_type,
        }

    # Format: host:port (2 fields, assume http)
    if len(parts) == 2:
        proxy_host = parts[0]
        try:
            proxy_port = int(parts[1])
        except ValueError:
            return None
        return {
            'proxy_host': proxy_host,
            'proxy_port': proxy_port,
            'proxy_user': '',
            'proxy_password': '',
            'proxy_type': proxy_type,
        }

    logger.warning(f"parse_proxy_line: cannot parse line: {line!r}")
    return None


def load_proxies_from_api(
    api_url: str = PROXY_API_URL,
    api_key: str = PROXY_API_KEY,
    page_size: int = PROXY_API_DEFAULT_PAGE_SIZE,
    fmt: str = PROXY_API_DEFAULT_FORMAT,
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    Fetch proxies from the external proxy API (replaces loading from proxy.json).

    The API returns plain text with one proxy per line, e.g.:
        http://178.62.184.67:3128
        http://user:pass@1.2.3.4:8080

    Args:
        api_url:  Base URL of the proxy API endpoint.
        api_key:  X-API-Key header value for authentication.
        page_size: Number of proxies to request (pageSize query param).
        fmt:       Response format (default 'txt').
        timeout:   HTTP request timeout in seconds.

    Returns:
        List of proxy dicts, each with keys:
            proxy_host, proxy_port, proxy_user, proxy_password, proxy_type
        Empty list on failure.
    """
    headers = {'X-API-Key': api_key}
    params = {'format': fmt, 'pageSize': page_size}

    logger.info(f"Proxy API: Fetching up to {page_size} proxies from {api_url}")

    try:
        resp = http_requests.get(api_url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Proxy API: Failed to fetch proxies — {e}")
        return []

    text = resp.text.strip()
    if not text:
        logger.warning("Proxy API: Response body is empty")
        return []

    proxies = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        parsed = parse_proxy_line(line)
        if parsed:
            proxies.append(parsed)
        else:
            logger.debug(f"Proxy API: Skipped unparsable line {line_no}: {line!r}")

    logger.info(f"Proxy API: Loaded {len(proxies)} proxy(ies)")
    return proxies


def load_proxy_config(
    api_url: str = PROXY_API_URL,
    api_key: str = PROXY_API_KEY,
    page_size: int = PROXY_API_DEFAULT_PAGE_SIZE,
    fmt: str = PROXY_API_DEFAULT_FORMAT,
    timeout: int = 15,
    # AdsPower credentials
    adspower_api_key: str = '',
    adspower_mode: str = '',
    adspower_base_url: str = '',
    adspower_port: int = 0,
    adspower_profile_id: str = '',
    adspower_group_id: str = '',
) -> Dict[str, Any]:
    """
    Load complete proxy + AdsPower configuration from the external API.

    This is a drop-in replacement for the old pattern of loading proxy.json.
    It returns a dict with the same top-level structure that bot_v6.py
    previously read from the JSON file:
        {
            'adspower': {
                'api_key':    <str>,
                'mode':       <str>,   # 'local' or 'cloud'
                'base_url':   <str>,
                'port':       <int>,
                'profile_id': <str>,
                'group_id':   <str>,
            },
            'proxies':    [ {proxy_host, proxy_port, ...}, ... ]
        }

    AdsPower credentials resolution order (per field):
      1. Explicit argument (e.g. adspower_api_key='...')
      2. Environment variable (e.g. ADSPOWER_API_KEY)
      3. Built-in default from ADSPOWER_DEFAULTS

    Args:
        api_url:            Proxy API endpoint URL.
        api_key:            API key for the proxy service.
        page_size:          Number of proxies to request.
        fmt:                Response format ('txt').
        timeout:            HTTP request timeout.
        adspower_api_key:   AdsPower API key (overrides env var / default).
        adspower_mode:      AdsPower mode — 'local' or 'cloud'.
        adspower_base_url:  AdsPower API base URL.
        adspower_port:      AdsPower API port.
        adspower_profile_id: AdsPower profile ID.
        adspower_group_id:  AdsPower group ID.

    Returns:
        Configuration dict compatible with the old proxy.json schema.
    """
    proxies = load_proxies_from_api(
        api_url=api_url,
        api_key=api_key,
        page_size=page_size,
        fmt=fmt,
        timeout=timeout,
    )

    # Resolve AdsPower credentials with 3-tier fallback:
    #   explicit arg > env var > ADSPOWER_DEFAULTS
    resolved_adspower = {
        'api_key': (
            adspower_api_key
            or os.environ.get('ADSPOWER_API_KEY', '')
            or ADSPOWER_DEFAULTS['api_key']
        ),
        'mode': (
            adspower_mode
            or os.environ.get('ADSPOWER_MODE', '')
            or ADSPOWER_DEFAULTS['mode']
        ),
        'base_url': (
            adspower_base_url
            or os.environ.get('ADSPOWER_API_BASE', '')
            or ADSPOWER_DEFAULTS['base_url']
        ),
        'port': (
            adspower_port
            or (int(os.environ['ADSPOWER_PORT']) if 'ADSPOWER_PORT' in os.environ else 0)
            or ADSPOWER_DEFAULTS['port']
        ),
        'profile_id': (
            adspower_profile_id
            or os.environ.get('ADSPOWER_PROFILE_ID', '')
            or ADSPOWER_DEFAULTS['profile_id']
        ),
        'group_id': (
            adspower_group_id
            or os.environ.get('ADSPOWER_GROUP_ID', '')
            or ADSPOWER_DEFAULTS['group_id']
        ),
    }

    config = {
        'adspower': resolved_adspower,
        'proxies': proxies,
    }

    logger.info(
        f"Config loaded from API: {len(proxies)} proxies, "
        f"adspower_api_key={resolved_adspower['api_key'][:8]}..., "
        f"adspower_mode={resolved_adspower['mode']!r}, "
        f"adspower_base={resolved_adspower['base_url']}:{resolved_adspower['port']}, "
        f"profile_id={resolved_adspower['profile_id']!r}, "
        f"group_id={resolved_adspower['group_id']!r}"
    )
    return config


# ====================================================================
# Anti-Detect Browser API Clients
# ====================================================================

class AdsPowerClient:
    """
    AdsPower API Client — Local API v2.
    
    API v2 Endpoints (only v2 — v1 returns 404 on current AdsPower):
      - GET  /status                              → Check if API is running
      - POST /api/v2/browser-profile/create       → Create a new profile
      - POST /api/v2/browser-profile/start         → Start a profile (open browser)
      - POST /api/v2/browser-profile/stop          → Stop a profile (close browser)
      - POST /api/v2/browser-profile/list          → List profiles
      - POST /api/v2/browser-profile/delete        → Delete profiles
      - POST /api/v2/browser-profile/update        → Update profile config
      - GET  /api/v2/browser-profile/active        → Check if profile is active
    
    Authentication: Authorization: Bearer {API_KEY} header
    Base URL: http://127.0.0.1:{PORT}
    All mutating endpoints use POST with JSON body.
    """

    DEFAULT_LOCAL_BASE = 'http://127.0.0.1'
    DEFAULT_LOCAL_PORT = 50325

    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds between retries
    API_CALL_INTERVAL = 0.8  # minimum seconds between API calls (rate limit protection)
    _last_api_call_time = 0  # class-level timestamp of last API call

    # BUG FIX #8: Cache webgl value yang berhasil agar tidak retry 3x
    # sequential setiap update_profile (menghemat 3-6 detik per user).
    # None = belum ditentukan, akan dicoba saat update/create pertama.
    _cached_webgl_value = None  # int 0/1/2/3 atau None (omit)
    _cached_webgl_drop_vendor = False  # apakah drop vendor/renderer juga

    # BUG FIX #6: Cache profile_id yang sudah dibuat untuk reuse cepat.
    # Menghindari create_profile() + list_profiles() + update_profile()
    # yang memakan 30-40 detik setiap user.
    _cached_profile_id = None  # profile_id yang sudah di-sync dan siap pakai

    def __init__(self, api_key=None, api_base=None, port=None, profile_id=None, group_id=None):
        """
        Args:
            api_key: AdsPower API key (sent as Authorization: Bearer header)
            api_base: Override API base URL (default: http://127.0.0.1)
            port: Override API port (default: 50325)
            profile_id: ID profil yang sudah ada (opsional, skip create jika ada)
            group_id: ID grup AdsPower (opsional, auto-detect jika kosong)
        """
        self.api_key = api_key or os.environ.get('ADSPOWER_API_KEY', '')
        self.api_base = api_base or os.environ.get('ADSPOWER_API_BASE', self.DEFAULT_LOCAL_BASE)
        self.port = port or int(os.environ.get('ADSPOWER_PORT', self.DEFAULT_LOCAL_PORT))
        self.default_profile_id = profile_id or os.environ.get('ADSPOWER_PROFILE_ID', '')
        self.default_group_id = group_id or os.environ.get('ADSPOWER_GROUP_ID', '')
        self.base_url = f"{self.api_base}:{self.port}"
        logger.info(f"AdsPower: Using Local API v2 ({self.base_url})")
        if not self.api_key:
            logger.warning("AdsPower: Tidak ada API key! Set ADSPOWER_API_KEY.")
        if self.default_profile_id:
            logger.info(f"AdsPower: Default profile_id = {self.default_profile_id}")
        if self.default_group_id:
            logger.info(f"AdsPower: Default group_id = {self.default_group_id}")

    def _headers(self):
        """Build request headers with Bearer auth."""
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers

    def _request(self, method, path, json_data=None, params=None, timeout=15, _retry_count=0, silent_errors=False):
        """
        Make an HTTP request to AdsPower Local API.
        
        Args:
            method: 'GET' or 'POST'
            path: API endpoint path (e.g., '/api/v2/browser-profile/start')
            json_data: JSON body for POST requests
            params: Query parameters for GET requests
            timeout: Request timeout in seconds
            _retry_count: Internal retry counter
            silent_errors: bila True, suppress warning log untuk non-zero
                           response codes (response tetap dikembalikan).
                           Dipakai oleh stop_profile(silent=True) agar
                           "Profile is not open" tidak noisy di log.
        """
        url = f"{self.base_url}{path}"
        
        # Rate limit protection: ensure minimum interval between API calls
        now = time.time()
        elapsed = now - AdsPowerClient._last_api_call_time
        if elapsed < AdsPowerClient.API_CALL_INTERVAL and AdsPowerClient._last_api_call_time > 0:
            wait = AdsPowerClient.API_CALL_INTERVAL - elapsed
            time.sleep(wait)
        AdsPowerClient._last_api_call_time = time.time()
        
        try:
            if method == 'GET':
                resp = http_requests.get(url, headers=self._headers(), params=params, timeout=timeout)
            else:
                resp = http_requests.post(url, headers=self._headers(), json=json_data or {}, timeout=timeout)
            
            logger.debug(f"AdsPower {method} {path} → HTTP {resp.status_code}, Body length: {len(resp.text)}")
            
            # Handle empty response
            if not resp.text or resp.text.strip() == '':
                logger.warning(f"AdsPower {method} {path} mengembalikan response KOSONG (HTTP {resp.status_code})")
                return None
            
            # Parse JSON
            try:
                data = resp.json()
            except ValueError:
                logger.error(
                    f"AdsPower {method} {path} mengembalikan response BUKAN JSON!\n"
                    f"HTTP {resp.status_code}, Response: {resp.text[:300]}"
                )
                return None
            
            if data.get('code') == 0:
                return data
            else:
                # v9.7.1: Log full request payload + response on error so we can
                # see exactly which field AdsPower is rejecting.
                # v9.5.2: Bila silent_errors=True (dipanggil dari stop_profile
                # silent cleanup), skip warning log — caller akan handle
                # sendiri berdasarkan response code/msg.
                if not silent_errors:
                    try:
                        payload_str = json.dumps(json_data or params or {}, default=str)
                        if len(payload_str) > 1500:
                            payload_str = payload_str[:1500] + '...(truncated)'
                    except Exception:
                        payload_str = '<unable-to-serialize>'
                    logger.warning(
                        f"AdsPower API error: code={data.get('code')}, msg={data.get('msg', 'unknown')}\n"
                        f"  endpoint : {method} {path}\n"
                        f"  request  : {payload_str}\n"
                        f"  response : {json.dumps(data, default=str)[:500]}"
                    )
                return data
                
        except http_requests.exceptions.ConnectionError as e:
            if _retry_count < self.MAX_RETRIES:
                _retry_count += 1
                logger.warning(f"AdsPower Local API tidak bisa terhubung (percobaan {_retry_count}/{self.MAX_RETRIES}): {e}")
                logger.info(f"Menunggu {self.RETRY_DELAY} detik sebelum mencoba lagi...")
                time.sleep(self.RETRY_DELAY)
                return self._request(method, path, json_data, params, timeout, _retry_count)
            logger.error(
                f"AdsPower Local API gagal setelah {self.MAX_RETRIES}x percobaan!\n"
                f"Kemungkinan penyebab:\n"
                f"  1. AdsPower belum dibuka atau belum login\n"
                f"  2. Local API tidak aktif di port {self.port}\n"
                f"  3. Port salah (cek Settings → Local API di AdsPower)\n"
                f"Error detail: {e}"
            )
            return None
        except Exception as e:
            logger.error(f"AdsPower API request failed: {e}")
            return None

    def _get(self, path, params=None, timeout=15):
        """GET request helper."""
        return self._request('GET', path, params=params, timeout=timeout)

    def _post(self, path, json_data=None, timeout=15, silent_errors=False):
        """POST request helper."""
        return self._request('POST', path, json_data=json_data, timeout=timeout, silent_errors=silent_errors)

    def check_status(self):
        """Check if AdsPower Local API is running."""
        result = self._get('/status')
        if result is not None:
            logger.info(f"AdsPower Local API v2 accessible ({self.base_url})")
            return True
        logger.error(
            f"AdsPower Local API TIDAK bisa diakses di {self.base_url}!\n"
            f"Pastikan:\n"
            f"  1. AdsPower sudah dibuka dan login\n"
            f"  2. Local API aktif di port {self.port} (Settings → Local API)\n"
            f"  3. Cek di browser: {self.base_url}/status"
        )
        return False

    def _get_default_group_id(self):
        """
        Get group_id for profile creation.

        Priority:
          1. Use explicitly configured group_id (from constructor, env var, or Proxy API config)
          2. Fallback to "0" (default group in most AdsPower installations)
        """
        if self.default_group_id:
            logger.info(f"AdsPower: Using configured group_id = {self.default_group_id}")
            return self.default_group_id
        logger.info('AdsPower: Using default group_id = "0"')
        return "0"

    # =====================================================
    # v9.1: Helpers to build fingerprint_config bodies
    # =====================================================
    # AdsPower's v2 /browser-profile/create and /browser-profile/update
    # endpoints accept a `fingerprint_config` object. The previous code
    # only populated a subset (ua, os, language, resolution, timezone,
    # font_list) and let AdsPower auto-generate the rest — which produced
    # values that did NOT match our sync_config (random WebGL vendor,
    # random hardware_concurrency, etc.).
    #
    # These helpers build a COMPLETE fingerprint_config from sync_config
    # so the running AdsPower browser exposes exactly the fingerprint we
    # computed in ProfileSynchronizer.build_full_profile.

    @staticmethod
    def _normalize_os_for_adspower(os_type):
        """
        AdsPower accepts: 'Windows', 'macOS', 'Linux', 'Android', 'iOS'.
        Our internal OS values: 'Windows', 'Mac', 'Linux', 'Android'.
        """
        mapping = {
            'Windows': 'Windows',
            'Mac': 'macOS',
            'Linux': 'Linux',
            'Android': 'Android',
            'iOS': 'iOS',
        }
        return mapping.get(os_type, 'Windows')

    def _build_fingerprint_config(self, profile_config):
        """
        Build the AdsPower fingerprint_config dict from our sync_config.
        Includes: ua, os, language, resolution, screen, timezone, font_list,
        WebGL vendor/renderer, hardware (memory/CPU), touch, color_depth,
        platform, WebRTC mode.
        """
        ua = profile_config.get('ua', '')
        os_type = profile_config.get('os', 'Windows')
        lan = profile_config.get('lan', 'en-US')
        resolution = profile_config.get('resolution', '1920x1080')
        timezone = profile_config.get('timezone', 'America/New_York')

        fingerprint = {}
        if ua:
            fingerprint['ua'] = ua
        if os_type:
            fingerprint['os'] = self._normalize_os_for_adspower(os_type)
        if lan:
            fingerprint['language'] = [lan]
        if resolution and 'x' in str(resolution):
            parts = str(resolution).split('x')
            if len(parts) == 2:
                try:
                    fingerprint['resolution'] = [int(parts[0]), int(parts[1])]
                except ValueError:
                    pass
        if timezone:
            fingerprint['timezone'] = timezone

        font_list = profile_config.get('font_list', '')
        if font_list:
            fingerprint['font_list'] = font_list.split(',')

        # v9.8: WebGL — AdsPower Local API v2 expects `webgl` as an INTEGER
        # 0|1|2|3 (NOT a nested dict, NOT a string):
        #   0 = Real   (use real GPU)
        #   1 = Custom (use webgl_vendor / webgl_renderer top-level keys)
        #   2 = Block  (disable WebGL)
        #   3 = Noise  (perturb real values)
        #
        # History of this fix:
        #   v9.6   : sent `webgl: {mode:'mask', vendor, renderer}` (DICT) → REJECTED
        #   v9.7   : sent `webgl: 1` (int) → still rejected (residual error)
        #   v9.7.1 : added `webgl_image: 0` → made it WORSE (webgl_image is not
        #            a real AdsPower v2 field — was causing the residual error)
        #   v9.8   : REMOVED webgl_image. Only send `webgl` as int 1, plus
        #            webgl_vendor / webgl_renderer as top-level strings.
        #            Added retry-with-fallback in create_profile() and
        #            update_profile() so the bot can still launch even if
        #            AdsPower rejects our webgl value.
        webgl_vendor = profile_config.get('webgl_vendor', '')
        webgl_renderer = profile_config.get('webgl_renderer', '')
        if webgl_vendor or webgl_renderer:
            fingerprint['webgl'] = 1  # 1 = Custom/mask mode (MUST be int, not str/dict)
            # webgl_vendor / webgl_renderer are top-level keys inside
            # fingerprint_config (only consulted when webgl == 1).
            fingerprint['webgl_vendor'] = webgl_vendor or 'Google Inc. (NVIDIA)'
            fingerprint['webgl_renderer'] = webgl_renderer or 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)'

        # v9.8: DEFENSIVE SANITIZATION — force `webgl` to be a valid integer.
        # Handles edge cases where upstream code might inject bad values.
        if 'webgl' in fingerprint:
            v = fingerprint['webgl']
            if isinstance(v, bool):
                fingerprint['webgl'] = 1 if v else 0
            elif isinstance(v, str) and v.strip().isdigit():
                fingerprint['webgl'] = int(v.strip())
            elif isinstance(v, float) and v in (0.0, 1.0, 2.0, 3.0):
                fingerprint['webgl'] = int(v)
            elif not isinstance(v, int) or v not in (0, 1, 2, 3):
                # Invalid type (dict/list/None/out-of-range) — use safe default
                fingerprint['webgl'] = 1

        # v9.8: NEVER send `webgl_image` — it is NOT a valid AdsPower v2 field.
        # If any upstream code injected it, remove it to avoid validation errors.
        fingerprint.pop('webgl_image', None)

        # v9.1: Hardware concurrency & device memory
        if profile_config.get('hardware_concurrency'):
            fingerprint['hardware_concurrency'] = int(profile_config['hardware_concurrency'])
        if profile_config.get('device_memory'):
            # AdsPower expects device_memory in GB (4/8/16/32)
            fingerprint['device_memory'] = int(profile_config['device_memory'])

        # v9.1: Touch points — distinguishes mobile vs desktop
        if 'max_touch_points' in profile_config:
            fingerprint['max_touch_points'] = int(profile_config['max_touch_points'])

        # v9.1: Color depth
        if profile_config.get('color_depth'):
            fingerprint['color_depth'] = int(profile_config['color_depth'])

        # v9.1: Platform string (Win32 / MacIntel / Linux x86_64 / Linux armv81)
        if profile_config.get('platform'):
            fingerprint['platform'] = profile_config['platform']

        # v9.1: WebRTC — must be blocked to prevent IP leak
        webrtc_mode = profile_config.get('webrtc_mode', 'disabled')
        if webrtc_mode == 'disabled':
            # AdsPower's webrtc_config: mode='block' (altered) or 'real'
            fingerprint['webrtc_config'] = {'mode': 'block'}

        return fingerprint

    def _build_proxy_config(self, profile_config):
        """
        Build AdsPower user_proxy_config from sync_config.
        v9.2: Added launch_args to include --ignore-certificate-errors so
        the browser launched by AdsPower will ignore SSL errors when
        connecting through an HTTPS proxy tunnel. This fixes ERR_SSL_PROTOCOL_ERROR.
        """
        proxy_host = profile_config.get('proxy_host', '')
        if not proxy_host:
            return {'proxy_soft': 'no_proxy'}
        proxy_type = profile_config.get('proxy_type', 'http')
        # v9.2: Ensure proxy_type supports HTTPS tunneling.
        # FloppyData and most residential proxies use HTTP protocol
        # but support CONNECT for HTTPS targets. The proxy_type 'http'
        # in AdsPower means the proxy speaks HTTP (including CONNECT).
        config = {
            'proxy_soft': 'other',
            'proxy_type': proxy_type,
            'proxy_host': proxy_host,
            'proxy_port': str(profile_config.get('proxy_port', '')),
            'proxy_user': profile_config.get('proxy_user', ''),
            'proxy_password': profile_config.get('proxy_password', ''),
        }
        return config

    def _build_create_body(self, profile_config, group_id):
        """Build the POST body for /api/v2/browser-profile/create."""
        body = {
            'name': profile_config.get('name', f'bot_profile_{int(time.time())}'),
            'group_id': group_id,
            'user_proxy_config': self._build_proxy_config(profile_config),
            'fingerprint_config': self._build_fingerprint_config(profile_config),
        }
        # v9.2.2: REMOVED launch_args from create body.
        # Putting launch_args in create/update caused AdsPower to crash
        # the browser on start. launch_args is now ONLY sent in
        # start_profile() API call, which applies them at runtime
        # without corrupting the profile configuration.
        return body

    def _build_update_body(self, profile_config):
        """
        Build the POST body for /api/v2/browser-profile/update.
        Includes BOTH proxy and fingerprint_config so reused profiles
        are fully re-synchronized with our sync_config.
        v9.2.2: REMOVED launch_args — caused browser crashes when stored
        in profile config. launch_args is only sent at start_profile() time.
        """
        return {
            'user_proxy_config': self._build_proxy_config(profile_config),
            'fingerprint_config': self._build_fingerprint_config(profile_config),
        }

    def _cleanup_old_profiles(self, keep_count=2):
        """
        Delete old profiles to free up slots when profile limit is reached.
        
        Deletes ALL profiles (not just bot-created) starting from the oldest.
        Adds delays between operations to avoid rate limiting.
        
        Args:
            keep_count: Number of profiles to keep (default: 2)
        
        Returns:
            Number of profiles deleted
        """
        time.sleep(1)  # Rate limit protection
        profiles = self.list_profiles()
        if not profiles:
            return 0
        
        # Collect all profiles with IDs
        all_profiles = []
        for p in profiles:
            pid = p.get('id', p.get('user_id', p.get('profile_id', '')))
            name = p.get('name', 'unnamed')
            if pid:
                all_profiles.append({'id': pid, 'name': name})
        
        if not all_profiles:
            return 0
        
        # Sort by name (oldest timestamp first for bot_profile_*, alphabetical otherwise)
        all_profiles.sort(key=lambda x: x['name'])
        
        # Delete oldest, keep only `keep_count`
        to_delete = all_profiles[:-keep_count] if len(all_profiles) > keep_count else []
        
        deleted = 0
        for p in to_delete:
            logger.info(f"AdsPower: Deleting old profile: {p['name']} (id={p['id']})")
            time.sleep(0.5)  # Rate limit protection
            # v9.5.2: silent=True — old profile mungkin sudah tidak open,
            # "Profile is not open" expected dan tidak perlu noisy.
            self.stop_profile(p['id'], silent=True)
            time.sleep(0.5)
            self.delete_profile(p['id'])
            deleted += 1
            time.sleep(0.5)
        
        if deleted > 0:
            logger.info(f"AdsPower: Cleaned up {deleted} old profiles (kept {len(all_profiles) - deleted})")
        return deleted

    def create_profile(self, profile_config):
        """
        Create or reuse a browser profile via AdsPower Local API v2.

        Strategy (avoids profile limit and rate limiting):
          1. If profile_id configured (via env var, constructor, or Proxy API config) → use it directly
             (but still call update_profile to sync fingerprint & proxy)
          2. Try to reuse existing profile (update proxy + fingerprint config)
          3. If no existing profile, create new
          4. If limit reached, delete ALL old profiles and retry

        v9.1: now sends the FULL fingerprint_config (WebGL vendor/renderer,
        device_memory, hardware_concurrency, max_touch_points, color_depth,
        platform, screen size) — not just ua/os/language/resolution/timezone/
        font_list. Previously AdsPower only got a partial fingerprint and
        fell back to its own random values for the missing fields, causing
        desync between sync_config (what we computed) and what the running
        AdsPower browser actually exposed.
        """
        # If we have a pre-configured profile_id, use it directly
        # v9.1: but still push the fingerprint_config so the reused profile
        # is synchronized with our sync_config (otherwise AdsPower keeps
        # whatever fingerprint the profile was originally created with).
        if self.default_profile_id:
            logger.info(f"AdsPower: Menggunakan profile_id yang sudah dikonfigurasi: {self.default_profile_id}")
            self.update_profile(self.default_profile_id, self._build_update_body(profile_config))
            time.sleep(0.5)
            return self.default_profile_id

        # Auto-detect group_id (required by AdsPower API)
        group_id = self._get_default_group_id()

        # Extract proxy fields
        proxy_host = profile_config.get('proxy_host', '')
        proxy_port = profile_config.get('proxy_port', '')
        proxy_user = profile_config.get('proxy_user', '')
        proxy_password = profile_config.get('proxy_password', '')
        proxy_type = profile_config.get('proxy_type', 'http')

        # =====================================================
        # STEP 1: Try to reuse existing profile (avoids limit)
        # =====================================================
        time.sleep(0.5)  # Rate limit protection
        existing_id = self._find_reusable_profile()
        if existing_id:
            logger.info(f"AdsPower: Reusing existing profile {existing_id} (updating proxy + fingerprint config)")
            # v9.1: update BOTH proxy AND fingerprint_config so the reused
            # profile is fully synchronized with our sync_config.
            self.update_profile(existing_id, self._build_update_body(profile_config))
            time.sleep(0.5)  # Rate limit protection
            return existing_id

        # =====================================================
        # STEP 2: Create new profile
        # =====================================================
        body = self._build_create_body(profile_config, group_id)

        logger.info(f"AdsPower: Creating profile (group_id={group_id}, proxy={proxy_host}:{proxy_port})")
        result = self._post('/api/v2/browser-profile/create', json_data=body)

        # Handle profile limit — cleanup ALL old profiles and retry
        if result and result.get('code') != 0:
            error_msg = result.get('msg', '')
            if 'limit' in error_msg.lower() or 'exceed' in error_msg.lower():
                logger.warning(f"AdsPower: Profile limit reached! Deleting old profiles...")
                deleted = self._cleanup_old_profiles(keep_count=0)
                if deleted > 0:
                    time.sleep(1)  # Rate limit protection
                    logger.info(f"AdsPower: Retrying profile creation after cleanup...")
                    result = self._post('/api/v2/browser-profile/create', json_data=body)

        # v9.8: Handle webgl error — retry with fallback webgl values
        if result and result.get('code') != 0:
            err_msg = result.get('msg', '').lower()
            if 'webgl' in err_msg and ('must be' in err_msg or '0,1,2,3' in err_msg):
                logger.warning(
                    f"AdsPower: create_profile failed with webgl error. "
                    f"Trying fallback variants..."
                )
                fp_original = body.get('fingerprint_config', {})
                fp_variants = [
                    ('webgl=0 (Real)', self._make_webgl_variant(fp_original, webgl_value=0, drop_vendor=False)),
                    ('webgl removed', self._make_webgl_variant(fp_original, webgl_value=None, drop_vendor=True)),
                ]
                for label, fp_variant in fp_variants:
                    logger.info(f"AdsPower: Retrying create with fallback: {label}")
                    fallback_body = dict(body)
                    fallback_body['fingerprint_config'] = fp_variant
                    time.sleep(0.5)
                    result = self._post('/api/v2/browser-profile/create', json_data=fallback_body)
                    if result and result.get('code') == 0:
                        logger.info(f"AdsPower: create succeeded with fallback: {label}")
                        break
                    # If still webgl error, try next variant; otherwise stop
                    next_err = (result.get('msg', '') if result else '').lower()
                    if not ('webgl' in next_err and ('must be' in next_err or '0,1,2,3' in next_err)):
                        break

        if result:
            if result.get('code') == 0:
                data = result.get('data', {})
                profile_id = ''
                
                if isinstance(data, dict):
                    for key in ['id', 'user_id', 'profile_id', 'browser_id', 'serial_number']:
                        profile_id = data.get(key, '')
                        if profile_id:
                            break
                    if not profile_id and 'profile' in data:
                        profile_data = data.get('profile', {})
                        if isinstance(profile_data, dict):
                            for key in ['id', 'user_id', 'profile_id']:
                                profile_id = profile_data.get(key, '')
                                if profile_id:
                                    break
                elif isinstance(data, str):
                    profile_id = data
                elif isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    if isinstance(first, dict):
                        profile_id = first.get('id', first.get('user_id', first.get('profile_id', '')))
                    elif isinstance(first, str):
                        profile_id = first
                
                if profile_id:
                    logger.info(f"AdsPower: Profil berhasil dibuat: {profile_id}")
                    return profile_id
                
                logger.warning(
                    f"AdsPower: create returned success but no profile_id in data! "
                    f"data: {json.dumps(data, default=str)[:300]}"
                )
        
        error_msg = result.get('msg', 'unknown') if result else 'no response'
        logger.error(
            f"AdsPower: Gagal membuat profil baru. Error: {error_msg}\n"
            f"Solusi: Set 'profile_id' via env var ADSPOWER_PROFILE_ID atau Proxy API config.\n"
            f"  1. Buka AdsPower, buat profil baru secara manual\n"
            f"  2. Salin ID profil dari daftar profil\n"
            f"  3. Set profile_id via load_proxy_config(profile_id='ID_PROFIL')"
        )
        return None

    def _find_reusable_profile(self):
        """
        Find an existing profile that can be reused.
        Prefers profiles that are not currently running.
        
        Returns:
            profile_id string, or None if no profile found
        """
        profiles = self.list_profiles()
        if not profiles:
            return None
        
        # Use the first available profile
        for p in profiles:
            pid = p.get('id', p.get('user_id', p.get('profile_id', '')))
            if pid:
                logger.info(f"AdsPower: Found reusable profile: {p.get('name', 'unnamed')} (id={pid})")
                return pid
        
        return None

    def list_profiles(self, page=1, limit=200):
        """
        List existing browser profiles from AdsPower (v2 API).
        """
        body = {'page': page, 'limit': limit}
        result = self._post('/api/v2/browser-profile/list', json_data=body)
        if result and result.get('code') == 0:
            profiles = result.get('data', {}).get('list', [])
            logger.info(f"AdsPower has {len(profiles)} profiles")
            return profiles
        
        logger.warning("Failed to list AdsPower profiles")
        return []

    def stop_profile(self, profile_id, silent=False):
        """
        Stop a running browser profile (v2 API).

        v9.5.1: "Profile is not open" adalah response NORMAL bila profile
        memang sudah closed (mis. setelah start gagal). Jangan log sebagai
        error — log di level debug saja agar tidak menggangu.

        v9.5.2: Teruskan silent=True ke _post() sebagai silent_errors=True
        agar _request() JUGA skip warning log di source-nya. Sebelumnya,
        _request() sudah log warning "AdsPower API error: code=-1, msg=Profile
        is not open" SEBELUM return ke stop_profile(), sehingga flag silent
        di stop_profile() tidak efektif.

        Args:
            profile_id: AdsPower profile ID
            silent: bila True, suppress SEMUA logging (warning dari _request
                    maupun dari stop_profile sendiri). Untuk pre-stop cleanup.
        """
        try:
            result = self._post('/api/v2/browser-profile/stop',
                                json_data={'profile_id': profile_id},
                                silent_errors=silent)
        except Exception as e:
            if not silent:
                logger.warning(f"AdsPower: stop_profile exception ({e})")
            return False

        if not result:
            if not silent:
                logger.warning(f"AdsPower: stop_profile {profile_id} returned None")
            return False

        code = result.get('code', -1)
        msg = (result.get('msg') or '').lower()

        # code=0 → success
        if code == 0:
            if not silent:
                logger.info(f"AdsPower: profile {profile_id} stopped OK")
            return True

        # "Profile is not open" / "not running" → idempotent: profile
        # memang sudah closed. Treated as success.
        if 'not open' in msg or 'not running' in msg or 'not started' in msg:
            if not silent:
                logger.debug(f"AdsPower: profile {profile_id} already closed (idempotent stop)")
            return True

        # Other error → log (kecuali silent)
        if not silent:
            logger.warning(f"AdsPower: stop_profile {profile_id} failed: code={code} msg={result.get('msg')}")
        return False

    def clear_profile_cache(self, profile_id):
        """
        Clear browser cache untuk profile (v2 API).

        AdsPower v2 menyediakan endpoint:
          POST /api/v2/browser-profile/clear-cache
          body: {"profile_id": "..."}

        Dipakai sebagai recovery bila start_profile gagal dengan
        "Failed to start browser" — kadang cache Chromium corrupt dan
        clearing cache + retry start bisa recover.
        """
        try:
            result = self._post('/api/v2/browser-profile/clear-cache',
                                json_data={'profile_id': profile_id})
            if result and result.get('code') == 0:
                logger.info(f"AdsPower: cache cleared for profile {profile_id}")
                return True
            logger.warning(f"AdsPower: clear-cache failed: {result}")
        except Exception as e:
            logger.warning(f"AdsPower: clear-cache exception ({e})")
        return False

    def start_profile(self, profile_id, headless=False):
        """
        Start a browser profile and get WebSocket debug URL (v2 API).

        v9.5.1 RESILIENCE UPGRADE:
          Sebelumnya, "Failed to start browser" (code=-1) langsung
          menyebabkan fallback ke Patchright. Penyebab umum error ini:
            1. Profile stuck di state "open" dari run sebelumnya yang crash
               → AdsPower mengira profile masih running, padahal browser
               process sudah mati.
            2. AdsPower app sedang busy/syncing → perlu retry setelah delay.
            3. Browser cache Chromium corrupt → perlu clear-cache + retry.
            4. Concurrent profile limit reached → perlu wait + retry.
            5. launch_args bermasalah → perlu coba variant tanpa args.

          Strategi baru (3 attempt dengan progressive recovery):
            Attempt 1: pre-stop (silent) → start with launch_args
            Attempt 2: start WITHOUT launch_args (kadang --ignore-certificate-
                       errors menyebabkan crash di certain AdsPower versions)
            Attempt 3: clear-cache → wait 2s → start with launch_args

          Setiap attempt punya backoff 2-4 detik untuk kasih waktu AdsPower
          me-release resources.
        """
        # ============================================================
        # v9.5.1: PRE-STOP cleanup — clear stale "open" state.
        # Profile yang crash sebelumnya sering masih dianggap "open"
        # oleh AdsPower. Stop dulu (silent=True agar tidak noisy).
        # ============================================================
        logger.info(f"AdsPower: pre-stop cleanup for profile {profile_id}")
        self.stop_profile(profile_id, silent=True)
        time.sleep(1.0)  # beri waktu AdsPower me-release resources

        # ============================================================
        # Helper untuk parse ws_endpoint dari result
        # ============================================================
        def _extract_ws(result_data):
            ws = result_data.get('ws', {})
            if isinstance(ws, dict):
                ws = (ws.get('puppeteer', '') or ws.get('selenium', '')
                      or ws.get('cdp', ''))
            elif not isinstance(ws, str):
                ws = str(ws) if ws else ''
            return ws

        # ============================================================
        # Attempt 1: start WITH launch_args (preferred — handles proxy SSL)
        # ============================================================
        body_with_args = {
            'profile_id': profile_id,
            'ip_tab': 0,
            'headless': 1 if headless else 0,
            'launch_args': ['--ignore-certificate-errors'],
        }
        result = self._post('/api/v2/browser-profile/start', json_data=body_with_args)
        if result and result.get('code') == 0:
            data = result.get('data', {}) or {}
            ws_endpoint = _extract_ws(data)
            if ws_endpoint:
                logger.info(f"AdsPower: profile {profile_id} started (attempt 1, with launch_args)")
                return {
                    'ws_endpoint': ws_endpoint,
                    'debug_port': data.get('debug_port', ''),
                    'profile_id': profile_id,
                }

        err_msg_1 = (result.get('msg', '') if result else 'no response')
        logger.warning(
            f"AdsPower: start attempt 1 FAILED (profile={profile_id}): "
            f"code={result.get('code') if result else 'None'}, msg={err_msg_1}"
        )

        # ============================================================
        # Attempt 2: start WITHOUT launch_args
        # (kadang --ignore-certificate-errors menyebabkan crash)
        # ============================================================
        time.sleep(2.0)  # backoff before retry
        body_no_args = {
            'profile_id': profile_id,
            'ip_tab': 0,
            'headless': 1 if headless else 0,
        }
        result = self._post('/api/v2/browser-profile/start', json_data=body_no_args)
        if result and result.get('code') == 0:
            data = result.get('data', {}) or {}
            ws_endpoint = _extract_ws(data)
            if ws_endpoint:
                logger.info(f"AdsPower: profile {profile_id} started (attempt 2, no launch_args)")
                return {
                    'ws_endpoint': ws_endpoint,
                    'debug_port': data.get('debug_port', ''),
                    'profile_id': profile_id,
                }

        err_msg_2 = (result.get('msg', '') if result else 'no response')
        logger.warning(
            f"AdsPower: start attempt 2 FAILED (no launch_args): "
            f"code={result.get('code') if result else 'None'}, msg={err_msg_2}"
        )

        # ============================================================
        # Attempt 3: clear-cache + retry with launch_args
        # (cache Chromium corrupt → clear + restart)
        # ============================================================
        time.sleep(2.0)
        logger.info(f"AdsPower: attempt 3 — clearing cache for {profile_id}")
        self.clear_profile_cache(profile_id)
        time.sleep(2.0)  # beri waktu clear-cache selesai

        # Pre-stop lagi setelah clear-cache (kadang clear-cache membuka
        # locks yang menyebabkan profile dianggap still open)
        self.stop_profile(profile_id, silent=True)
        time.sleep(1.0)

        result = self._post('/api/v2/browser-profile/start', json_data=body_with_args)
        if result and result.get('code') == 0:
            data = result.get('data', {}) or {}
            ws_endpoint = _extract_ws(data)
            if ws_endpoint:
                logger.info(f"AdsPower: profile {profile_id} started (attempt 3, after clear-cache)")
                return {
                    'ws_endpoint': ws_endpoint,
                    'debug_port': data.get('debug_port', ''),
                    'profile_id': profile_id,
                }

        err_msg_3 = (result.get('msg', '') if result else 'no response')
        logger.error(
            f"AdsPower: ALL 3 start attempts FAILED for profile {profile_id}:\n"
            f"  attempt 1 (with launch_args):    {err_msg_1}\n"
            f"  attempt 2 (without launch_args): {err_msg_2}\n"
            f"  attempt 3 (after clear-cache):   {err_msg_3}\n"
            f"Kemungkinan penyebab:\n"
            f"  - Profile corrupt (delete & recreate)\n"
            f"  - Concurrent profile limit reached (close other profiles)\n"
            f"  - AdsPower app not responding (restart AdsPower)\n"
            f"  - Proxy unreachable (check proxy config)\n"
            f"Falling back to Patchright."
        )
        return None

    def delete_profile(self, profile_id):
        """Delete a browser profile (v2 API)."""
        self._post('/api/v2/browser-profile/delete', json_data={'profile_id': profile_id})

    def update_profile(self, profile_id, profile_config):
        """
        Update an existing profile's configuration (v2 API).

        v9.9 (BUG FIX #8): Pakai cached webgl value dari class-level cache.
        Hanya retry jika cache belum ada (cold start). Setelah webgl value
        yang berhasil ditemukan, semua update_profile berikutnya pakai
        value tersebut — menghemat 3-6 detik per user.
        """
        profile_config['profile_id'] = profile_id

        fp_original = profile_config.get('fingerprint_config', {})
        import copy

        # BUG FIX #8: Jika cache sudah ada, pakai langsung tanpa retry.
        if AdsPowerClient._cached_webgl_value is not None or AdsPowerClient._cached_webgl_drop_vendor:
            cached_fp = self._make_webgl_variant(
                fp_original,
                webgl_value=AdsPowerClient._cached_webgl_value,
                drop_vendor=AdsPowerClient._cached_webgl_drop_vendor,
            )
            body = dict(profile_config)
            body['fingerprint_config'] = cached_fp
            result = self._post('/api/v2/browser-profile/update', json_data=body)
            if result and result.get('code') == 0:
                return result
            # Jika gagal dengan cached value, reset cache dan fallback ke retry
            err_msg = (result.get('msg', '') if result else '').lower()
            if not ('webgl' in err_msg and ('must be' in err_msg or '0,1,2,3' in err_msg)):
                # Bukan webgl error — return result apa adanya
                return result
            logger.warning(f"AdsPower: cached webgl value gagal, fallback ke retry sequential...")

        # Cold-start: coba 3 variant dan cache yang berhasil
        fp_variants = [
            ('webgl=1 (Custom)', fp_original, 1, False),
            ('webgl=0 (Real)', self._make_webgl_variant(fp_original, webgl_value=0, drop_vendor=False), 0, False),
            ('webgl removed', self._make_webgl_variant(fp_original, webgl_value=None, drop_vendor=True), None, True),
        ]

        last_result = None
        for label, fp_variant, webgl_val, drop_vendor in fp_variants:
            body = dict(profile_config)
            body['fingerprint_config'] = fp_variant
            result = self._post('/api/v2/browser-profile/update', json_data=body)
            last_result = result

            if result and result.get('code') == 0:
                # BUG FIX #8: cache webgl value yang berhasil
                AdsPowerClient._cached_webgl_value = webgl_val
                AdsPowerClient._cached_webgl_drop_vendor = drop_vendor
                if label != 'webgl=1 (Custom)':
                    logger.info(f"AdsPower update_profile succeeded with fallback: {label} (cached for future calls)")
                else:
                    logger.info(f"AdsPower update_profile succeeded with webgl=1 (cached for future calls)")
                return result

            err_msg = (result.get('msg', '') if result else '').lower()
            if 'webgl' in err_msg and ('must be' in err_msg or '0,1,2,3' in err_msg):
                logger.warning(
                    f"AdsPower update_profile failed with webgl error using '{label}'. "
                    f"Trying next fallback..."
                )
                continue
            else:
                return result

        logger.error(
            f"AdsPower update_profile: ALL WebGL fallback variants failed. "
            f"Profile {profile_id} was NOT updated. Last error: "
            f"{last_result.get('msg', 'unknown') if last_result else 'no response'}"
        )
        return last_result

    @staticmethod
    def _make_webgl_variant(fp_config, webgl_value=None, drop_vendor=False):
        """
        Create a copy of fingerprint_config with a different webgl setting.
        Used by update_profile / create_profile retry logic.

        Args:
            fp_config: original fingerprint_config dict
            webgl_value: new webgl value (int 0/1/2/3), or None to remove the key
            drop_vendor: if True, also remove webgl_vendor / webgl_renderer
        """
        import copy
        variant = copy.deepcopy(fp_config)
        if webgl_value is None:
            variant.pop('webgl', None)
        else:
            variant['webgl'] = int(webgl_value)
        if drop_vendor:
            variant.pop('webgl_vendor', None)
            variant.pop('webgl_renderer', None)
            variant.pop('webgl_image', None)  # v9.8: never send webgl_image
        else:
            # Always strip webgl_image even when keeping vendor/renderer
            variant.pop('webgl_image', None)
        return variant

    def get_opened_browsers(self):
        """Get all currently opened browser profiles."""
        return self._get('/api/v2/browser-profile/active')

    def get_active_status(self, profile_id):
        """Check if a specific profile is currently active."""
        return self._get('/api/v2/browser-profile/active', params={'profile_id': profile_id})


class MultiloginClient:
    """
    Multilogin Local API Client.
    Docs: https://docs.multilogin.com/l/en/multilogin-local-api
    
    Multilogin provides deep Chromium modifications:
    - Binary-level navigator.webdriver masking
    - Hardware WebGL fingerprint emulation
    - OS-consistent font injection
    - Canvas/Audio fingerprint noise at engine level
    """

    DEFAULT_API_BASE = 'http://127.0.0.1'
    DEFAULT_PORT = 45001

    def __init__(self, api_base=None, port=None):
        self.api_base = api_base or os.environ.get('MULTILOGIN_API_BASE', self.DEFAULT_API_BASE)
        self.port = port or int(os.environ.get('MULTILOGIN_PORT', self.DEFAULT_PORT))
        self.base_url = f"{self.api_base}:{self.port}"

    def check_status(self):
        try:
            resp = http_requests.get(f"{self.base_url}/api/v2/profiles", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def create_profile(self, profile_config):
        payload = {
            'name': profile_config.get('name', f'bot_{int(time.time())}'),
            'os': profile_config.get('os', 'win'),
            'browserType': 'mimic',
            'proxy': {
                'type': profile_config.get('proxy_type', 'http'),
                'host': profile_config.get('proxy_host', ''),
                'port': profile_config.get('proxy_port', 0),
                'username': profile_config.get('proxy_user', ''),
                'password': profile_config.get('proxy_password', ''),
            },
            'navigator': {
                'userAgent': profile_config.get('ua', ''),
                'language': profile_config.get('lan', 'en-US'),
                'resolution': profile_config.get('resolution', '1920x1080'),
                'platform': profile_config.get('platform', 'Win32'),
            },
            'timezone': {
                'mode': 'manual',
                'value': profile_config.get('timezone', 'America/New_York'),
            },
            'fonts': {
                'mode': 'custom',
                'families': profile_config.get('font_list', '').split(',') if profile_config.get('font_list') else [],
            },
            'webgl': {
                'mode': 'mask',
                'vendor': profile_config.get('webgl_vendor', 'Google Inc. (NVIDIA)'),
                'renderer': profile_config.get('webgl_renderer', 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)'),
            },
        }
        try:
            resp = http_requests.post(f"{self.base_url}/api/v2/profiles", json=payload, timeout=15)
            data = resp.json()
            return data.get('id')
        except Exception as e:
            logger.error(f"Multilogin create failed: {e}")
            return None

    def start_profile(self, profile_id, headless=False):
        try:
            resp = http_requests.get(
                f"{self.base_url}/api/v2/profiles/{profile_id}/start",
                params={'headless': str(headless).lower()},
                timeout=30
            )
            data = resp.json()
            ws_endpoint = data.get('ws_endpoint', '')
            return {
                'ws_endpoint': ws_endpoint,
                'profile_id': profile_id,
            }
        except Exception as e:
            logger.error(f"Multilogin start failed: {e}")
            return None

    def stop_profile(self, profile_id):
        try:
            http_requests.get(f"{self.base_url}/api/v2/profiles/{profile_id}/stop", timeout=10)
        except Exception:
            pass

    def delete_profile(self, profile_id):
        try:
            http_requests.delete(f"{self.base_url}/api/v2/profiles/{profile_id}", timeout=10)
        except Exception:
            pass


class DolphinAntyClient:
    """
    Dolphin{anty} Local API Client.
    Docs: https://dolphin-anty-docs.readme.io/docs/local-api
    
    Dolphin{anty} provides:
    - Binary-level browser fingerprint masking
    - Native WebGL & Canvas emulation
    - OS-consistent font rendering
    - Proxy integration with WebRTC/DNS leak prevention built-in
    """

    DEFAULT_API_BASE = 'http://127.0.0.1'
    DEFAULT_PORT = 3001

    def __init__(self, api_base=None, port=None):
        self.api_base = api_base or os.environ.get('DOLPHIN_API_BASE', self.DEFAULT_API_BASE)
        self.port = port or int(os.environ.get('DOLPHIN_PORT', self.DEFAULT_PORT))
        self.base_url = f"{self.api_base}:{self.port}"

    def check_status(self):
        try:
            resp = http_requests.get(f"{self.base_url}/v1.0/browserprofiles", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def create_profile(self, profile_config):
        payload = {
            'name': profile_config.get('name', f'bot_{int(time.time())}'),
            'os': profile_config.get('os', 'win'),
            'browserType': 'antidetect',
            'proxy': {
                'type': profile_config.get('proxy_type', 'http'),
                'host': profile_config.get('proxy_host', ''),
                'port': profile_config.get('proxy_port', 0),
                'username': profile_config.get('proxy_user', ''),
                'password': profile_config.get('proxy_password', ''),
            },
            'navigator': {
                'userAgent': profile_config.get('ua', ''),
                'language': profile_config.get('lan', 'en-US'),
                'resolution': profile_config.get('resolution', '1920x1080'),
                'platform': profile_config.get('platform', 'Win32'),
            },
            'timezone': profile_config.get('timezone', 'America/New_York'),
            'webgl': {
                'vendor': profile_config.get('webgl_vendor', 'Google Inc. (NVIDIA)'),
                'renderer': profile_config.get('webgl_renderer', 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti)'),
            },
        }
        try:
            resp = http_requests.post(f"{self.base_url}/v1.0/browserprofiles", json=payload, timeout=15)
            data = resp.json()
            return data.get('id')
        except Exception as e:
            logger.error(f"Dolphin create failed: {e}")
            return None

    def start_profile(self, profile_id, headless=False):
        try:
            resp = http_requests.get(
                f"{self.base_url}/v1.0/browserprofiles/{profile_id}/start",
                params={'headless': str(headless).lower()},
                timeout=30
            )
            data = resp.json()
            ws_endpoint = data.get('ws_endpoint', '')
            automation = data.get('automation', {})
            if not ws_endpoint and automation:
                ws_endpoint = automation.get('ws_endpoint', '')
            return {
                'ws_endpoint': ws_endpoint,
                'profile_id': profile_id,
            }
        except Exception as e:
            logger.error(f"Dolphin start failed: {e}")
            return None

    def stop_profile(self, profile_id):
        try:
            http_requests.get(f"{self.base_url}/v1.0/browserprofiles/{profile_id}/stop", timeout=10)
        except Exception:
            pass

    def delete_profile(self, profile_id):
        try:
            http_requests.delete(f"{self.base_url}/v1.0/browserprofiles/{profile_id}", timeout=10)
        except Exception:
            pass


# ====================================================================
# Anti-Detect Manager — Unified Interface
# ====================================================================

class AntiDetectManager:
    """
    Unified manager for anti-detect browser profiles.
    
    This is the RECOMMENDATION #1 implementation:
    "Gunakan Peramban Anti-Deteksi Asli (Anti-detect Browser)"
    
    Instead of faking properties via JS proxies that leave detectable artifacts,
    this connects to anti-detect browsers that modify Chromium at the C++ level,
    making properties like navigator.webdriver naturally undetectable.
    
    Usage (new — with load_proxy_config):
        from antidetect_browser import load_proxy_config, AntiDetectManager

        config = load_proxy_config(page_size=5)
        adspower_cfg = config['adspower']
        proxies = config['proxies']

        manager = AntiDetectManager(
            mode="antidetect",
            browser_type="adspower",
            adspower_config=adspower_cfg,
        )

        for proxy_entry in proxies:
            profile_config = manager.build_profile_config(
                proxy=proxy_entry,
                user_profile=user_profile_dict,
            )
            session = manager.create_and_start(profile_config)
            session.page.goto("https://example.com")
            manager.close_and_cleanup(session)

    Usage (legacy — manual credentials):
        manager = AntiDetectManager(mode="antidetect", browser_type="adspower",
                                    api_key="...")

        # Create a profile with full synchronization
        profile_config = manager.build_profile_config(
            proxy=proxy_entry,
            user_profile=user_profile_dict,
        )
        session = manager.create_and_start(profile_config)

        # session.page is a Playwright page — use it normally
        session.page.goto("https://example.com")

        # Cleanup
        manager.close_and_cleanup(session)
    """

    def __init__(self, mode="antidetect", browser_type="adspower", api_key=None,
                 profile_id=None, group_id=None, adspower_config=None):
        """
        Args:
            mode: "antidetect" (use anti-detect browser) or "patchright" (legacy fallback)
            browser_type: "adspower", "multilogin", or "dolphin"
            api_key: API key untuk AdsPower Local API (diteruskan sebagai Bearer header)
            profile_id: ID profil AdsPower yang sudah ada (opsional, jika create tidak tersedia)
            group_id: ID grup AdsPower (opsional, auto-detect jika kosong)
            adspower_config: Dict dari load_proxy_config()['adspower'] — berisi
                             api_key, mode, base_url, port, profile_id, group_id.
                             Jika diberikan, meng-override api_key/profile_id/group_id
                             yang terpisah.
        """
        self.mode = mode
        self.browser_type = browser_type.lower()
        self._adspower_config = adspower_config  # raw config for _init_client
        self._sessions = []

        # Resolve AdsPower credentials with 3-tier fallback:
        #   adspower_config > explicit arg > env var > ADSPOWER_DEFAULTS
        if adspower_config:
            self.api_key = adspower_config.get('api_key', '') or api_key or os.environ.get('ADSPOWER_API_KEY', '') or ADSPOWER_DEFAULTS['api_key']
            self.profile_id = adspower_config.get('profile_id', '') or profile_id or os.environ.get('ADSPOWER_PROFILE_ID', '') or ADSPOWER_DEFAULTS['profile_id']
            self.group_id = adspower_config.get('group_id', '') or group_id or os.environ.get('ADSPOWER_GROUP_ID', '') or ADSPOWER_DEFAULTS['group_id']
            self._adspower_base_url = adspower_config.get('base_url', '') or os.environ.get('ADSPOWER_API_BASE', '') or ADSPOWER_DEFAULTS['base_url']
            self._adspower_port = adspower_config.get('port', 0) or (int(os.environ['ADSPOWER_PORT']) if 'ADSPOWER_PORT' in os.environ else 0) or ADSPOWER_DEFAULTS['port']
        else:
            self.api_key = api_key or os.environ.get('ADSPOWER_API_KEY', '') or ADSPOWER_DEFAULTS['api_key']
            self.profile_id = profile_id or os.environ.get('ADSPOWER_PROFILE_ID', '') or ADSPOWER_DEFAULTS['profile_id']
            self.group_id = group_id or os.environ.get('ADSPOWER_GROUP_ID', '') or ADSPOWER_DEFAULTS['group_id']
            self._adspower_base_url = os.environ.get('ADSPOWER_API_BASE', '') or ADSPOWER_DEFAULTS['base_url']
            self._adspower_port = (int(os.environ['ADSPOWER_PORT']) if 'ADSPOWER_PORT' in os.environ else 0) or ADSPOWER_DEFAULTS['port']

        if self.mode == "antidetect":
            self._init_client()
        else:
            logger.info("Running in Patchright fallback mode (legacy)")

    def _init_client(self):
        """Initialize the anti-detect browser API client."""
        if self.browser_type == 'adspower':
            self._client = AdsPowerClient(
                api_key=self.api_key,
                api_base=self._adspower_base_url,
                port=self._adspower_port,
                profile_id=self.profile_id,
                group_id=self.group_id,
            )
        elif self.browser_type == 'multilogin':
            self._client = MultiloginClient()
        elif self.browser_type == 'dolphin':
            self._client = DolphinAntyClient()
        else:
            logger.error(f"Unknown browser type: {self.browser_type}. Falling back to Patchright.")
            self.mode = "patchright"
            return

        if not self._client.check_status():
            logger.warning(f"{self.browser_type} is not running or API not accessible. Falling back to Patchright.")
            self.mode = "patchright"
            self._client = None
        else:
            logger.info(f"Connected to {self.browser_type} anti-detect browser successfully (mode={getattr(self._client, 'mode', 'unknown')}).")

    @property
    def is_antidetect_mode(self):
        return self.mode == "antidetect" and self._client is not None

    def build_profile_config(self, proxy, user_profile):
        """
        Build a complete profile configuration with total synchronization.
        
        This is the RECOMMENDATION #2 implementation:
        "Sinkronisasi Total Profil" — ensures OS, User-Agent, font fingerprint,
        screen coordinates, timezone, and TCP/IP parameters (TTL) are all
        dynamically synchronized to match the geographic location and
        characteristics of the residential proxy IP being used.
        
        Args:
            proxy: Proxy entry dict from bot_v6.py
            user_profile: User profile dict from make_user_profile()
        
        Returns:
            Complete profile_config dict for anti-detect browser API
        """
        from profile_sync import ProfileSynchronizer
        sync = ProfileSynchronizer()
        return sync.build_full_profile(proxy, user_profile)

    def _try_use_existing_profile(self, profile_config=None):
        """
        If creating a new profile fails, try to find and use an existing
        profile in AdsPower. Updates its proxy AND fingerprint config
        before returning.

        v9.1: previously this method only updated user_proxy_config, leaving
        the reused profile's UA/OS/fonts/WebGL/etc. as whatever it was
        originally created with. That broke synchronization: sync_config
        said "Android Pixel 8" but the reused AdsPower profile still had
        a Windows fingerprint from a previous run.

        Returns:
            profile_id string, or None if no available profile found
        """
        # Use _find_reusable_profile which handles list_profiles
        existing_id = self._client._find_reusable_profile() if hasattr(self._client, '_find_reusable_profile') else None

        if existing_id:
            # v9.1: update BOTH proxy and fingerprint config for the reused
            # profile — use the same _build_update_body helper used by
            # create_profile so the fingerprint is fully synchronized.
            # v9.3: FIX dead-code bug — sebelumnya `if` dan `elif` punya
            # kondisi identik (`profile_config and hasattr(update_profile)`),
            # sehingga branch Multilogin/Dolphin tidak pernah dijalankan.
            # Sekarang elif mengecek keberadaan _build_update_body secara
            # terpisah.
            if profile_config and hasattr(self._client, 'update_profile') and hasattr(self._client, '_build_update_body'):
                logger.info(f"AdsPower: Re-syncing existing profile {existing_id} with full sync_config (proxy + fingerprint)")
                self._client.update_profile(existing_id,
                                            self._client._build_update_body(profile_config))
                time.sleep(0.5)
            elif profile_config and hasattr(self._client, 'update_profile'):
                # Fallback path for clients without _build_update_body (Multilogin / Dolphin)
                proxy_host = profile_config.get('proxy_host', '')
                if proxy_host:
                    self._client.update_profile(existing_id, {
                        'user_proxy_config': {
                            'proxy_soft': 'other',
                            'proxy_type': profile_config.get('proxy_type', 'http'),
                            'proxy_host': proxy_host,
                            'proxy_port': str(profile_config.get('proxy_port', '')),
                            'proxy_user': profile_config.get('proxy_user', ''),
                            'proxy_password': profile_config.get('proxy_password', ''),
                        },
                    })
                    time.sleep(0.5)
            return existing_id

        # Fallback: try list_profiles directly
        if hasattr(self._client, 'list_profiles'):
            time.sleep(0.5)  # Rate limit protection
            profiles = self._client.list_profiles()
            if profiles:
                for p in profiles:
                    profile_id = p.get('id', p.get('user_id', p.get('profile_id', '')))
                    if profile_id:
                        logger.info(f"AdsPower: Menggunakan profil yang sudah ada: {p.get('name', 'unnamed')} (id={profile_id})")
                        # v9.1: also re-sync fingerprint when recovered via list_profiles
                        if profile_config and hasattr(self._client, '_build_update_body'):
                            self._client.update_profile(profile_id,
                                                        self._client._build_update_body(profile_config))
                            time.sleep(0.5)
                        return profile_id

        logger.warning("AdsPower: Tidak ada profil yang bisa digunakan.")
        return None

    def create_and_start(self, profile_config, pw=None):
        """
        Create a browser profile, start it, and connect via Playwright.
        
        Args:
            profile_config: Full profile configuration dict
            pw: Playwright instance (from sync_playwright())
        
        Returns:
            Session dict with keys:
              - page: Playwright Page object
              - context: Playwright BrowserContext
              - browser: Playwright Browser
              - profile_id: Anti-detect profile ID
              - ws_endpoint: WebSocket debug URL
              - mode: "antidetect" or "patchright"
        """
        if self.is_antidetect_mode:
            return self._create_antidetect_session(profile_config, pw)
        else:
            return self._create_patchright_session(profile_config, pw)

    def _create_antidetect_session(self, profile_config, pw):
        """Create session using anti-detect browser.

        v9.9 (BUG FIX #6): Optimasi startup time dari ~49s → ~10s.
          1. Cache profile_id di class-level (create_profile hanya sekali)
          2. Reuse profile yang sudah ada — cukup update_profile untuk
             sync proxy + fingerprint, tanpa list_profiles() tiap kali
          3. Kurangi time.sleep(3) → time.sleep(1) setelah start_profile
             (CDP connect sudah punya retry-nya sendiri)
        """
        # BUG FIX #6: Cek cache dulu — jika profile_id sudah ada, skip
        # create_profile() dan langsung update + start.
        profile_id = None
        if isinstance(self._client, AdsPowerClient) and AdsPowerClient._cached_profile_id:
            profile_id = AdsPowerClient._cached_profile_id
            logger.info(f"AdsPower: REUSING cached profile_id={profile_id} (skipping create)")
            # Update fingerprint + proxy untuk user baru
            try:
                self._client.update_profile(profile_id, self._client._build_update_body(profile_config))
                time.sleep(0.3)  # rate limit pendek
            except Exception as e:
                logger.warning(f"AdsPower: cached profile update gagal ({e}), akan create baru")
                profile_id = None

        if not profile_id:
            # Create profile baru
            profile_id = self._client.create_profile(profile_config)
            # Cache profile_id untuk user berikutnya
            if profile_id and isinstance(self._client, AdsPowerClient):
                AdsPowerClient._cached_profile_id = profile_id
                logger.info(f"AdsPower: profile_id {profile_id} CACHED untuk reuse")

        # If create fails, try using an existing profile instead
        if not profile_id:
            logger.warning("Failed to create new profile. Trying to use existing profile...")
            profile_id = self._try_use_existing_profile(profile_config)
            if not profile_id:
                logger.error("No existing profile available either. Falling back to Patchright.")
                return self._create_patchright_session(profile_config, pw)

        # Start profile and get WebSocket endpoint
        start_result = self._client.start_profile(profile_id, headless=False)

        # v9.5.1: Bila start gagal DAN profile_id berasal dari cache,
        # coba delete cached profile + create fresh sebelum fallback ke
        # patchright. Profile cache kadang corrupt setelah banyak run.
        if (not start_result or not start_result.get('ws_endpoint')) and \
           isinstance(self._client, AdsPowerClient) and \
           AdsPowerClient._cached_profile_id == profile_id:
            logger.warning(
                f"AdsPower: cached profile {profile_id} failed to start. "
                f"Attempting DELETE + CREATE FRESH before Patchright fallback..."
            )
            # Stop & delete corrupt cached profile
            self._client.stop_profile(profile_id, silent=True)
            try:
                self._client.delete_profile(profile_id)
                logger.info(f"AdsPower: deleted corrupt cached profile {profile_id}")
            except Exception as e:
                logger.warning(f"AdsPower: delete_profile failed ({e}) — continuing")
            AdsPowerClient._cached_profile_id = None

            # Create fresh profile and try start once more
            try:
                fresh_profile_id = self._client.create_profile(profile_config)
                if fresh_profile_id:
                    AdsPowerClient._cached_profile_id = fresh_profile_id
                    logger.info(f"AdsPower: created fresh profile {fresh_profile_id}")
                    # start_profile lagi (sudah termasuk 3-attempt retry)
                    start_result = self._client.start_profile(
                        fresh_profile_id, headless=False
                    )
                    if start_result and start_result.get('ws_endpoint'):
                        profile_id = fresh_profile_id
                        logger.info(f"AdsPower: fresh profile started successfully!")
            except Exception as e:
                logger.warning(f"AdsPower: fresh create+start failed ({e})")

        if not start_result or not start_result.get('ws_endpoint'):
            logger.error("Failed to start anti-detect profile. Falling back.")
            # v9.5.1: stop silently — start_profile() sudah melakukan
            # 3 attempts + clear-cache. Stop di sini hanya cleanup,
            # tidak perlu noisy lagi.
            self._client.stop_profile(profile_id, silent=True)
            # Reset cache jika start gagal — profile mungkin corrupt
            if isinstance(self._client, AdsPowerClient):
                AdsPowerClient._cached_profile_id = None
            return self._create_patchright_session(profile_config, pw)

        ws_endpoint = start_result['ws_endpoint']

        # BUG FIX #6: Kurangi wait dari 3s → 1s. CDP connect punya retry
        # 3x dengan backoff 2-6s sendiri, jadi tidak perlu wait panjang
        # di sini. Ini menghemat ~2 detik per user.
        logger.info(f"Waiting for browser to be ready...")
        time.sleep(1)

        # Connect Playwright to the running browser (with retry)
        max_connect_retries = 3
        for attempt in range(max_connect_retries):
            try:
                browser = pw.chromium.connect_over_cdp(ws_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context()

                # ========================================================
                # MANAGE EXISTING PAGES — remove AdsPower start tabs
                # ========================================================
                # AdsPower always opens a start page (start.adspower.net)
                # and sometimes duplicate tabs.
                #
                # Strategy: Navigate pages[0] to about:blank (reuse it as
                # our automation page), then close all OTHER pages.
                # We do NOT close all pages because Chromium will auto-
                # reopen a new tab if we close the last one.
                existing_pages = list(context.pages)
                logger.info(
                    f"AdsPower: Found {len(existing_pages)} existing tab(s) on connect"
                )

                if existing_pages:
                    # Reuse the first page — navigate it away from AdsPower start page
                    page = existing_pages[0]
                    
                    # v9.2.2: Check if page is alive BEFORE navigating.
                    # If the browser crashed during startup (e.g., due to proxy
                    # or launch_args issues), the page will be closed already.
                    try:
                        _ = page.url  # Simple liveness check
                    except Exception as alive_err:
                        logger.warning(f"AdsPower: First page is dead on connect: {alive_err}")
                        # Page is dead — browser likely crashed. Try creating a new page.
                        try:
                            page = context.new_page()
                            logger.info("AdsPower: Created new page after dead page detected")
                        except Exception as new_page_err:
                            logger.error(f"AdsPower: Cannot create new page — browser is dead: {new_page_err}")
                            raise Exception(f"AdsPower browser crashed on startup — page dead, cannot create new page")
                    
                    try:
                        page.goto('about:blank', timeout=5000)
                        logger.info(f"AdsPower: Navigated first tab to about:blank")
                    except Exception as nav_err:
                        # v9.2.2: Don't treat about:blank navigation failure as fatal.
                        # The page might still be usable for our target URL.
                        logger.warning(f"AdsPower: Could not navigate first tab to about:blank: {nav_err}")
                        # Check if page is still alive after the failed navigation
                        try:
                            _ = page.url
                        except Exception:
                            logger.error("AdsPower: Page died after about:blank navigation attempt")
                            raise Exception("AdsPower browser crashed — page closed after navigation attempt")

                    # Close all other pages (duplicate AdsPower tabs, etc.)
                    for extra_page in existing_pages[1:]:
                        try:
                            extra_page.close()
                            logger.info(f"AdsPower: Closed extra tab")
                        except Exception as close_err:
                            logger.warning(f"Could not close extra tab: {close_err}")

                # Wait briefly for any auto-reopened tabs, then aggressively clean up
                # AdsPower may auto-reopen its start page multiple times
                for cleanup_round in range(3):
                    time.sleep(0.5)
                    closed_any = False
                    for p in list(context.pages):
                        try:
                            if p != page and ('adspower.net' in (p.url or '') or 'adspower.com' in (p.url or '')):
                                p.close()
                                logger.info(f"AdsPower: Closed auto-reopened start page tab (round {cleanup_round + 1})")
                                closed_any = True
                        except Exception:
                            pass
                    if not closed_any:
                        break  # No more AdsPower tabs to close

                tab_count = len(context.pages)
                # Verify our page is not an AdsPower tab (safety check)
                current_url = page.url or ''
                if 'adspower.net' in current_url or 'adspower.com' in current_url:
                    logger.warning(f"AdsPower: Main page is still on AdsPower URL, navigating to about:blank...")
                    try:
                        page.goto('about:blank', timeout=5000)
                    except Exception:
                        pass
                logger.info(
                    f"AdsPower: Session ready — {tab_count} tab(s), "
                    f"page URL = {page.url}"
                )

                # ========================================================
                # MAXIMIZE BROWSER WINDOW via CDP
                # v9.2.2: If CDP fails with "has been closed", browser is dead.
                # Detect this early and raise exception to trigger Patchright fallback.
                # ========================================================
                try:
                    cdp_session = context.new_cdp_session(page)
                    window_info = cdp_session.send('Browser.getWindowForTarget')
                    window_id = window_info.get('windowId')
                    if window_id is not None:
                        cdp_session.send('Browser.setWindowBounds', {
                            'windowId': window_id,
                            'bounds': {'windowState': 'maximized'}
                        })
                        logger.info(f"AdsPower: Browser window MAXIMIZED via CDP")
                    cdp_session.detach()
                except Exception as max_err:
                    max_err_str = str(max_err)
                    if 'has been closed' in max_err_str or 'Target closed' in max_err_str:
                        logger.error(f"AdsPower: Browser is DEAD during maximize: {max_err}")
                        raise Exception(f"AdsPower browser crashed during setup — CDP session failed: {max_err_str[:100]}")
                    logger.warning(f"AdsPower: Could not maximize window via CDP: {max_err}")

                session = {
                    'page': page,
                    'context': context,
                    'browser': browser,
                    'profile_id': profile_id,
                    'ws_endpoint': ws_endpoint,
                    'mode': 'antidetect',
                }
                self._sessions.append(session)
                logger.info(f"Anti-detect session created: profile={profile_id}")
                return session

            except Exception as e:
                if attempt < max_connect_retries - 1:
                    wait_time = 2 * (attempt + 1)
                    logger.warning(
                        f"Failed to connect to anti-detect browser (attempt {attempt + 1}/{max_connect_retries}): {e}\n"
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to connect to anti-detect browser after {max_connect_retries} attempts: {e}")
                    # v9.5.2: silent=True — profile mungkin sudah crash, "not open" OK
                    self._client.stop_profile(profile_id, silent=True)
                    # Don't delete — keep for reuse
                    return self._create_patchright_session(profile_config, pw)

    def _create_patchright_session(self, profile_config, pw):
        """
        Create session using Patchright (legacy fallback).
        Even in fallback mode, we apply enhanced stealth from the
        upgraded stealth_py.py.

        v9.1: now derives viewport / has_touch / device_scale_factor from
        sync_config (single source of truth) rather than from UA parsing,
        so a Pixel 8 device stays a Pixel 8 even when this fallback fires.
        """
        from stealth_py import apply_stealth_py, inject_stealth_to_page

        launch_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--ignore-certificate-errors',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-infobars',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-ipc-flooding-protection',
            '--force-color-profile=srgb',
        ]

        ua = profile_config.get('ua', '')
        resolution = profile_config.get('resolution', '1920x1080')
        if 'x' in resolution:
            w, h = resolution.split('x')
            launch_args.append(f'--window-size={w},{h}')

        locale = profile_config.get('lan', 'en-US')
        launch_args.append(f'--lang={locale}')

        # Disable WebRTC completely to prevent IP leaks
        launch_args.extend([
            '--disable-features=WebRTC',
            '--enforce-webrtc-ip-permission-check',
            '--webrtc-ip-handling-policy=disable_non_proxied_udp',
        ])

        # DNS leak prevention — force DNS through proxy.
        # v9.1: do NOT append '' (empty string) when proxy IS set — that
        # was a latent bug that polluted the args list with empty entries.
        launch_args.append('--disable-features=DnsOverHttps')
        if not profile_config.get('proxy_host'):
            launch_args.append('--no-proxy-server')

        # Filter out any empty strings just in case
        launch_args = [a for a in launch_args if a]

        launch_kwargs = {
            'headless': False,
            'args': launch_args,
        }

        # Configure proxy
        proxy_host = profile_config.get('proxy_host', '')
        proxy_port = profile_config.get('proxy_port', '')
        proxy_user = profile_config.get('proxy_user', '')
        proxy_pass = profile_config.get('proxy_password', '')
        proxy_type = profile_config.get('proxy_type', 'http')

        if proxy_host and proxy_port:
            proxy_server = f"{proxy_type}://{proxy_host}:{proxy_port}"
            if proxy_user:
                launch_kwargs['proxy'] = {
                    'server': proxy_server,
                    'username': proxy_user,
                    'password': proxy_pass,
                }
                # v9.2: Log proxy auth for debugging tunnel issues
                logger.info(f"Patchright proxy: {proxy_server} (auth: {proxy_user[:8]}...)")
            else:
                launch_args.append(f'--proxy-server={proxy_server}')
                logger.info(f"Patchright proxy: {proxy_server} (no auth)")

        browser = pw.chromium.launch(**launch_kwargs)

        # Build context with full profile sync
        tz = profile_config.get('timezone', 'America/New_York')
        viewport_w, viewport_h = 1920, 1080
        if 'x' in resolution:
            parts = resolution.split('x')
            try:
                viewport_w, viewport_h = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass

        # v9.1: derive has_touch from sync_config (authoritative), NOT
        # from UA string parsing. sync_config.has_touch is set by
        # ProfileSynchronizer based on the chosen device.os.
        has_touch = bool(profile_config.get('has_touch', False))
        device_scale = profile_config.get('device_scale_factor',
                                          2.625 if has_touch else 1.0)

        # v9.1: Chrome version — read from sync_config if provided,
        # otherwise parse from UA. Previously hardcoded '148' fallback
        # which was inconsistent with the rest of the codebase (CHROME_VERSION=137).
        chrome_version = profile_config.get('chrome_version')
        if not chrome_version:
            import re
            m = re.search(r'chrome/(\d+)\.', ua or '', re.IGNORECASE)
            chrome_version = m.group(1) if m else '137'

        # v9.1: avoid the circular import (bot_v6 imports antidetect_browser
        # at top-level). Inline the small derive_ch_ua_headers function here.
        ch_ua_headers = self._derive_ch_ua_headers(ua, chrome_version)

        context = browser.new_context(
            viewport={'width': viewport_w, 'height': viewport_h},
            locale=locale,
            timezone_id=tz,
            user_agent=ua,
            ignore_https_errors=True,
            java_script_enabled=True,
            has_touch=has_touch,
            device_scale_factor=device_scale,
            is_mobile=has_touch,
            extra_http_headers={
                'Accept-Language': f'{locale},en;q=0.9',
                **ch_ua_headers,
            },
        )

        # Apply enhanced stealth (upgraded stealth_py.py) — pass full
        # profile_config so WebGL / fonts / hardware / color_depth are
        # all synchronized with what we computed in ProfileSynchronizer.
        apply_stealth_py(
            context,
            locale=locale,
            user_agent=ua,
            chrome_version=chrome_version,
            use_patchright=True,
            profile_config=profile_config,
        )

        page = context.new_page()
        page.set_viewport_size({'width': viewport_w, 'height': viewport_h})

        session = {
            'page': page,
            'context': context,
            'browser': browser,
            'profile_id': None,
            'ws_endpoint': None,
            'mode': 'patchright',
        }
        self._sessions.append(session)
        logger.info("Patchright fallback session created with enhanced stealth")
        return session

    @staticmethod
    def _derive_ch_ua_headers(user_agent, chrome_version='137'):
        """
        Inline copy of bot_v6.derive_ch_ua_headers — avoids circular import.
        v9.1: imported from bot_v6 previously; that created a fragile
        circular dependency (bot_v6 imports antidetect_browser at top-level,
        antidetect_browser imported bot_v6 inside _create_patchright_session).
        """
        ua = (user_agent or '').lower()
        if 'windows nt' in ua:
            platform = 'Windows'
            mobile = '?0'
        elif 'mac os x' in ua or 'macintosh' in ua:
            platform = 'macOS'
            mobile = '?0'
        elif 'android' in ua:
            platform = 'Android'
            mobile = '?1'
        elif 'iphone' in ua or 'ipad' in ua or 'cros' in ua:
            platform = 'Linux' if 'cros' in ua else 'iOS'
            mobile = '?1'
        elif 'linux' in ua:
            platform = 'Linux'
            mobile = '?0'
        else:
            platform = 'Windows'
            mobile = '?0'
        sec_ch_ua = f'"Chromium";v="{chrome_version}", "Not_A Brand";v="24", "Google Chrome";v="{chrome_version}"'
        return {
            'Sec-CH-UA': sec_ch_ua,
            'Sec-CH-UA-Mobile': mobile,
            'Sec-CH-UA-Platform': f'"{platform}"',
        }

    def close_and_cleanup(self, session):
        """Close a browser session and clean up resources."""
        try:
            if session.get('mode') == 'antidetect' and self._client:
                # Stop the profile (but DON'T delete — reuse next time)
                profile_id = session.get('profile_id')
                if profile_id:
                    # v9.5.2: silent=True — bila profile sudah crash/close
                    # sebelumnya, "Profile is not open" tidak perlu noisy.
                    self._client.stop_profile(profile_id, silent=True)
                    logger.info(f"Stopped anti-detect profile: {profile_id} (kept for reuse)")

            # Close Playwright connection
            if session.get('context'):
                try:
                    session['context'].close()
                except Exception:
                    pass
            if session.get('browser'):
                try:
                    session['browser'].close()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")
        finally:
            if session in self._sessions:
                self._sessions.remove(session)

    def cleanup_all(self):
        """Clean up all active sessions."""
        for session in list(self._sessions):
            self.close_and_cleanup(session)

    def __del__(self):
        self.cleanup_all()
