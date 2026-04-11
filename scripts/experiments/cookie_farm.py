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


SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_USER_DATA_DIR = SCRIPT_DIR / ".browser-profile"


class CookieFarm:
    """Background browser manager that maintains fresh Akamai cookies.

    Launches a persistent Chrome context via Playwright, keeps the session
    alive, and exports cookies/tokens on demand for curl_cffi to use.
    """

    def __init__(self, user_data_dir=None, headless=False, ephemeral=True, env_file=None):
        if ephemeral and user_data_dir is None:
            self._ephemeral = True
            self._user_data_dir = Path(tempfile.mkdtemp(prefix="seataero-browser-"))
        else:
            self._ephemeral = False
            self._user_data_dir = Path(user_data_dir) if user_data_dir else DEFAULT_USER_DATA_DIR
        self._headless = headless
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = threading.Lock()
        self._mfa_prompt = None  # stored callback for re-auth during session recovery
        self._load_credentials(env_file)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def _load_credentials(self, env_file=None):
        """Load login and Gmail credentials from .env file."""
        script_dir = Path(__file__).parent.resolve()
        load_dotenv(env_file or (script_dir / ".env"))

        self._united_email = os.getenv("UNITED_EMAIL", "").strip()
        self._united_password = os.getenv("UNITED_PASSWORD", "").strip()
        self._gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
        self._gmail_app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()

    def _has_auto_login_credentials(self) -> bool:
        """Return True if all auto-login credentials are configured."""
        return all([self._united_email, self._united_password])

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
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._user_data_dir),
            headless=False,
            channel="chrome",
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        print(f"Cookie farm started ({'ephemeral' if self._ephemeral else 'persistent'} profile)")

    def stop(self):
        """Close browser context and Playwright instance."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None
        if self._ephemeral and self._user_data_dir.exists():
            try:
                shutil.rmtree(self._user_data_dir, ignore_errors=True)
                print(f"Cleaned up ephemeral profile: {self._user_data_dir}")
            except Exception:
                pass
        print("Cookie farm stopped")

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
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
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
            try:
                shutil.rmtree(old_dir, ignore_errors=True)
            except Exception:
                pass

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
                            pass
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

    def ensure_logged_in(self, mfa_prompt=None):
        """Navigate to united.com and verify login state.

        Args:
            mfa_prompt: Optional callable that returns the SMS verification
                code as a string. When provided and MFA is required, this
                callback is invoked to get the code from the user.

        Raises:
            RuntimeError: If headless and not logged in with no way to log in.
        """
        # Persist callback so restart() and recovery paths can re-auth
        if mfa_prompt is not None:
            self._mfa_prompt = mfa_prompt

        with self._lock:
            current_url = self._page.url or ""
            if "united.com" not in current_url:
                self._page.goto(
                    "https://www.united.com/en/ca/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

            # Wait for SPA to resolve auth state — either the Sign in
            # button appears (anonymous) or a logged-in indicator renders.
            try:
                self._page.wait_for_selector(
                    'button:has-text("Sign in"):visible, '
                    'a:has-text("Sign in"):visible, '
                    'text="View My United", '
                    'text="MILEAGEPLUS NUMBER"',
                    timeout=10000,
                )
            except Exception:
                # Timeout — page may be slow, proceed with detection anyway
                pass

            if self._is_logged_in():
                print("Already logged in")
                return

            if not self._headless:
                # Headed mode: try auto-login first
                if self._has_auto_login_credentials():
                    result = self._auto_login()
                    if result == "success":
                        return
                    if result == "mfa_required":
                        if mfa_prompt is not None:
                            code = mfa_prompt()
                            if self._enter_mfa_code(code):
                                return
                            print("MFA code failed — falling back to manual login")
                        # Fall through to manual login
                self._wait_for_login()
                return

            # Headless mode: must auto-login
            if self._has_auto_login_credentials():
                result = self._auto_login()
                if result == "success":
                    return
                if result == "mfa_required":
                    if mfa_prompt is not None:
                        code = mfa_prompt()
                        if self._enter_mfa_code(code):
                            return
                        raise RuntimeError("MFA code was rejected")
                    raise RuntimeError(
                        "MFA required but no prompt callback provided. "
                        "Pass mfa_prompt to ensure_logged_in() or run headed."
                    )
                print("Auto-login failed")

            raise RuntimeError(
                "Not logged in and running headless. "
                "Configure auto-login credentials in .env or run headed first."
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
                pass

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
                    pass
            return logged_in
        except Exception:
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

    def _auto_login(self):
        """Automate United login up to the MFA wall.

        Enters MileagePlus credentials (email + password) and clicks
        Sign in.

        Must be called from within self._lock (called by ensure_logged_in).

        Returns:
            "success" if login succeeded without MFA,
            "mfa_required" if an MFA/verification code screen is detected,
            "failed" otherwise.
        """
        if not self._has_auto_login_credentials():
            print("Auto-login credentials not configured in .env")
            print("Required: UNITED_EMAIL, UNITED_PASSWORD")
            return "failed"

        print("Attempting auto-login...")
        page = self._page

        # Step 1: Click "Sign in" on the homepage
        try:
            page.goto("https://www.united.com/en/ca/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            print("  Step 1: Loaded homepage")

            sign_in = page.locator("text=Sign in").first
            sign_in.click()
            page.wait_for_timeout(4000)
            print("  Step 1: Clicked Sign in")
        except Exception as e:
            print(f"  Step 1 FAILED: {e}")
            return "failed"

        # Step 2: Enter email/MileagePlus number
        # The sign-in panel is a slide-out on the right side of the page.
        # The email input has id="MPIDEmailField" (discovered via debug_login.py).
        # United sometimes shows "Something went wrong" after clicking Continue,
        # so we retry the email+Continue step up to 3 times.
        password_visible = False
        for attempt in range(3):
            try:
                email_input = page.locator('#MPIDEmailField')
                email_input.wait_for(state="visible", timeout=10000)
                email_input.fill(self._united_email)
                page.wait_for_timeout(1000)
                print(f"  Step 2: Entered email (attempt {attempt + 1})")

                # Click Continue (JS click to avoid overlay interception)
                page.evaluate("""() => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const text = btn.textContent.trim();
                        const rect = btn.getBoundingClientRect();
                        if (text === 'Continue' && rect.x > 650 && rect.width > 100) {
                            btn.click();
                            return;
                        }
                    }
                }""")
                page.wait_for_timeout(4000)
                print("  Step 2: Clicked Continue")

                # Check if password field appeared or if we got an error
                password_input = page.locator('#password')
                password_input.wait_for(state="visible", timeout=8000)
                password_visible = True
                break
            except Exception as e:
                # Check for "Something went wrong" error
                content = page.content()
                if "Something went wrong" in content and attempt < 2:
                    print(f"  Step 2: 'Something went wrong' error, retrying in 5s...")
                    page.wait_for_timeout(5000)
                    # Close the drawer and re-open to reset state
                    try:
                        close_btn = page.locator('button[aria-label="Close"]').first
                        close_btn.click()
                        page.wait_for_timeout(2000)
                        sign_in = page.locator("text=Sign in").first
                        sign_in.click()
                        page.wait_for_timeout(4000)
                    except Exception:
                        # If close/reopen fails, just reload the page
                        page.goto("https://www.united.com/en/ca/", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(4000)
                        sign_in = page.locator("text=Sign in").first
                        sign_in.click()
                        page.wait_for_timeout(4000)
                    continue
                else:
                    print(f"  Step 2 FAILED: {e}")
                    return "failed"

        if not password_visible:
            print("  Step 2 FAILED: Could not get past email step after 3 attempts")
            return "failed"

        # Step 3: Enter password
        # Password input has id="password" inside the sign-in drawer.
        try:
            password_input = page.locator('#password')
            password_input.wait_for(state="visible", timeout=5000)
            password_input.fill(self._united_password)
            page.wait_for_timeout(1000)
            print("  Step 3: Entered password")

            # The "Sign in" button in the drawer gets blocked by an overlay
            # when using normal Playwright click (the nav bar "Sign in" button
            # intercepts). Use JavaScript click scoped to the drawer instead.
            page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    const rect = btn.getBoundingClientRect();
                    // The drawer button is on the right side (x > 650) and is
                    // the filled/primary style, not the bare nav button
                    if (text === 'Sign in' && rect.x > 650 && rect.width > 100) {
                        btn.click();
                        return;
                    }
                }
            }""")
            page.wait_for_timeout(6000)
            print("  Step 3: Clicked Sign in")
        except Exception as e:
            print(f"  Step 3 FAILED: {e}")
            return "failed"

        # Check if we're already logged in (no MFA required)
        if self._is_logged_in():
            print("  Auto-login successful (no MFA required)!")
            return "success"

        # Check for MFA/verification code input
        try:
            page = self._page
            content = page.content().lower()
            # Look for verification code indicators
            has_mfa = any([
                "verification code" in content,
                "security code" in content,
                "enter code" in content,
                "enter your code" in content,
            ])
            if not has_mfa:
                # Also check for a numeric code input field
                code_input = page.locator('input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]')
                if code_input.count() > 0:
                    has_mfa = True
            if has_mfa:
                print("  MFA/verification code screen detected")
                return "mfa_required"
        except Exception:
            pass

        print("  Login did not complete — unknown state")
        return "failed"

    def _enter_mfa_code(self, code: str) -> bool:
        """Fill the MFA verification code and submit.

        Must be called after _auto_login() returns "mfa_required" and
        within self._lock (called from ensure_logged_in).

        Returns:
            True if login succeeded after entering the code.
        """
        page = self._page
        try:
            # Find the code input field
            code_input = page.locator(
                'input[type="tel"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            ).first
            code_input.wait_for(state="visible", timeout=5000)
            code_input.fill(code)
            page.wait_for_timeout(1000)
            print("  MFA: Entered verification code")

            # Find and click the submit/verify button
            page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim().toLowerCase();
                    if (['verify', 'submit', 'continue', 'confirm'].some(k => text.includes(k))) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 50 && rect.height > 20) {
                            btn.click();
                            return;
                        }
                    }
                }
            }""")
            page.wait_for_timeout(5000)
            print("  MFA: Clicked submit, waiting for page to settle...")

            # United's SPA does NOT redirect after MFA — it stays on the
            # verification page.  Navigate to the homepage so that
            # _is_logged_in() can see the authenticated DOM state.
            page.goto(
                "https://www.united.com/en/ca/",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Wait for SPA to resolve auth state
            try:
                page.wait_for_selector(
                    'button:has-text("Sign in"):visible, '
                    'a:has-text("Sign in"):visible, '
                    'text="View My United", '
                    'text="MILEAGEPLUS NUMBER"',
                    timeout=10000,
                )
            except Exception:
                pass

            if self._is_logged_in():
                print("  MFA: Login confirmed!")
                return True

            # Retry once — SPA hydration can be slow
            page.wait_for_timeout(3000)
            if self._is_logged_in():
                print("  MFA: Login confirmed!")
                return True

            print("  MFA: Login not confirmed after code entry")
            return False
        except Exception as e:
            print(f"  MFA: Failed to enter code — {e}")
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
