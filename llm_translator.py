"""
Translation Layer for Autonomous Vaadin Testing Agent.

Converts plain English testing scenarios into structured, executable test actions.
Supports:
  1. Local Ollama LLM (100% private, free local inference).
  2. Built-in Deterministic NLP Parser (zero-dependency fallback that works out of the box).
  3. Direct Playwright / JSON instruction interpreter for prompts generated via ChatGPT.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

logger = logging.getLogger("vaadin_agent.translator")


class TestAction:
    """Represents a discrete executable action in the Vaadin testing engine."""
    NAVIGATE = "navigate"
    LOGIN = "login"
    CLICK = "click"
    FILL = "fill"
    VERIFY = "verify"
    WAIT_IDLE = "wait_idle"
    SELECT_COMPANY = "select_company"
    SET_LANGUAGE = "set_language"
    SELECT_GRID_RECORD = "select_grid_record"
    INSPECT_ORDER_LANGUAGE = "inspect_order_language"


class LLMTranslator:
    """
    Translates plain English test instructions into structured action specifications
    strictly complying with Playwright accessibility locators and Vaadin principles.
    """

    DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
    DEFAULT_MODEL = "llama3"

    SYSTEM_PROMPT = """
You are an expert QA Automation Engineer specialized in stateful Vaadin applications.
Convert the user's natural language test scenario into a strict JSON array of executable test actions.

Available actions:
1. {"action": "navigate", "url": "<URL>"}
2. {"action": "login"} - Authenticates using securely injected credentials.
3. {"action": "click", "target": "<text or accessible name>", "role": "<button|link|menuitem|tab|None>"}
4. {"action": "fill", "target": "<label or placeholder>", "value": "<text to input>"}
5. {"action": "verify", "target": "<text or label to verify visible>", "role": "<textbox|heading|cell|None>"}
6. {"action": "wait_idle"}

