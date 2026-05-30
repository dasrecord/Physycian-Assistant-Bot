"""
emr/oscar.py
Automates OSCAR Pro via Brave browser + Selenium remote-debugging.
Architecture mirrors billing_bot.py — same Brave setup pattern.

Configure in .env:
    OSCAR_URL       = https://your-clinic.oscarpro.com/oscar/login.do
    OSCAR_USERNAME  = your_username
    OSCAR_PASSWORD  = your_password

OSCAR Pro selector notes
────────────────────────
OSCAR Pro's HTML changes between hosted versions.  The selectors below
work for typical OSCAR Pro cloud deployments.  If your clinic has a
customised theme, open DevTools on the patient search / encounter pages
and update the CSS_* constants at the top of this file.
"""

import os
import sys
import time
import socket
import shutil
import tempfile
import subprocess
import logging

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

from config import OSCAR_URL, OSCAR_USERNAME, OSCAR_PASSWORD

log = logging.getLogger(__name__)

# ── Configurable selectors ────────────────────────────────────────────────────
# Update these if your OSCAR Pro version uses different IDs / names.

# Login page
CSS_LOGIN_USER  = "input[name='username'], input[id='username']"
CSS_LOGIN_PASS  = "input[name='password'], input[id='password']"
CSS_LOGIN_BTN   = "input[type='submit'], button[type='submit']"

# Patient search (provider home / search bar)
CSS_SEARCH_INPUT = "input[name='srchname'], input[id='srchname'], input[placeholder*='patient' i]"
CSS_SEARCH_BTN   = "input[value='Search'], button[contains(text(),'Search')]"
# First row of search results table
XPATH_FIRST_PT   = "(//table[contains(@class,'grid') or contains(@class,'result')]//tr[not(th)][1]//a)[1]"

# Encounter / note
XPATH_NEW_ENC    = (
    "//a[contains(text(),'New Encounter') or contains(text(),'Add Note') "
    "or contains(text(),'Encounter Notes') or @id='addEncounterNote']"
)
# Note text area inside the encounter window
XPATH_NOTE_AREA  = (
    "//textarea[@name='textNote' or @id='textNote' "
    "or @name='note' or @id='note' or contains(@class,'noteText')]"
)
XPATH_SAVE_BTN   = (
    "//input[@value='Save' or @value='Sign' or @value='Save Note'] "
    "| //button[contains(text(),'Save') or contains(text(),'Sign')]"
)

REMOTE_DEBUG_PORT = 9224   # separate port from billing bot (9222) and upload bot (9223)


