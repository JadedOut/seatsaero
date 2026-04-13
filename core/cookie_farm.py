"""Playwright cookie farm for maintaining fresh Akamai cookies.

Runs a real Chrome browser in the background to keep Akamai's _abck cookies
valid. The cookie farm exports cookies on demand for curl_cffi to use in
API calls, solving the problem of cookies burning after ~3-4 requests.

Usage:
    from cookie_farm import CookieFarm

    with CookieFarm() as farm:
        farm.ensure_logged_in()
        cookies = farm.get_cookies()
        token = farm.get_bearer_token()
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import atexit
import logging

log = logging.getLogger(__name__)

# Debug file log — visible even when stdout is redirected (MCP stdio transport)
_debug_log_path = Path.home() / ".seataero" / "cookie_farm_debug.log"
def _dbg(msg):
    """Write timestamped debug line to file + stderr."""
    import time as _t
    line = f"[{_t.strftime('%H:%M:%S')}] {msg}\n"
    try:
        with open(_debug_log_path, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_USER_DATA_DIR = SCRIPT_DIR / ".browser-profile"


class CookieFarm:
    """Background browser manager that maintains fresh Akamai cookies.

    Launches a persistent Chrome context via Playwright, keeps the session
    alive, and exports cookies/tokens on demand for curl_cffi to use.
    """

    def __init__(self, user_data_dir=None, headless=False, ephemeral=True, env_file=None, proxy=None):
        if ephemeral and user_data_dir is None:
            self._ephemeral = True
            self._user_data_dir = Path(tempfile.mkdtemp(prefix="seataero-browser-"))
            self._all_ephemeral_dirs = [self._user_data_dir]
        else:
            self._ephemeral = False
            self._user_data_dir = Path(user_data_dir) if user_data_dir else DEFAULT_USER_DATA_DIR
            self._all_ephemeral_dirs = []
        atexit.register(self._cleanup_all_profiles)
        self._headless = headless
        self._proxy = proxy or os.getenv("PROXY_URL", "").strip() or None
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = threading.Lock()
        self._mfa_prompt = None  # stored callback for re-auth during session recovery
        self.status_message = ""  # current login step, read by MCP status poller
        self._load_credentials(env_file)

    @property
    def proxy(self) -> str | None:
        return self._proxy

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def _load_credentials(self, env_file=None):
        """Load login credentials from .env file."""
        load_dotenv(env_file or (Path.home() / ".seataero" / ".env"))

        self._united_mp_number = os.getenv("UNITED_MP_NUMBER", "").strip()
        self._united_password = os.getenv("UNITED_PASSWORD", "").strip()

    def _has_auto_login_credentials(self) -> bool:
        """Return True if all auto-login credentials are configured."""
        return all([self._united_mp_number, self._united_password])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch Playwright persistent context with anti-detection args.

        Kills any orphaned Chrome processes holding the profile lock before
        launching to prevent "Opening in existing browser session" errors.
        """
        self._kill_orphaned_chrome()
        time.sleep(1)  # Allow OS to release file locks
        self._playwright = sync_playwright().start()
        # United.com is behind Akamai Bot Manager which detects headless
        # browsers via TLS/HTTP2 fingerprinting (ERR_HTTP2_PROTOCOL_ERROR).
        # Always launch headed — headless is not viable against Akamai.
        if self._headless:
            print("WARNING: headless mode is not supported for united.com "
                  "(Akamai bot detection). Launching headed instead.")
            self._headless = False
        launch_kwargs = dict(
            user_data_dir=str(self._user_data_dir),
            headless=False,
            channel="chrome",
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}
            print(f"Cookie farm using proxy: {self._proxy.split('@')[-1] if '@' in self._proxy else self._proxy}")

        self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        print(f"Cookie farm started ({'ephemeral' if self._ephemeral else 'persistent'} profile)")

    def stop(self):
        """Close browser context and Playwright instance."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                log.debug("context close failed", exc_info=True)
            self._context = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                log.debug("playwright stop failed", exc_info=True)
            self._playwright = None
        self._page = None
        if self._ephemeral and self._user_data_dir.exists():
            try:
                shutil.rmtree(self._user_data_dir, ignore_errors=True)
                print(f"Cleaned up ephemeral profile: {self._user_data_dir}")
            except Exception:
                log.debug("ephemeral profile cleanup failed", exc_info=True)
        print("Cookie farm stopped")

    def _cleanup_all_profiles(self):
        """atexit handler: remove any ephemeral profile dirs that weren't cleaned up."""
        for d in self._all_ephemeral_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    def restart(self):
        """Restart the browser after a crash.

        Safely tears down whatever remains of the old browser context and
        launches a fresh one.  The persistent profile is preserved so the
        login session survives.  On Windows, also kills orphaned Chrome
        processes that hold the profile lock.
        """
        print("Restarting browser (crash recovery)...")
        # Tear down old state — ignore errors from already-dead objects
        try:
            if self._context:
                self._context.close()
        except Exception:
            log.debug("context close failed during restart", exc_info=True)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            log.debug("playwright stop failed during restart", exc_info=True)
        self._context = None
        self._playwright = None
        self._page = None

        # Kill orphaned Chrome processes that hold the profile lock
        self._kill_orphaned_chrome()
        time.sleep(2)  # Allow OS to release file locks

        # Rotate ephemeral profile (don't reuse potentially flagged cookies)
        if self._ephemeral:
            old_dir = self._user_data_dir
            self._user_data_dir = Path(tempfile.mkdtemp(prefix="seataero-browser-"))
            self._all_ephemeral_dirs.append(self._user_data_dir)
            try:
                shutil.rmtree(old_dir, ignore_errors=True)
            except Exception:
                log.debug("old profile cleanup failed during restart", exc_info=True)

        # Re-launch
        self.start()
        self.ensure_logged_in(mfa_prompt=self._mfa_prompt)
        print("Browser restarted successfully (re-authenticated)")

    def _kill_orphaned_chrome(self):
        """Kill Chrome processes using this instance's user-data-dir.

        On Windows, uses wmic + taskkill to find and terminate Chrome
        processes launched with our specific --user-data-dir flag.
        """
        if sys.platform != "win32":
            return
        profile_str = str(self._user_data_dir).replace("/", "\\")
        try:
            result = subprocess.run(
                ["wmic", "process", "where", "name='chrome.exe'",
                 "get", "ProcessId,CommandLine"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if profile_str in line:
                    # Extract PID (last whitespace-separated token)
                    parts = line.strip().split()
                    if parts:
                        pid = parts[-1]
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/PID", pid, "/T"],
                                capture_output=True, timeout=10,
                            )
                            print(f"  Killed orphaned Chrome process PID {pid}")
                        except Exception:
                            log.debug("taskkill failed for PID %s", pid, exc_info=True)
        except Exception as exc:
            print(f"  Warning: could not scan for orphaned Chrome: {exc}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def ensure_logged_in(self, mfa_prompt=None, mfa_method="sms"):
        """Navigate to united.com and verify login state."""
        _dbg(f"ensure_logged_in called, mfa_prompt={'SET' if mfa_prompt else 'None'}, creds={self._has_auto_login_credentials()}")
        if mfa_prompt is not None:
            self._mfa_prompt = mfa_prompt

        with self._lock:
            current_url = self._page.url or ""
            _dbg(f"current_url={current_url}")
            if "united.com" not in current_url:
                _dbg("Navigating to united.com...")
                self._page.goto(
                    "https://www.united.com/en/ca/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                _dbg("Navigation done, sleeping 3s...")

            # Let the SPA settle before interacting — immediate DOM queries
            # after navigation can trigger Akamai bot detection tab crashes.
            time.sleep(3)
            _dbg("Sleep done, checking login state...")

            if self._is_logged_in():
                _dbg("Already logged in!")
                print("Already logged in")
                return

            _dbg(f"Not logged in. Has creds: {self._has_auto_login_credentials()}")
            if not self._has_auto_login_credentials():
                _dbg("No credentials — falling back to manual login")
                if not self._headless:
                    self._wait_for_login()
                    return
                raise RuntimeError(
                    "No login credentials configured. "
                    "Set UNITED_MP_NUMBER + UNITED_PASSWORD in .env."
                )

            _dbg("Starting auto-login (regular click)...")
            print("Trying auto-login...")
            result = self._auto_login(mfa_method=mfa_method)
            print(f"  auto_login returned: {result!r}")
            self.status_message = f"auto_login returned: {result!r}"
            if result == "success":
                return
            if result == "failed":
                # Retry with a fresh navigation and longer settle time.
                # Ghost cursor is NOT used — it can hang indefinitely and
                # lock the MCP session permanently.
                print("  Login failed — retrying with fresh page...")
                self.status_message = "Retrying login..."
                try:
                    self._page.goto("https://www.united.com/en/ca/", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                except Exception:
                    log.warning("navigation retry failed", exc_info=True)
                result = self._auto_login(mfa_method=mfa_method)
                if result == "success":
                    return
            if result == "mfa_required":
                if mfa_prompt is not None:
                    print("  Calling mfa_prompt (waiting for code file)...")
                    self.status_message = "Waiting for MFA code file..."
                    code = mfa_prompt()
                    print(f"  Got MFA code: {code[:2]}***")
                    self.status_message = f"Got MFA code, entering..."
                    mfa_ok = self._enter_mfa_code(code)
                    print(f"  _enter_mfa_code returned: {mfa_ok}")
                    self.status_message = f"_enter_mfa_code returned: {mfa_ok}"
                    if mfa_ok:
                        return
                    print("  MFA code rejected")
                else:
                    print("  MFA required but no prompt callback")

            # If running programmatically (mfa_prompt set), always raise on failure.
            # _wait_for_login() is only for interactive CLI use without mfa_prompt.
            if not self._headless and mfa_prompt is None:
                self._wait_for_login()
                return

            self.status_message = f"FINAL FAIL: auto_login={result!r}, url={self._page.url}"
            raise RuntimeError(
                "Login failed. Your IP may be blocked by Akamai. "
                "Try restarting your router for a new IP, "
                "or check your .env credentials."
            )

    def _is_logged_in(self) -> bool:
        """Check whether the current session is authenticated.

        Strategy: first prove we're NOT logged in (visible Sign in button),
        then look for positive evidence of authentication (user-specific
        DOM content that React only renders after login).
        """
        try:
            # Negative signal: if the Sign in button is visible, we're
            # definitely not logged in.  This catches fresh profiles and
            # expired sessions quickly.
            try:
                sign_in_btn = self._page.locator(
                    'button:has-text("Sign in"):visible, '
                    'a:has-text("Sign in"):visible'
                )
                if sign_in_btn.count() > 0:
                    return False
            except Exception:
                log.warning("sign-in button check failed", exc_info=True)

            # Positive signals: user-specific content that React renders
            # only after authentication.  These never appear for anonymous
            # visitors (unlike "mileageplus" or "my trips" in the nav bar).
            content = self._page.content().lower()
            logged_in = any([
                "mileageplus number:" in content,   # "MILEAGEPLUS NUMBER: MUH48117"
                "view my united" in content,          # "View My United" button
                "myaccount" in content,               # Account URL/link
                "hi, " in content,                    # Greeting only rendered for logged-in users
            ])
            if logged_in:
                try:
                    cookies = self._context.cookies("https://www.united.com")
                    names = sorted({c["name"] for c in cookies})
                    print(f"  [DEBUG] Login confirmed. Cookies ({len(names)}): {names}")
                except Exception:
                    log.debug("cookie debug logging failed", exc_info=True)
            return logged_in
        except Exception:
            log.warning("login check failed", exc_info=True)
            return False

    def _has_login_cookies(self) -> bool:
        """Check for logged-in cookies WITHOUT touching the page DOM.

        Uses Playwright's cookie jar API (CDP-level) which does not interfere
        with any in-progress page interactions like the login flow.
        """
        try:
            cookies = self._context.cookies("https://www.united.com")
            cookie_names = {c["name"] for c in cookies}
            # United sets these cookies after successful authentication
            login_indicators = {"MileagePlusID", "uaLoginToken", "MP_AToken", "AuthCookie", "User"}
            return bool(cookie_names & login_indicators)
        except Exception:
            return False

    def _wait_for_login(self):
        """Print instructions and poll until the user logs in manually."""
        print()
        print("=" * 50)
        print("MANUAL LOGIN REQUIRED")
        print("=" * 50)
        print("1. The browser is open at united.com")
        print("2. Click 'Sign in' and log in with your MileagePlus account")
        print("3. Complete SMS MFA when prompted")
        print("4. Once logged in, this script will detect it automatically")
        print("=" * 50)

        print("\nPolling for login (checking every 10s, timeout 30min)...")
        deadline = time.time() + 1800  # 30 minutes
        while time.time() < deadline:
            time.sleep(10)
            if self._is_logged_in():
                print("Login confirmed!")
                return
            print("  Still waiting for login...")

        print("ERROR: Login timed out after 30 minutes.")

    # ------------------------------------------------------------------
    # Auto-login
    # ------------------------------------------------------------------

    def _auto_login(self, use_ghost_cursor=False, mfa_method="sms"):
        """Automate United login up to the MFA wall.

        Enters MileagePlus number + password and clicks Sign in.

        Must be called from within self._lock (called by ensure_logged_in).

        Returns:
            "success" if login succeeded without MFA,
            "mfa_required" if an MFA/verification code screen is detected,
            "failed" otherwise.
        """
        if not self._has_auto_login_credentials():
            print("Auto-login credentials not configured in .env")
            print("Required: UNITED_MP_NUMBER, UNITED_PASSWORD")
            return "failed"

        def _step(msg):
            """Update status_message (for MCP poller) and print."""
            self.status_message = msg
            print(f"  {msg}")

        _step("Starting auto-login...")
        page = self._page

        # Selectors used throughout the login flow
        _SIGN_IN_DRAWER_INPUT = '#MPIDEmailField'
        _PASSWORD_FIELD = '#password'
        _SIGN_IN_BUTTON = 'text=Sign in'
        _MFA_INPUTS = 'input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'

        # Click a button inside the sign-in drawer by its visible text.
        # The drawer is the rightmost panel; its buttons come AFTER nav-bar
        # buttons in DOM order, so we pick the last match.  No magic pixel
        # coordinates — works at any viewport width.
        # Returns True if a button was found and clicked, False otherwise.
        def _click_drawer_button(text):
            if use_ghost_cursor:
                from core.ghost_click import ghost_click_button_by_text
                return ghost_click_button_by_text(page, text)
            return page.evaluate("""(text) => {
                const matches = [];
                for (const btn of document.querySelectorAll('button')) {
                    if (btn.textContent.trim() === text && btn.offsetParent !== null) {
                        matches.push(btn);
                    }
                }
                if (matches.length === 0) return false;
                matches[matches.length - 1].click();
                return true;
            }""", text)

        # Helper: open the sign-in drawer and wait for the input field
        def _open_sign_in_drawer():
            sign_in = page.locator(_SIGN_IN_BUTTON).first
            if use_ghost_cursor:
                from core.ghost_click import ghost_click_element
                try:
                    ghost_click_element(page, sign_in.element_handle())
                except Exception:
                    sign_in.click()
            else:
                sign_in.click()
            page.locator(_SIGN_IN_DRAWER_INPUT).wait_for(state="visible", timeout=10000)

        # Helper: fill identifier + click Continue, return True if password field appears
        def _submit_identifier(value, label="identifier"):
            page.locator(_SIGN_IN_DRAWER_INPUT).fill(value)
            _step(f"Entered {label}, clicking Continue...")
            if not _click_drawer_button("Continue"):
                _step(f"Could not find Continue button in drawer")
                return False
            try:
                page.locator(_PASSWORD_FIELD).wait_for(state="visible", timeout=12000)
                return True
            except Exception:
                return False

        # Step 1: Open sign-in drawer (caller already navigated to united.com)
        try:
            current = page.url or ""
            if "united.com" not in current:
                _dbg("_auto_login: navigating to united.com (not already there)...")
                _step("Loading united.com...")
                page.goto("https://www.united.com/en/ca/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
            _dbg("_auto_login: opening sign-in drawer...")
            _step("Opening sign-in drawer...")
            _open_sign_in_drawer()
            _dbg("_auto_login: drawer open!")
            _step("Sign-in drawer open")
        except Exception as e:
            _dbg(f"_auto_login: FAILED at step 1: {type(e).__name__}: {e}")
            _step(f"FAILED: {e}")
            return "failed"

        # Step 2: Enter MP# and click Continue
        _dbg("_auto_login: entering MP#...")
        _step("Entering MP#...")
        password_visible = _submit_identifier(self._united_mp_number, "MP#")
        if not password_visible:
            _step("FAILED: Password field not visible after MP# entry")
            return "failed"
        _step("MP# accepted — entering password...")

        # Step 3: Enter password and submit
        try:
            page.locator(_PASSWORD_FIELD).fill(self._united_password)
            _step("Clicking Sign in...")

            if not _click_drawer_button("Sign in"):
                _step("FAILED: Could not find Sign in button in drawer")
                return "failed"

            _step("Waiting for login response...")
            try:
                page.locator(_PASSWORD_FIELD).wait_for(state="hidden", timeout=15000)
            except Exception:
                log.warning("password field hide wait failed", exc_info=True)
        except Exception as e:
            _step(f"FAILED: {e}")
            return "failed"

        # Check outcome: logged in, MFA required, or unknown
        if self._is_logged_in():
            _step("Login successful!")
            return "success"

        try:
            content = page.content().lower()
            has_mfa = any(kw in content for kw in [
                "verification code", "security code", "enter code", "enter your code",
            ])
            if not has_mfa:
                if page.locator(_MFA_INPUTS).count() > 0:
                    has_mfa = True
            if has_mfa:
                self._select_mfa_method(page, mfa_method, _step)
                _step(f"MFA code required ({mfa_method}) — waiting for code...")
                return "mfa_required"
        except Exception:
            log.error("MFA detection/selection failed", exc_info=True)

        _step("Login did not complete — unknown state")
        return "failed"

    def _select_mfa_method(self, page, mfa_method, _step):
        """Select SMS or email delivery on United's MFA method screen.

        Called after _auto_login detects MFA. If United shows a method selection
        (e.g., radio buttons or links for SMS/email), clicks the appropriate one.
        If no selection is shown (code input already visible), does nothing.

        Args:
            page: Playwright page on MFA screen.
            mfa_method: "sms" or "email".
            _step: Logging callback.
        """
        # If code input is already visible, United auto-sent — no selection needed
        try:
            code_input = page.locator(
                'input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            ).first
            if code_input.is_visible():
                _step(f"Code input already visible — United auto-sent ({mfa_method})")
                return
        except Exception:
            log.warning("code input visibility check failed", exc_info=True)

        # Look for method selection options
        _step(f"Looking for MFA method selection (want: {mfa_method})...")

        if mfa_method == "email":
            selectors = [
                'button:has-text("email")',
                'a:has-text("email")',
                'label:has-text("email")',
                '[data-testid*="email"]',
            ]
        else:
            selectors = [
                'button:has-text("text")',
                'a:has-text("text")',
                'button:has-text("phone")',
                'a:has-text("phone")',
                'label:has-text("text")',
                'label:has-text("phone")',
                '[data-testid*="sms"]',
                '[data-testid*="phone"]',
            ]

        for selector in selectors:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=2000):
                    _step(f"Found method option: {selector}")
                    el.click()
                    # Wait for code input to appear after selection
                    page.locator(
                        'input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                    ).first.wait_for(state="visible", timeout=10000)
                    _step(f"Code input appeared after selecting {mfa_method}")
                    return
            except Exception:
                continue

        _step(f"No method selection found — proceeding with default ({mfa_method})")

    def _enter_mfa_code(self, code: str) -> bool:
        """Fill the MFA verification code and submit.

        Must be called after _auto_login() returns "mfa_required" and
        within self._lock (called from ensure_logged_in).

        Returns:
            True if login succeeded after entering the code.
        """
        def _mfa_step(msg):
            self.status_message = f"MFA: {msg}"
            print(f"  MFA: {msg}")

        page = self._page
        try:
            _mfa_step(f"Looking for code input (url: {page.url})")
            code_input = page.locator(
                'input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            ).first
            code_input.wait_for(state="visible", timeout=5000)
            code_input.fill(code)
            _mfa_step("Entered verification code")

            # Click the submit/verify button
            clicked = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim().toLowerCase();
                    if (['verify', 'submit', 'continue', 'confirm'].some(k => text.includes(k))) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 50 && rect.height > 20) {
                            btn.click();
                            return text;
                        }
                    }
                }
                return null;
            }""")
            _mfa_step(f"Clicked button: {clicked}")

            if not clicked:
                _mfa_step("WARNING: No submit button found!")

            # Wait for United to process the MFA code before navigating away.
            # The code input disappearing (or a navigation) signals acceptance.
            try:
                code_input.wait_for(state="hidden", timeout=15000)
                _mfa_step(f"Verification page dismissed (url: {page.url})")
            except Exception:
                import time as _time
                _time.sleep(3)
                _mfa_step(f"Code input still visible after 15s (url: {page.url})")

            # Check cookies before navigating — they may already be set
            if self._has_login_cookies():
                _mfa_step("Auth cookies present after MFA submit!")

            # Check if United already redirected us (e.g. to homepage)
            current = page.url or ""
            _mfa_step(f"Current URL: {current}")
            if "verification" in current.lower() or "otp" in current.lower() or "mfa" in current.lower():
                _mfa_step("Still on MFA page, navigating home...")
                page.goto(
                    "https://www.united.com/en/ca/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            else:
                _mfa_step("United redirected, waiting for page to settle...")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    log.warning("page load wait failed", exc_info=True)

            _mfa_step(f"Final URL: {page.url}")

            # Wait for SPA to resolve auth state (sign-in button or user greeting)
            try:
                page.wait_for_selector(
                    'button:has-text("Sign in"):visible, '
                    'a:has-text("Sign in"):visible',
                    timeout=15000,
                )
            except Exception:
                pass  # Timeout — page may be slow, proceed with detection anyway

            if self._is_logged_in():
                _mfa_step("Login confirmed!")
                return True

            # DOM check failed, but auth cookies may already be set
            if self._has_login_cookies():
                _mfa_step("Login confirmed via cookies (DOM not yet rendered)")
                return True

            _mfa_step("Login not confirmed after code entry")
            return False
        except Exception as e:
            _mfa_step(f"Failed — {e}")
            return False

    # ------------------------------------------------------------------
    # Cookie / token export
    # ------------------------------------------------------------------

    def get_cookies(self) -> str:
        """Export all united.com cookies as a Cookie header string.

        Automatically restarts the browser if it has crashed.

        Returns:
            Cookie header value, e.g. "name1=value1; name2=value2; ..."
        """
        try:
            with self._lock:
                cookies = self._context.cookies("https://www.united.com")
                return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("closed", "target", "disposed", "disconnected", "crashed", "terminated")):
                print(f"Browser crashed during get_cookies: {exc}")
                self.restart()
                with self._lock:
                    cookies = self._context.cookies("https://www.united.com")
                    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            raise

    def get_bearer_token(self) -> str:
        """Fetch an anonymous bearer token from within the browser.

        Uses page.evaluate() to call the anonymous-token endpoint with the
        browser's full cookie context, exactly as the real site does.

        Automatically restarts the browser if it has crashed.

        Returns:
            Full authorization header value, e.g. "bearer abc123..."
        """
        try:
            with self._lock:
                token = self._page.evaluate(
                    """async () => {
                        const resp = await fetch('/api/auth/anonymous-token', {
                            method: 'GET',
                            credentials: 'same-origin',
                        });
                        if (resp.ok) {
                            const data = await resp.json();
                            return data?.data?.token?.hash
                                || data?.data?.token
                                || data?.token?.hash
                                || data?.token
                                || '';
                        }
                        return '';
                    }"""
                )
                if token:
                    return f"bearer {token}"
                return ""
        except Exception as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("closed", "target", "disposed", "disconnected", "crashed", "terminated")):
                print(f"Browser crashed during get_bearer_token: {exc}")
                self.restart()
                with self._lock:
                    token = self._page.evaluate(
                        """async () => {
                            const resp = await fetch('/api/auth/anonymous-token', {
                                method: 'GET',
                                credentials: 'same-origin',
                            });
                            if (resp.ok) {
                                const data = await resp.json();
                                return data?.data?.token?.hash
                                    || data?.data?.token
                                    || data?.token?.hash
                                    || data?.token
                                    || '';
                            }
                            return '';
                        }"""
                    )
                    if token:
                        return f"bearer {token}"
                    return ""
            raise

    # ------------------------------------------------------------------
    # Session expiry detection
    # ------------------------------------------------------------------

    def check_session(self) -> bool:
        """Navigate to united.com and check if the session is still active.

        Automatically restarts the browser if it has crashed.

        Returns:
            True if still logged in, False if session has expired.
        """
        try:
            with self._lock:
                self._page.goto(
                    "https://www.united.com/en/ca/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                self._page.wait_for_timeout(3000)
                logged_in = self._is_logged_in()
        except Exception as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("closed", "target", "disposed", "disconnected", "crashed", "terminated")):
                print(f"Browser crashed during session check: {exc}")
                self.restart()
                # After restart, session state is unknown — try again
                try:
                    with self._lock:
                        self._page.goto(
                            "https://www.united.com/en/ca/",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._page.wait_for_timeout(3000)
                        logged_in = self._is_logged_in()
                except Exception:
                    print("Session check failed even after browser restart")
                    return False
            else:
                raise

        if logged_in:
            print("Session check: still logged in")
        else:
            print("Session check: session has expired")
        return logged_in

    # ------------------------------------------------------------------
    # Cookie refresh
    # ------------------------------------------------------------------

    def refresh_cookies(self) -> bool:
        """Reload the current page to trigger Akamai JS sensor refresh.

        Uses reload() instead of goto() to preserve client-side auth state.
        Waits 2s after DOM load for Akamai's sensor JS to execute.

        Automatically restarts the browser if it has crashed.

        Returns:
            True if still logged in after refresh, False if session expired.
        """
        try:
            with self._lock:
                start = time.time()
                self._page.reload(
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                self._page.wait_for_timeout(2000)
                elapsed = time.time() - start
                # Cookie-first: auth cookies in jar = session valid
                if self._has_login_cookies():
                    logged_in = True
                else:
                    # Cookies missing — fall back to DOM check
                    logged_in = self._is_logged_in()
        except Exception as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("closed", "target", "disposed", "disconnected", "crashed", "terminated")):
                print(f"Browser crashed during cookie refresh: {exc}")
                self.restart()
                # Retry after restart
                try:
                    with self._lock:
                        start = time.time()
                        self._page.reload(
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._page.wait_for_timeout(2000)
                        elapsed = time.time() - start
                        # Cookie-first: auth cookies in jar = session valid
                        if self._has_login_cookies():
                            logged_in = True
                        else:
                            # Cookies missing — fall back to DOM check
                            logged_in = self._is_logged_in()
                except Exception:
                    print("Cookie refresh failed even after browser restart")
                    return False
            else:
                raise

        if logged_in:
            print(f"Cookies refreshed ({elapsed:.1f}s)")
        else:
            print("WARNING: Cookies AND DOM both indicate session expired")
        return logged_in