RULES:
- NEVER use CSS selectors or HTML IDs. All targets must be human-readable accessibility names (labels, placeholders, visible text, or ARIA roles).
- Output ONLY valid JSON containing the array. Do not output explanatory text.
Example input: "Go to https://delta.zerp.no/, use the login credentials provided, navigate to the Dashboard, click 'New Order', and verify the 'Customer Name' field appears."
Example output:
[
  {"action": "navigate", "url": "https://delta.zerp.no/"},
  {"action": "login"},
  {"action": "click", "target": "Dashboard", "role": "link"},
  {"action": "click", "target": "New Order", "role": "button"},
  {"action": "verify", "target": "Customer Name", "role": "textbox"}
]
"""

    def __init__(self, ollama_url: Optional[str] = None, model: Optional[str] = None):
        """
        Initialize the translator.

        Args:
            ollama_url: URL to the local Ollama instance (defaults to http://localhost:11434/api/generate).
            model: Ollama model name (defaults to llama3).
        """
        self.ollama_url = ollama_url or self.DEFAULT_OLLAMA_URL
        self.model = model or self.DEFAULT_MODEL

    def is_ollama_available(self) -> bool:
        """Check if local Ollama daemon is active and responding."""
        try:
            base_url = self.ollama_url.rsplit("/api/", 1)[0]
            req = urllib.request.Request(f"{base_url}/api/tags", headers={"User-Agent": "VaadinAgent/1.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def translate(self, user_instruction: str, prefer_ollama: bool = True) -> List[Dict[str, Any]]:
        """
        Translate plain English or raw input into structured action steps.

        Args:
            user_instruction: Raw instruction text provided by user.
            prefer_ollama: Whether to try local Ollama first before deterministic fallback.

        Returns:
            List of action dictionaries.
        """
        clean_input = user_instruction.strip()

        # Check if user directly pasted raw JSON actions
        if clean_input.startswith("[") and clean_input.endswith("]"):
            try:
                parsed = json.loads(clean_input)
                if isinstance(parsed, list):
                    logger.info("Direct JSON action plan detected.")
                    return parsed
            except json.JSONDecodeError:
                pass

        # Check if user pasted ChatGPT-generated Playwright code
        if "page.get_by" in clean_input or "page.goto" in clean_input:
            logger.info("Playwright code snippet detected. Extracting actions...")
            return self._parse_playwright_snippet(clean_input)

        # Attempt Ollama local translation if available and requested
        if prefer_ollama and self.is_ollama_available():
            try:
                logger.info(f"Using local Ollama model ({self.model}) for translation...")
                plan = self._translate_with_ollama(clean_input)
                if plan:
                    return plan
            except Exception as e:
                logger.warning(f"Ollama translation failed ({e}); switching to deterministic parser.")

        # Deterministic Rule-Based Fallback (Zero external dependencies)
        logger.info("Using built-in deterministic NLP intent parser.")
        return self._deterministic_nlp_parse(clean_input)

    def _translate_with_ollama(self, prompt: str) -> Optional[List[Dict[str, Any]]]:
        """Call local Ollama model to generate structured action plan."""
        payload = {
            "model": self.model,
            "prompt": f"{self.SYSTEM_PROMPT}\n\nUser Instruction: {prompt}\n\nJSON Output:",
            "stream": False,
            "format": "json"
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.ollama_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "VaadinAgent/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    resp_body = json.loads(response.read().decode("utf-8"))
                    result = resp_body.get("response", "")
                    match = re.search(r"\[.*\]", result, re.DOTALL)
                    if match:
                        return json.loads(match.group(0))
        except Exception as e:
            logger.warning(f"Ollama request error: {e}")
        return None

    def _clean_target(self, text: str) -> str:
        """Strips punctuation, articles, and noise from extracted targets."""
        cleaned = text.strip().strip(" .,;:!?\"'")
        if cleaned.lower().startswith("the "):
            cleaned = cleaned[4:].strip()
        if cleaned.lower().startswith("company "):
            cleaned = cleaned[8:].strip()
        cleaned = re.sub(r"\s+if applicable.*$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned.strip(" .,;:!?\"'")

    def _deterministic_nlp_parse(self, prompt: str) -> List[Dict[str, Any]]:
        """
        Deterministic, zero-dependency natural language parser.
        Robustly interprets standard QA scenarios, numbered test steps,
        and ERP domain actions in exact sequential order.
        """
        actions: List[Dict[str, Any]] = []

        # Split instruction into lines and process in exact sequence
        lines = prompt.splitlines()
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            # Remove leading numbering (e.g. "1. ", "2) ", "Step 1: ")
            line_clean = re.sub(r"^(?:step\s*)?\d+[\.\):\-]\s*", "", line_str, flags=re.IGNORECASE).strip()
            if not line_clean:
                continue

            # Check for menu navigation patterns like "Navigate: Sale - Pending Orders"
            menu_match = re.search(r"(?:navigate|menu)[:\s]+([^->–\n]+?)\s*[-–>]\s*([^.,;\n]+)", line_clean, re.IGNORECASE)
            if menu_match:
                m1 = self._clean_target(menu_match.group(1))
                m2 = self._clean_target(menu_match.group(2))
                actions.append({"action": TestAction.CLICK, "target": m1, "role": None})
                actions.append({"action": TestAction.CLICK, "target": m2, "role": None})
                continue

            # Check for grid selection pattern
            if re.search(r"(?:open grid|select\s+(?:an?\s+)?existing record|select\s+record|click\s+first\s+row)", line_clean, re.IGNORECASE):
                actions.append({"action": TestAction.SELECT_GRID_RECORD})
                continue

            # Check for inspection and language verification
            if re.search(r"(?:inspect|audit|check).*(?:totals?|vat|quantity|discount).*(?:norwegian|language).*(?:fail|pass)", line_clean, re.IGNORECASE):
                actions.append({
                    "action": TestAction.INSPECT_ORDER_LANGUAGE,
                    "target": "Order Totals & Details",
                    "forbidden_language": "norwegian"
                })
                continue

            # Check for Select Company in Delta
            company_match = re.search(r"(?:select company|in delta select company|switch company to)\s+['\"]?([^'\",;]+?)['\"]?$", line_clean, re.IGNORECASE)
            if company_match:
                comp_name = self._clean_target(company_match.group(1))
                actions.append({
                    "action": TestAction.SELECT_COMPANY,
                    "target": comp_name
                })
                continue

            # Check for Profile and Language selection
            if re.search(r"(?:open profile|select english|profile.*language|language.*english)", line_clean, re.IGNORECASE):
                actions.append({
                    "action": TestAction.SET_LANGUAGE,
                    "target": "English"
                })
                continue

            # Otherwise split sub-clauses on semicolons or commas
            sub_clauses = re.split(r"(?:;|\. |\band then\b|\bthen\b|, and\b|,)", line_clean, flags=re.IGNORECASE)
            for sc in sub_clauses:
                clause = sc.strip()
                if not clause:
                    continue

                # 1. Navigation / Go to URL
                nav_match = re.search(r"(?:go to|navigate to|open)\s+(https?://[^\s,'\"]+)", clause, re.IGNORECASE)
                if nav_match:
                    actions.append({
                        "action": TestAction.NAVIGATE,
                        "url": nav_match.group(1).rstrip(" .,;:")
                    })
                    continue

                # 2. Login step
                if re.search(r"(?:login|log in|credentials|sign in|authenticate)", clause, re.IGNORECASE):
                    actions.append({
                        "action": TestAction.LOGIN
                    })
                    continue

                # 3. Fill / Enter input
                fill_match = re.search(
                    r"(?:fill|enter|type|input)\s+['\"]?([^'\"]+?)['\"]?\s+(?:into|in|with)\s+['\"]?([^'\"]+?)['\"]?$",
                    clause, re.IGNORECASE
                )
                if fill_match:
                    g1, g2 = fill_match.group(1).strip(), fill_match.group(2).strip()
                    if "with" in clause.lower():
                        target, val = g1, g2
                    else:
                        target, val = g2, g1
                    actions.append({
                        "action": TestAction.FILL,
                        "target": self._clean_target(target),
                        "value": val
                    })
                    continue

                # 4. Verify / Assert visibility
                verify_match = re.search(
                    r"(?:verify|check|assert|ensure)\s+(?:the\s+)?['\"]?([^'\"]+?)['\"]?\s+(?:field|button|link|element)?\s*(?:appears|is visible|exists|shows)",
                    clause, re.IGNORECASE
                )
                if not verify_match:
                    verify_match = re.search(r"(?:verify|check|assert|ensure)\s+['\"]([^'\"]+)['\"]", clause, re.IGNORECASE)

                if verify_match:
                    target_name = self._clean_target(verify_match.group(1))
                    actions.append({
                        "action": TestAction.VERIFY,
                        "target": target_name,
                        "role": None
                    })
                    continue

                # 5. Click / Press
                click_match = re.search(r"(?:click|press|select|navigate to)\s+['\"]?([^'\"]+?)['\"]?$", clause, re.IGNORECASE)
                if click_match:
                    target_name = self._clean_target(click_match.group(1))
                    if target_name.lower() not in ["it", "here", "button", "delta"]:
                        actions.append({
                            "action": TestAction.CLICK,
                            "target": target_name,
                            "role": None
                        })
                        continue

        return actions

    def _parse_playwright_snippet(self, code: str) -> List[Dict[str, Any]]:
        """
        Parses Playwright python lines (e.g., from ChatGPT) into action dictionaries.
        Guarantees that CSS/ID selectors are rejected and only accessibility locators are used.
        """
        actions: List[Dict[str, Any]] = []
        lines = code.splitlines()

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # goto
            goto_m = re.search(r"page\.goto\(['\"]([^'\"]+)['\"]\)", line)
            if goto_m:
                actions.append({"action": TestAction.NAVIGATE, "url": goto_m.group(1)})
                continue

            # click with get_by_role / get_by_text / get_by_label
            click_role_m = re.search(r"page\.get_by_role\(['\"]([^'\"]+)['\"],\s*name=['\"]([^'\"]+)['\"]\)\.click", line)
            if click_role_m:
                actions.append({
                    "action": TestAction.CLICK,
                    "role": click_role_m.group(1),
                    "target": click_role_m.group(2)
                })
                continue

            click_text_m = re.search(r"page\.get_by_text\(['\"]([^'\"]+)['\"]\)\.click", line)
            if click_text_m:
                actions.append({"action": TestAction.CLICK, "target": click_text_m.group(1), "role": None})
                continue

            # fill with get_by_label / get_by_placeholder / get_by_role
            fill_m = re.search(r"page\.get_by_(?:label|placeholder)\(['\"]([^'\"]+)['\"]\)\.fill\(['\"]([^'\"]+)['\"]\)", line)
            if fill_m:
                actions.append({"action": TestAction.FILL, "target": fill_m.group(1), "value": fill_m.group(2)})
                continue

            # expect / verify
            expect_m = re.search(r"get_by_(?:text|label|role)\(['\"]?([^'\")]+)['\"]?.*\.to_be_visible", line)
            if expect_m:
                actions.append({"action": TestAction.VERIFY, "target": expect_m.group(1).split(",")[-1].strip().strip("'\"")})
                continue

        return actions