class OscarEMR:
    """Posts SOAP notes to OSCAR Pro using Brave browser automation."""

    def __init__(self):
        self._driver        = None
        self._brave_process = None

    # ── Public ────────────────────────────────────────────────────────────────

    def post_note(self, patient_name: str, patient_dob: str, soap_note) -> dict:
        """
        Find the patient in OSCAR Pro and create an encounter note.

        Parameters
        ----------
        patient_name : str   e.g. "Smith, John" or "John Smith"
        patient_dob  : str   e.g. "1985-03-22" (YYYY-MM-DD)
        soap_note    : dict | str   structured dict or plain text

        Returns
        -------
        {"success": bool, "message": str, "error": str}
        """
        if not OSCAR_URL or not OSCAR_USERNAME:
            return {
                "success": False,
                "error": "OSCAR_URL / OSCAR_USERNAME not set in .env",
            }

        try:
            self._setup_driver()
            wait = WebDriverWait(self._driver, 20)

            self._login(wait)
            self._search_patient(wait, patient_name, patient_dob)
            self._open_new_encounter(wait)
            note_text = self._format_note(soap_note)
            self._enter_note(wait, note_text)
            self._save_note(wait)

            return {"success": True, "message": "Note saved to OSCAR Pro."}

        except Exception as exc:
            log.exception("OSCAR posting failed")
            return {"success": False, "error": str(exc)}
        finally:
            self._cleanup()

    # ── Driver setup (mirrors billing_bot.py pattern) ─────────────────────────

    def _setup_driver(self):
        brave_path = self._find_brave()
        brave_dir  = os.path.join(tempfile.gettempdir(), "brave_oscar")

        if os.path.exists(brave_dir):
            shutil.rmtree(brave_dir, ignore_errors=True)

        args = [
            brave_path,
            f"--remote-debugging-port={REMOTE_DEBUG_PORT}",
            f"--user-data-dir={brave_dir}",
            "--window-size=1280,900",
        ]
        self._brave_process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for debug port
        for _ in range(15):
            try:
                s = socket.socket()
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", REMOTE_DEBUG_PORT)) == 0:
                    s.close()
                    break
            except Exception:
                pass
            time.sleep(1)

        opts = webdriver.ChromeOptions()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{REMOTE_DEBUG_PORT}")

        driver_path = ChromeDriverManager().install()
        # webdriver-manager sometimes returns path to THIRD_PARTY_NOTICES instead of binary
        if "THIRD_PARTY_NOTICES" in driver_path:
            d = os.path.dirname(driver_path)
            exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
            driver_path = os.path.join(d, exe)

        service = ChromeService(driver_path)
        self._driver = webdriver.Chrome(service=service, options=opts)
        self._driver.set_page_load_timeout(30)

    @staticmethod
    def _find_brave() -> str:
        if sys.platform == "win32":
            candidates = [
                os.path.join(
                    os.environ.get("LOCALAPPDATA", ""),
                    "BraveSoftware", "Brave-Browser", "Application", "brave.exe",
                ),
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
            ]
        else:
            candidates = ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]

        path = next((p for p in candidates if os.path.exists(p)), None)
        if not path:
            raise RuntimeError(
                "Brave Browser not found.  Install from https://brave.com"
            )
        return path

    # ── OSCAR workflow steps ──────────────────────────────────────────────────

    def _login(self, wait: WebDriverWait):
        self._driver.get(OSCAR_URL)
        time.sleep(1)

        # Already logged in?
        if "login" not in self._driver.current_url.lower():
            return

        user_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, CSS_LOGIN_USER)))
        user_el.clear()
        user_el.send_keys(OSCAR_USERNAME)

        pass_el = self._driver.find_element(By.CSS_SELECTOR, CSS_LOGIN_PASS)
        pass_el.clear()
        pass_el.send_keys(OSCAR_PASSWORD)

        self._driver.find_element(By.CSS_SELECTOR, CSS_LOGIN_BTN).click()
        time.sleep(2)

    def _search_patient(self, wait: WebDriverWait, patient_name: str, patient_dob: str):
        # Try the search box (works in most OSCAR Pro layouts)
        try:
            search = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, CSS_SEARCH_INPUT)))
            search.clear()
            # OSCAR search: enter last name or "Last, First"
            search.send_keys(patient_name)
            search.send_keys(Keys.RETURN)
            time.sleep(2)
        except TimeoutException:
            raise RuntimeError(
                "Could not find patient search box.  "
                "Check CSS_SEARCH_INPUT selector in emr/oscar.py."
            )

        # Click the first matching result
        try:
            first = wait.until(EC.element_to_be_clickable((By.XPATH, XPATH_FIRST_PT)))
            first.click()
            time.sleep(2)
        except TimeoutException:
            raise RuntimeError(
                f"No patient found for '{patient_name}'.  "
                "Search returned no results or selector needs updating."
            )

    def _open_new_encounter(self, wait: WebDriverWait):
        main_window = self._driver.current_window_handle
        try:
            enc_link = wait.until(EC.element_to_be_clickable((By.XPATH, XPATH_NEW_ENC)))
            enc_link.click()
            time.sleep(2)
        except TimeoutException:
            raise RuntimeError(
                "Could not find 'New Encounter' link.  "
                "Update XPATH_NEW_ENC in emr/oscar.py."
            )

        # If a new window/tab opened, switch to it
        if len(self._driver.window_handles) > 1:
            for handle in self._driver.window_handles:
                if handle != main_window:
                    self._driver.switch_to.window(handle)
                    break
            time.sleep(1)

    def _enter_note(self, wait: WebDriverWait, note_text: str):
        try:
            area = wait.until(EC.presence_of_element_located((By.XPATH, XPATH_NOTE_AREA)))
            area.clear()
            # Use JavaScript to set value — faster for large blocks of text
            self._driver.execute_script(
                "arguments[0].value = arguments[1];", area, note_text
            )
            # Trigger change event so OSCAR registers the update
            self._driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", area
            )
        except TimeoutException:
            raise RuntimeError(
                "Could not find note textarea.  "
                "Update XPATH_NOTE_AREA in emr/oscar.py."
            )

    def _save_note(self, wait: WebDriverWait):
        try:
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, XPATH_SAVE_BTN)))
            btn.click()
            time.sleep(2)
        except TimeoutException:
            raise RuntimeError(
                "Could not find Save/Sign button.  "
                "Update XPATH_SAVE_BTN in emr/oscar.py."
            )

    # ── Formatting ────────────────────────────────────────────────────────────

    @staticmethod
    def _format_note(soap_note) -> str:
        """Convert the structured SOAP dict to plain text for the OSCAR note field."""
        if isinstance(soap_note, str):
            return soap_note

        if not isinstance(soap_note, dict):
            return str(soap_note)

        parts = []
        mapping = [
            ("subjective",   "SUBJECTIVE"),
            ("objective",    "OBJECTIVE"),
            ("assessment",   "ASSESSMENT"),
            ("differential", "DIFFERENTIAL DIAGNOSIS"),
            ("plan",         "PLAN"),
        ]
        for key, header in mapping:
            content = soap_note.get(key, "").strip()
            if content:
                parts.append(f"{header}:\n{content}\n")

        icd9 = soap_note.get("icd9_codes", [])
        if icd9:
            parts.append(f"\nICD9 CODES: {', '.join(icd9)}")

        return "\n".join(parts)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _cleanup(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

        if self._brave_process:
            try:
                self._brave_process.terminate()
                self._brave_process.wait(timeout=5)
            except Exception:
                try:
                    self._brave_process.kill()
                except Exception:
                    pass
            self._brave_process = None
