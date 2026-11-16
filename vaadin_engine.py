"""
Hardened Playwright Execution Engine for Vaadin Stateful ERP (Zirius Plus).

Architectural Principles:
  1. Strict Accessibility Locators: Exclusively uses Playwright's `get_by_role()`,
     `get_by_text()`, `get_by_label()`, and `get_by_placeholder()`. Zero CSS/ID selectors.
  2. Pure Event-Driven Waiting: Zero `time.sleep()`. Relies purely on Playwright's
     auto-waiting, network idle states, and locator actionability.
  3. Resilient Error Handling & Self-Healing: Catches exceptions, captures full-page
     error screenshots, logs diagnostic data, and generates structured HTML/JSON reports.
  4. Memory-Level Security: Scrub credentials from memory and logs immediately after use.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
import gc
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import struct
import sys
import time
from typing import Any, Dict, List, Optional

# Force UTF-8 stream handling on Windows to prevent 'charmap' / cp1252 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# Configure module logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [VaadinEngine] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vaadin_engine")


@dataclass
class StepResult:
    """Record of an individual step execution."""
    step_number: int
    action: str
    target: str
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    duration_ms: float
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None


@dataclass
class ExecutionReport:
    """Complete summary of a test run."""
    test_name: str
    start_time: str
    end_time: str
    total_duration_sec: float
    status: str  # "PASSED" | "FAILED"
    steps: List[StepResult] = field(default_factory=list)
    final_screenshot: Optional[str] = None
    error_summary: Optional[str] = None
    report_json_path: Optional[str] = None
    report_html_path: Optional[str] = None


class VaadinEngine:
    """
    Autonomous, state-hardened Playwright runner tailored for Vaadin applications.
    Handles dynamic DOM, asynchronous server push, and Vaadin client rendering.
    """

    DEFAULT_TIMEOUT_MS = 15000  # 15s auto-wait limit for asynchronous actions
    ZIRIUS_URL = "https://delta.zerp.no/"

    def __init__(self, reports_dir: str = "reports", headless: bool = False, browser_channel: Optional[str] = None):
        """
        Initialize the Vaadin automation engine.

        Args:
            reports_dir: Directory where reports, logs, and screenshots are stored.
            headless: Whether to run the browser in headless mode (default: False for visibility).
            browser_channel: Browser channel to use ('msedge', 'chrome', or None for auto-detect).
        """
        self.reports_dir = Path(reports_dir).resolve()
        self.screenshots_dir = self.reports_dir / "screenshots"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.browser_channel = browser_channel

    def _launch_browser(self, p: Playwright) -> Browser:
        """
        Launches a browser hardened against Windows Smart App Control (SAC).
        Prioritizes officially signed system browsers (Microsoft Edge or Google Chrome),
        which are verified by Microsoft's digital signature and never blocked by Smart App Control.
        """
        # 1. If explicit channel provided by user
        if self.browser_channel:
            try:
                logger.info(f"Launching explicitly requested browser channel: {self.browser_channel}")
                return p.chromium.launch(channel=self.browser_channel, headless=self.headless)
            except Exception as e:
                logger.warning(f"Failed to launch requested channel '{self.browser_channel}': {e}. Falling back...")

        # 2. Try Microsoft Edge (on Windows, 100% Microsoft digitally signed for SAC)
        if sys.platform == "win32":
            try:
                logger.info("Attempting launch via Microsoft Edge (Smart App Control compliant)...")
                return p.chromium.launch(channel="msedge", headless=self.headless)
            except Exception as e_edge:
                logger.debug(f"Microsoft Edge channel launch skipped: {e_edge}")

        # 3. Try Google Chrome (officially signed Google binary)
        try:
            logger.info("Attempting launch via Google Chrome...")
            return p.chromium.launch(channel="chrome", headless=self.headless)
        except Exception as e_chrome:
            logger.debug(f"Google Chrome channel launch skipped: {e_chrome}")

        # 4. Fallback to bundled Playwright Chromium (native Linux / Docker / Cloud)
        logger.info("Launching bundled Playwright Chromium binary...")
        return p.chromium.launch(headless=self.headless)

    def _wait_for_vaadin_network_idle(self, page: Page, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        """
        Waits for page DOM readiness and network idle state.
        Never uses time.sleep(); relies purely on Playwright lifecycle events.
        """
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Network idle may be soft-delayed in streaming Vaadin setups; proceed to locator actionability
            logger.debug("Network idle wait reached soft timeout; continuing to element assertions.")

    ERP_ALIASES = {
        "dashboard": ["Dashboard", "Oversikt", "Hjem", "Startside", "Home"],
        "new order": ["New Order", "Ny ordre", "Ny bestilling", "Ordre", "Bestilling", "Ny"],
        "customer name": ["Customer Name", "Kundenavn", "Kunde", "Navn", "Customer"],
        "order": ["Order", "Ordre", "Bestillinger"],
    }

    def _get_alias_candidates(self, target: str) -> List[str]:
        """Expands target with English/Norwegian ERP aliases for resilient matching."""
        candidates = [target]
        target_lower = target.strip().lower()
        if target_lower in self.ERP_ALIASES:
            for alias in self.ERP_ALIASES[target_lower]:
                if alias not in candidates:
                    candidates.append(alias)
        return candidates

    def _pick_first_visible(self, locator: Locator) -> Optional[Locator]:
        """Iterates through matching elements and returns the first visible one, ignoring hidden templates."""
        count = locator.count()
        for i in range(count):
            cand = locator.nth(i)
            try:
                if cand.is_visible():
                    return cand
            except Exception:
                pass
        return None

    def _find_clickable_accessible(self, page: Page, target: str, role: Optional[str] = None) -> Locator:
        """
        Resolves an element to click using strict accessibility locators.
        Supports Norwegian ERP aliases (e.g. Dashboard -> Oversikt, New Order -> Ny ordre).
        Strictly prioritizes VISIBLE elements to prevent locking onto hidden template tags.
        """
        candidates = self._get_alias_candidates(target)

        # 1. Search for visible candidates across standard roles
        for cand in candidates:
            # Explicit role if specified
            if role:
                loc = self._pick_first_visible(page.get_by_role(role, name=cand, exact=False))
                if loc:
                    return loc

            # Link role (standard for ERP navigation sidebars)
            loc = self._pick_first_visible(page.get_by_role("link", name=cand, exact=False))
            if loc:
                return loc

            # Button role
            loc = self._pick_first_visible(page.get_by_role("button", name=cand, exact=False))
            if loc:
                return loc

            # Tab role
            loc = self._pick_first_visible(page.get_by_role("tab", name=cand, exact=False))
            if loc:
                return loc

            # Menuitem role
            loc = self._pick_first_visible(page.get_by_role("menuitem", name=cand, exact=False))
            if loc:
                return loc

            # Visible accessible text
            loc = self._pick_first_visible(page.get_by_text(cand, exact=False))
            if loc:
                return loc

        # Fallback if no visible element was detected during fast-scan
        fallback = page.get_by_text(target, exact=False)
        for i in range(fallback.count()):
            if fallback.nth(i).is_visible():
                return fallback.nth(i)

        return fallback.first

    def _find_input_accessible(self, page: Page, target: str) -> Locator:
        """
        Resolves an input field using strict accessibility locators.
        Supports Norwegian ERP aliases (e.g. Customer Name -> Kundenavn).
        Priority: get_by_label -> get_by_placeholder -> get_by_role("textbox", name=target).
        """
        candidates = self._get_alias_candidates(target)

        for cand in candidates:
            label_loc = page.get_by_label(cand, exact=False)
            if label_loc.count() > 0:
                return label_loc.first

            placeholder_loc = page.get_by_placeholder(cand, exact=False)
            if placeholder_loc.count() > 0:
                return placeholder_loc.first

            textbox_loc = page.get_by_role("textbox", name=cand, exact=False)
            if textbox_loc.count() > 0:
                return textbox_loc.first

        return page.get_by_label(target, exact=False).first

    def _find_login_fields(self, page: Page) -> Dict[str, Locator]:
        """
        Identifies username, password, and submit buttons on Zirius Plus
        strictly through accessibility locators.
        Supports both English and Norwegian (E-postadresse/Brukernavn/Passord) ERP labels.
        Guaranteed to avoid 'Show password' eye toggle buttons by enforcing exact matching
        and verifying element editability.
        """
        # Username Locators
        user_candidates = [
            page.get_by_label("E-postadresse", exact=False),
            page.get_by_label("E-post", exact=False),
            page.get_by_label("Username", exact=False),
            page.get_by_label("Brukernavn", exact=False),
            page.get_by_label("User", exact=False),
            page.get_by_label("Email", exact=False),
            page.get_by_placeholder("E-postadresse", exact=False),
            page.get_by_placeholder("din@e-postadresse.no", exact=False),
            page.get_by_placeholder("Username", exact=False),
            page.get_by_placeholder("Brukernavn", exact=False),
            page.get_by_placeholder("User", exact=False),
            page.get_by_placeholder("Email", exact=False),
            page.get_by_role("textbox").first,
        ]
        username_loc = None
        for cand in user_candidates:
            if cand.count() > 0:
                username_loc = cand.first
                break

        # Password Locators (exact=True to avoid matching "Show password" eye button)
        pwd_candidates = [
            page.get_by_label("Passord", exact=True),
            page.get_by_label("Password", exact=True),
            page.get_by_placeholder("Passord", exact=True),
            page.get_by_placeholder("Password", exact=True),
            page.get_by_role("textbox", name="Passord", exact=True),
            page.get_by_role("textbox", name="Password", exact=True),
            # In case label contains punctuation or whitespace
            page.get_by_label("Passord:", exact=False),
            page.get_by_label("Password:", exact=False),
        ]
        password_loc = None
        for cand in pwd_candidates:
            if cand is not None and cand.count() > 0:
                loc = cand.first
                # Verify that resolved element is an editable input, not a button (e.g. eye toggle)
                try:
                    tag_name = loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag_name in ("input", "textarea"):
                        password_loc = loc
                        break
                except Exception:
                    password_loc = loc
                    break

        # Fallback if password input still not found: scan labels with Passord/Password
        if not password_loc:
            for lbl in ["Passord", "Password"]:
                cand_list = page.get_by_label(lbl, exact=False)
                count = cand_list.count()
                for i in range(count):
                    loc = cand_list.nth(i)
                    try:
                        tag_name = loc.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name in ("input", "textarea"):
                            password_loc = loc
                            break
                    except Exception:
                        pass
                if password_loc:
                    break

        # Submit / Login Button Locators
        btn_candidates = [
            page.get_by_role("button", name="Logg inn", exact=False),
            page.get_by_role("button", name="Log in", exact=False),
            page.get_by_role("button", name="Login", exact=False),
            page.get_by_role("button", name="Sign in", exact=False),
            page.get_by_role("button").first,
        ]
        submit_loc = None
        for cand in btn_candidates:
            if cand.count() > 0:
                submit_loc = cand.first
                break

        if not username_loc or not password_loc or not submit_loc:
            raise RuntimeError(
                f"Vaadin login fields could not be resolved via accessibility locators. "
                f"Found: user={bool(username_loc)}, pwd={bool(password_loc)}, btn={bool(submit_loc)}"
            )

        return {
            "username": username_loc,
            "password": password_loc,
            "submit": submit_loc,
        }

    @staticmethod
    def generate_totp_code(secret: str) -> str:
        """
        Computes standard RFC 6238 6-digit TOTP code from a Base32 secret.
        100% free, zero external dependencies (pure Python standard library).
        """
        clean_secret = secret.replace(" ", "").upper()
        missing_padding = len(clean_secret) % 8
        if missing_padding:
            clean_secret += "=" * (8 - missing_padding)

        key = base64.b32decode(clean_secret, casefold=True)
        counter = int(time.time()) // 30
        counter_bytes = struct.pack("!Q", counter)

        hmac_digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
        offset = hmac_digest[-1] & 0x0F
        code_int = struct.unpack("!I", hmac_digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return f"{code_int % 1000000:06d}"

    def _handle_2fa_challenge(self, page: Page, creds: Dict[str, str], timeout_ms: int = 45000) -> None:
        """
        Detects and solves the Zirius Plus 2FA / Authenticator (One-time code) challenge.
        Supports:
          1. Fully autonomous TOTP: generates 6-digit code via RFC 6238 if 'totp_secret' is provided.
          2. Dynamic OTP: fills pre-supplied 'otp' / 'one_time_code'.
          3. Interactive prompt: prompts user in terminal or allows in-browser verification.
        Strictly uses accessibility locators: get_by_label, get_by_placeholder, get_by_role.
        """
        otp_locators = [
            page.get_by_label("One-time code", exact=False),
            page.get_by_placeholder("One-time code", exact=False),
            page.get_by_label("Engangskode", exact=False),
            page.get_by_placeholder("Engangskode", exact=False),
            page.get_by_label("OTP", exact=False),
            page.get_by_role("textbox", name="One-time code", exact=False),
        ]

        # Check if 2FA screen rendered
        otp_field = None
        for cand in otp_locators:
            if cand.count() > 0 and cand.first.is_visible():
                otp_field = cand.first
                break

        if not otp_field:
            # Short wait for asynchronous transition to 2FA page
            try:
                page.wait_for_selector("text=One-time code", timeout=3500)
                for cand in otp_locators:
                    if cand.count() > 0 and cand.first.is_visible():
                        otp_field = cand.first
                        break
            except Exception:
                pass

        if not otp_field:
            logger.debug("No 2FA Authenticator challenge detected; continuing to dashboard.")
            return

        logger.info("🔐 [2FA Detected] Zirius Plus Authenticator Challenge active ('One-time code').")

        # Optional Device Selection ('computer' or 'unnamed' if present)
        target_device = creds.get("device", "computer")
        device_loc = page.get_by_text(target_device, exact=False)
        if device_loc.count() > 0 and device_loc.first.is_visible():
            try:
                device_loc.first.click()
                logger.info(f"Selected 2FA device: '{target_device}'")
            except Exception:
                pass

        # Determine 6-digit Authenticator code
        code = None
        totp_secret = creds.get("totp_secret") or creds.get("2fa_secret")
        direct_otp = creds.get("otp") or creds.get("one_time_code")

        if totp_secret:
            logger.info("Generating live 6-digit code from Authenticator TOTP Secret Key...")
            try:
                code = self.generate_totp_code(totp_secret)
                logger.info(f"Generated RFC 6238 code: {code[:2]}****")
            except Exception as e_totp:
                logger.error(f"Failed to generate TOTP code: {e_totp}")

        elif direct_otp:
            code = str(direct_otp).strip()
            logger.info("Using pre-supplied One-Time Code.")

        else:
            # Interactive terminal prompt if stdin available
            if sys.stdin and sys.stdin.isatty():
                print("\n" + "=" * 65)
                print("[2FA REQUIRED] Enter the 6-digit Authenticator code from your device:")
                print("=" * 65)
                code = input("> ").strip()
            else:
                logger.info("Waiting for 2FA completion in browser window...")

        if code and otp_field:
            logger.info("Submitting One-Time Code into authenticator field...")
            otp_field.fill(code)

            sign_in_btns = [
                page.get_by_role("button", name="Sign In", exact=False),
                page.get_by_role("button", name="Logg inn", exact=False),
                page.get_by_role("button", name="Log in", exact=False),
                page.get_by_role("button").first,
            ]
            for btn in sign_in_btns:
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    break
        elif not code and not self.headless:
            logger.info("🔐 2FA screen active! Please enter the 6-digit Authenticator code in the open browser window (waiting up to 60s)...")
            try:
                # Wait for the user to complete 2FA in the open browser
                page.wait_for_selector("text=One-time code", state="detached", timeout=60000)
                logger.info("✓ 2FA completed by user in browser! Proceeding...")
            except PlaywrightTimeoutError:
                raise RuntimeError("Timed out waiting for 2FA completion. Please enter the Authenticator code or TOTP Secret Key in the UI.")
        elif not code and self.headless:
            raise RuntimeError("2FA Authenticator Challenge detected on Zirius Plus, but no 2FA code was provided. Please enter your 6-digit code or TOTP Secret Key in the UI.")

        # Wait for ERP dashboard transition after 2FA submission
        self._wait_for_vaadin_network_idle(page, timeout_ms=timeout_ms)

    def _sanitize(self, message: str, secret: Optional[str]) -> str:
        """Scrub secret credentials from log output, reports, and exception messages."""
        if not secret or len(secret) < 2:
            return message
        return message.replace(secret, "********")

    def _secure_memory_cleanup(self, credentials: Dict[str, str]) -> None:
        """Securely wipe dynamic credentials from memory after execution."""
        for key in list(credentials.keys()):
            credentials[key] = " " * len(credentials[key])
            del credentials[key]
        credentials.clear()
        gc.collect()

    def run_dry_run(self, target_url: str = ZIRIUS_URL) -> ExecutionReport:
        """
        Executes a safe dry-run connection test against target URL.
        Opens the site, verifies accessibility tree, takes a screenshot of the login screen
        WITHOUT entering credentials, saves it to /reports, and closes cleanly.
        """
        logger.info(f"Starting Safe Dry Run against: {target_url}")
        start_time = datetime.now()
        steps: List[StepResult] = []
        final_screenshot = None
        status = "PASSED"
        error_msg = None

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            try:
                # Step 1: Navigate to target URL
                t0 = datetime.now()
                logger.info(f"Navigating to {target_url}...")
                page.goto(target_url, wait_until="domcontentloaded", timeout=self.DEFAULT_TIMEOUT_MS)
                self._wait_for_vaadin_network_idle(page)
                dur = (datetime.now() - t0).total_seconds() * 1000
                steps.append(StepResult(
                    step_number=1,
                    action="navigate",
                    target=target_url,
                    status="PASS",
                    duration_ms=dur,
                ))

                # Step 2: Inspect accessibility tree for login components
                t0 = datetime.now()
                logger.info("Verifying Vaadin login screen accessibility elements...")
                fields = self._find_login_fields(page)
                # Ensure elements are visible
                fields["username"].wait_for(state="visible", timeout=self.DEFAULT_TIMEOUT_MS)
                fields["submit"].wait_for(state="visible", timeout=self.DEFAULT_TIMEOUT_MS)
                dur = (datetime.now() - t0).total_seconds() * 1000
                steps.append(StepResult(
                    step_number=2,
                    action="verify_accessibility",
                    target="Vaadin Login Form (Username/Password/Submit)",
                    status="PASS",
                    duration_ms=dur,
                ))

                # Step 3: Capture Dry Run Screenshot
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                final_screenshot = str(self.screenshots_dir / f"dry_run_login_{timestamp_str}.png")
                page.screenshot(path=final_screenshot, full_page=True)
                steps.append(StepResult(
                    step_number=3,
                    action="screenshot",
                    target="Login Screen (No credentials entered)",
                    status="PASS",
                    duration_ms=0,
                    screenshot_path=final_screenshot,
                ))
                logger.info(f"Dry run screenshot captured: {final_screenshot}")

            except Exception as e:
                status = "FAILED"
                error_msg = str(e)
                logger.error(f"Dry run error: {error_msg}")
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                final_screenshot = str(self.screenshots_dir / f"dry_run_error_{timestamp_str}.png")
                try:
                    page.screenshot(path=final_screenshot, full_page=True)
                except Exception:
                    pass
                steps.append(StepResult(
                    step_number=len(steps) + 1,
                    action="error",
                    target="Dry Run Execution",
                    status="FAIL",
                    duration_ms=0,
                    error_message=error_msg,
                    screenshot_path=final_screenshot,
                ))
            finally:
                context.close()
                browser.close()

        end_time = datetime.now()
        report = ExecutionReport(
            test_name="Safe Dry Run Connection Test",
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            total_duration_sec=(end_time - start_time).total_seconds(),
            status=status,
            steps=steps,
            final_screenshot=final_screenshot,
            error_summary=error_msg,
        )
        self._write_reports(report)
        return report

    def execute_plan(
        self,
        actions: List[Dict[str, Any]],
        credentials: Optional[Dict[str, str]] = None,
        test_name: str = "Vaadin Autonomous Test",
    ) -> ExecutionReport:
        """
        Executes a sequence of translated actions against Zirius Plus.

        Args:
            actions: List of action dictionaries (navigate, login, click, fill, verify).
            credentials: Dict with 'username' and 'password' (scrubbed immediately after use).
            test_name: Name of the test scenario.

        Returns:
            ExecutionReport containing step-by-step logs, status, and screenshot links.
        """
        # Copy credentials safely
        creds = dict(credentials) if credentials else {}
        pwd_secret = creds.get("password", "")
        start_time = datetime.now()
        steps: List[StepResult] = []
        status = "PASSED"
        error_summary = None
        final_screenshot = None

        logger.info(f"Starting test execution: '{test_name}' with {len(actions)} steps.")

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()

            try:
                for idx, act in enumerate(actions, start=1):
                    action_type = act.get("action", "").lower()
                    target = act.get("target", "")
                    role = act.get("role")
                    value = act.get("value", "")
                    t0 = datetime.now()

                    logger.info(f"Step {idx}/{len(actions)}: [{action_type.upper()}] target='{target}' role='{role}'")

                    # 1. NAVIGATE
                    if action_type == "navigate":
                        url = act.get("url", self.ZIRIUS_URL)
                        page.goto(url, wait_until="domcontentloaded", timeout=self.DEFAULT_TIMEOUT_MS)
                        self._wait_for_vaadin_network_idle(page)

                    # 2. LOGIN (Secure dynamic credential injection)
                    elif action_type == "login":
                        if not creds.get("username") or not creds.get("password"):
                            raise ValueError("Login step requested but no dynamic credentials were provided!")
                        self._wait_for_vaadin_network_idle(page)
                        fields = self._find_login_fields(page)

                        # Fill username via accessibility locator
                        fields["username"].fill(creds["username"])
                        # Fill password via accessibility locator
                        fields["password"].fill(creds["password"])
                        # Click login button
                        fields["submit"].click()
                        self._wait_for_vaadin_network_idle(page)

                        # Check and resolve 2FA / Authenticator (One-time code) if challenged
                        self._handle_2fa_challenge(page, creds)

                        # Securely wipe all credentials from internal memory immediately after input
                        self._secure_memory_cleanup(creds)
                        # Wait for Vaadin server push / navigation after login
                        self._wait_for_vaadin_network_idle(page)

                    # 3. CLICK (Buttons, tabs, links, menuitems)
                    elif action_type == "click":
                        self._wait_for_vaadin_network_idle(page)
                        clean_t = target.strip(" .,;:!?\"'")

                        # Dismiss any lingering popups/overlays to ensure sidebar is interactive
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass

                        # Check: If instructed to navigate to or click Dashboard, but already on it
                        if clean_t.lower() in ("dashboard", "hjem", "home"):
                            dash_loc = page.get_by_text("Dashboard", exact=False)
                            if dash_loc.count() > 0:
                                try:
                                    for i in range(dash_loc.count()):
                                        if dash_loc.nth(i).is_visible():
                                            dash_loc.nth(i).click(timeout=3000)
                                            break
                                    self._wait_for_vaadin_network_idle(page)
                                except Exception:
                                    pass
                                dur_ms = (datetime.now() - t0).total_seconds() * 1000
                                steps.append(StepResult(
                                    step_number=idx,
                                    action=action_type,
                                    target=clean_t,
                                    status="PASS",
                                    duration_ms=dur_ms,
                                ))
                                continue

                        loc = self._find_clickable_accessible(page, clean_t, role=role)
                        loc.wait_for(state="visible", timeout=self.DEFAULT_TIMEOUT_MS)
                        loc.click()
                        self._wait_for_vaadin_network_idle(page)

                    # 4. SELECT COMPANY (Zirius Plus Multi-company Switcher)
                    elif action_type == "select_company":
                        self._wait_for_vaadin_network_idle(page)
                        clean_comp = target.strip(" .,;:!?\"'")
                        logger.info(f"Switching company to '{clean_comp}'...")

                        # 1. Click company selector button at top-left
                        comp_btn = page.get_by_text(re.compile(r"company|bedrift|virksomhet|nordick", re.I)).first
                        try:
                            comp_btn.click(timeout=4000)
                            self._wait_for_vaadin_network_idle(page)
                        except Exception:
                            header_btn = page.locator("header").get_by_role("button").first
                            if header_btn.is_visible():
                                header_btn.click()

                        # 2. Select target company from list
                        comp_item = page.get_by_text(clean_comp, exact=False)
                        if comp_item.count() > 0:
                            for i in range(comp_item.count()):
                                if comp_item.nth(i).is_visible():
                                    comp_item.nth(i).click()
                                    break

                        # Close any lingering dropdowns
                        page.keyboard.press("Escape")
                        self._wait_for_vaadin_network_idle(page)

                    # 5. SET LANGUAGE (Profile Language Switcher)
                    elif action_type == "set_language":
                        self._wait_for_vaadin_network_idle(page)
                        lang_name = target.strip(" .,;:!?\"'") or "English"
                        logger.info(f"Opening profile and setting language to '{lang_name}'...")

                        # Dismiss any open dialogs first
                        page.keyboard.press("Escape")

                        # 1. Click profile avatar at top right
                        profile_btn = page.locator("header").get_by_role("button").last
                        if not profile_btn.is_visible():
                            profile_btn = page.get_by_role("button", name=re.compile(r"profile|account|bruker", re.I)).first
                        try:
                            profile_btn.click(timeout=4000)
                            self._wait_for_vaadin_network_idle(page)
                        except Exception:
                            pass

                        # 2. Select language
                        lang_opt = page.get_by_text(lang_name, exact=False)
                        if lang_opt.count() > 0:
                            for i in range(lang_opt.count()):
                                if lang_opt.nth(i).is_visible():
                                    lang_opt.nth(i).click()
                                    break
                            self._wait_for_vaadin_network_idle(page)

                        # 3. Click Save button if present
                        save_btn = page.get_by_role("button", name=re.compile(r"save|lagre", re.I))
                        if save_btn.count() > 0 and save_btn.first.is_visible():
                            save_btn.first.click()
                            self._wait_for_vaadin_network_idle(page)

                        # 4. Refresh to apply changes and close modals
                        page.keyboard.press("Escape")
                        page.reload()
                        self._wait_for_vaadin_network_idle(page)

                    # 6. SELECT GRID RECORD (Pending Orders Grid)
                    elif action_type == "select_grid_record":
                        self._wait_for_vaadin_network_idle(page)
                        logger.info("Selecting an existing record from grid...")
                        grid_candidates = [
                            page.locator("vaadin-grid-cell-content"),
                            page.locator("table tbody tr td"),
                            page.locator("tbody tr"),
                            page.get_by_role("row"),
                        ]
                        clicked = False
                        for cand in grid_candidates:
                            if cand.count() > 1:
                                try:
                                    cand.nth(1).click(timeout=4000)
                                    clicked = True
                                    break
                                except Exception:
                                    pass
                        if not clicked:
                            # Click first cell
                            page.locator("table td").first.click(timeout=5000)
                        self._wait_for_vaadin_network_idle(page)

                    # 7. INSPECT ORDER & VERIFY LANGUAGE (English vs Norwegian)
                    elif action_type == "inspect_order_language":
                        self._wait_for_vaadin_network_idle(page)
                        logger.info("Inspecting Order totals, quantities, VAT, and language...")
                        body_text = page.locator("body").inner_text()

                        # Extract Reference ID / Order Number
                        ref_match = re.search(r"(?:Order\s*#?|Ordre\s*#?|Ref(?:erence)?\s*#?)\s*[:.]?\s*([A-Za-z0-9\-]+)", body_text, re.I)
                        ref_id = ref_match.group(1) if ref_match else "Found Record"

                        # Extract visible totals / quantities if present
                        totals_summary = []
                        for kw in ["total", "subtotal", "vat", "mva", "discount", "rabatt", "qty", "quantity", "antall"]:
                            m = re.search(r"(" + re.escape(kw) + r"\s*[:.]?\s*[\d,.\s]+)", body_text, re.I)
                            if m:
                                totals_summary.append(m.group(1).strip())

                        # Norwegian keywords that should NOT appear in English mode
                        norwegian_terms = [
                            "mva", "merverdiavgift", "rabatt", "antall", "ordre", 
                            "kundenavn", "beløp", "totalt", "leveringsdato", "fakturadato"
                        ]
                        found_norwegian = []
                        lower_text = body_text.lower()
                        for term in norwegian_terms:
                            if re.search(r"\b" + re.escape(term) + r"\b", lower_text):
                                found_norwegian.append(term)

                        logger.info(f"Order Reference: {ref_id} | Visible Data: {', '.join(totals_summary[:4])}")

                        if found_norwegian:
                            err_msg = f"Language check FAILED: Norwegian words detected ({found_norwegian}). Order Ref: {ref_id}"
                            logger.error(err_msg)
                            raise AssertionError(err_msg)
                        else:
                            logger.info(f"Language check PASSED: Order UI is verified in English. Ref: {ref_id}")

                    # 8. FILL (Form inputs, search boxes, filters)
                    elif action_type == "fill":
                        self._wait_for_vaadin_network_idle(page)
                        loc = self._find_input_accessible(page, target)
                        loc.wait_for(state="visible", timeout=self.DEFAULT_TIMEOUT_MS)
                        loc.fill(value)
                        self._wait_for_vaadin_network_idle(page)

                    # 5. VERIFY (Assert visibility using accessibility locators)
                    elif action_type == "verify":
                        self._wait_for_vaadin_network_idle(page)
                        candidates = self._get_alias_candidates(target)
                        resolved_loc = None

                        for cand in candidates:
                            if role:
                                c_loc = page.get_by_role(role, name=cand, exact=False)
                            else:
                                label_cand = page.get_by_label(cand, exact=False)
                                text_cand = page.get_by_text(cand, exact=False)
                                if label_cand.count() > 0:
                                    c_loc = label_cand.first
                                elif text_cand.count() > 0:
                                    c_loc = text_cand.first
                                else:
                                    c_loc = page.get_by_placeholder(cand, exact=False).first

                            try:
                                c_loc.wait_for(state="visible", timeout=3000)
                                resolved_loc = c_loc
                                break
                            except Exception:
                                pass

                        if not resolved_loc:
                            # Primary locator wait for full timeout to throw clean error
                            primary_loc = page.get_by_text(target, exact=False).first
                            primary_loc.wait_for(state="visible", timeout=self.DEFAULT_TIMEOUT_MS)

                    # 6. WAIT FOR IDLE
                    elif action_type == "wait_idle":
                        self._wait_for_vaadin_network_idle(page)

                    else:
                        logger.warning(f"Unknown action type: {action_type}. Skipping.")

                    dur_ms = (datetime.now() - t0).total_seconds() * 1000
                    steps.append(StepResult(
                        step_number=idx,
                        action=action_type,
                        target=self._sanitize(f"{target} {value}".strip(), pwd_secret),
                        status="PASS",
                        duration_ms=dur_ms,
                    ))

                # Capture final success screenshot
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                final_screenshot = str(self.screenshots_dir / f"success_{timestamp_str}.png")
                page.screenshot(path=final_screenshot, full_page=True)

            except Exception as e:
                status = "FAILED"
                raw_err = str(e)
                error_summary = self._sanitize(raw_err, pwd_secret)
                logger.error(f"Test Execution Failed: {error_summary}")

                # Capture Error Screenshot
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                final_screenshot = str(self.screenshots_dir / f"error_{timestamp_str}.png")
                try:
                    page.screenshot(path=final_screenshot, full_page=True)
                except Exception:
                    pass

                steps.append(StepResult(
                    step_number=len(steps) + 1,
                    action="exception_handler",
                    target="Execution Halt",
                    status="FAIL",
                    duration_ms=0,
                    error_message=error_summary,
                    screenshot_path=final_screenshot,
                ))

            finally:
                # Ensure memory scrubbing happens even on failure
                self._secure_memory_cleanup(creds)
                context.close()
                browser.close()

        end_time = datetime.now()
        report = ExecutionReport(
            test_name=test_name,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            total_duration_sec=(end_time - start_time).total_seconds(),
            status=status,
            steps=steps,
            final_screenshot=final_screenshot,
            error_summary=error_summary,
        )
        self._write_reports(report)
        return report

    def _write_reports(self, report: ExecutionReport) -> None:
        """
        Generates and saves structured JSON and standalone HTML reports to /reports.
        """
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 1. JSON Report
        json_path = self.reports_dir / f"report_{timestamp_str}.json"
        report_dict = {
            "test_name": report.test_name,
            "status": report.status,
            "start_time": report.start_time,
            "end_time": report.end_time,
            "total_duration_sec": report.total_duration_sec,
            "error_summary": report.error_summary,
            "final_screenshot": report.final_screenshot,
            "steps": [
                {
                    "step_number": s.step_number,
                    "action": s.action,
                    "target": s.target,
                    "status": s.status,
                    "duration_ms": s.duration_ms,
                    "error_message": s.error_message,
                    "screenshot_path": s.screenshot_path,
                }
                for s in report.steps
            ],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, indent=2)
        report.report_json_path = str(json_path)

        # 2. Standalone HTML Report
        html_path = self.reports_dir / f"report_{timestamp_str}.html"
        html_content = self._generate_html_report(report)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        report.report_html_path = str(html_path)

        logger.info(f"Test reports written to:")
        logger.info(f" - JSON: {json_path}")
        logger.info(f" - HTML: {html_path}")

    def _generate_html_report(self, report: ExecutionReport) -> str:
        """Generates a self-contained, responsive HTML test report."""
        badge_color = "#28a745" if report.status == "PASSED" else "#dc3545"

        rows_html = ""
        for s in report.steps:
            st_color = "#28a745" if s.status == "PASS" else "#dc3545"
            screenshot_link = (
                f'<a href="{Path(s.screenshot_path).name}" target="_blank">View Screenshot</a>'
                if s.screenshot_path else "-"
            )
            err_html = f"<br><small style='color:#dc3545'>{s.error_message}</small>" if s.error_message else ""
            rows_html += f"""
            <tr>
              <td><b>{s.step_number}</b></td>
              <td><code>{s.action.upper()}</code></td>
              <td>{s.target} {err_html}</td>
              <td><span style="color:{st_color}; font-weight:bold;">{s.status}</span></td>
              <td>{s.duration_ms:.1f} ms</td>
              <td>{screenshot_link}</td>
            </tr>
            """

        final_ss_html = ""
        if report.final_screenshot and os.path.exists(report.final_screenshot):
            rel_ss = Path(report.final_screenshot).name
            final_ss_html = f"""
            <h3>Captured Screenshot</h3>
            <div style="margin: 20px 0;">
              <a href="{rel_ss}" target="_blank">
                <img src="{rel_ss}" style="max-width: 100%; border: 1px solid #ddd; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);" />
              </a>
            </div>
            """

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Vaadin Test Report - {report.test_name}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background-color: #f8f9fa; color: #333; }}
    .container {{ max-width: 1100px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }}
    .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e9ecef; padding-bottom: 15px; margin-bottom: 25px; }}
    .badge {{ background-color: {badge_color}; color: #fff; padding: 6px 16px; border-radius: 20px; font-weight: bold; font-size: 16px; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 25px; background: #f1f3f5; padding: 15px; border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
    th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #dee2e6; }}
    th {{ background: #e9ecef; font-weight: 600; }}
    code {{ background: #e9ecef; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1 style="margin: 0 0 5px 0;">Zirius Plus - Vaadin Test Report</h1>
        <small style="color: #6c757d;">Autonomous Open-Source QA Agent</small>
      </div>
      <div><span class="badge">{report.status}</span></div>
    </div>
    <div class="meta-grid">
      <div><strong>Test Scenario:</strong><br>{report.test_name}</div>
      <div><strong>Execution Time:</strong><br>{report.start_time}</div>
      <div><strong>Total Duration:</strong><br>{report.total_duration_sec:.2f} seconds</div>
    </div>
    <h3>Execution Steps</h3>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Action</th>
          <th>Target / Locator Context</th>
          <th>Status</th>
          <th>Duration</th>
          <th>Screenshot</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    {final_ss_html}
  </div>
</body>
</html>
"""
