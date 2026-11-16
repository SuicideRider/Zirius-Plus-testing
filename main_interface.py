"""
Main Interface for Autonomous Vaadin Testing Agent (Zirius Plus).

Provides a dual interface:
  1. Interactive CLI: Run via `python main_interface.py` (with getpass masking and flags).
  2. Local Web UI: Run via `streamlit run main_interface.py` or `python main_interface.py --gui`.

Features:
  - Dynamic credential management (passwords never saved to disk, scrubbed from memory).
  - Dry-run mode for safe connectivity & login screen verification.
  - Integration with Ollama local LLM and deterministic NLP fallback.
  - Automatic report generation (HTML, JSON, and screenshots).
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Dict, Optional

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

from llm_translator import LLMTranslator

try:
    from vaadin_engine import ExecutionReport, VaadinEngine
except ModuleNotFoundError as e:
    if "playwright" in str(e):
        print("\n" + "=" * 70)
        print("[!] CRITICAL DEPENDENCY MISSING: Playwright is not installed.")
        print("    Please run setup.bat or execute:")
        print("      pip install -r requirements.txt")
        print("      playwright install chromium")
        print("=" * 70 + "\n")
        sys.exit(1)
    raise

def ensure_playwright_installed():
    """Auto-installs Playwright Chromium binary on free Linux cloud hosts."""
    if sys.platform != "win32":
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        except Exception:
            pass


FEW_SHOT_EXAMPLE = (
    "Go to https://delta.zerp.no/, use the login credentials provided, "
    "navigate to the Dashboard, click 'New Order', and verify the 'Customer Name' field appears."
)


# ==============================================================================
# 1. STREAMLIT WEB INTERFACE
# ==============================================================================
def run_streamlit_app():
    """Renders the local Streamlit Web UI."""
    ensure_playwright_installed()
    import streamlit as st

    st.set_page_config(
        page_title="Vaadin Autonomous Testing Agent",
        page_icon="⚡",
        layout="wide",
    )

    st.title("⚡ Vaadin Autonomous Testing Agent (Zirius Plus)")
    st.markdown(
        "**Self-healing, 100% free local automation engine for Vaadin ERP systems.** "
        "Strictly uses Playwright accessibility locators with zero CSS/ID dependency."
    )

    translator = LLMTranslator()
    ollama_ready = translator.is_ollama_available()

    is_cloud_env = sys.platform != "win32" or bool(os.environ.get("PORT"))

    # Sidebar settings
    with st.sidebar:
        st.header("⚙️ Agent Settings")
        headless_mode = st.checkbox(
            "Headless Browser",
            value=True if is_cloud_env else False,
            help="Uncheck to watch the browser in real-time. Kept checked on cloud servers."
        )
        browser_choice = st.selectbox(
            "Browser Engine (SAC Compliant)",
            options=["Auto-detect", "msedge (Microsoft Edge)", "chrome (Google Chrome)"],
            help="Microsoft Edge and Google Chrome are officially signed system binaries that pass Windows Smart App Control."
        )
        channel_arg = "msedge" if "msedge" in browser_choice else ("chrome" if "chrome" in browser_choice else None)
        
        st.subheader("Translation Engine")
        if ollama_ready:
            st.success("🟢 Local Ollama LLM Detected")
            use_ollama = st.checkbox("Use Ollama LLM", value=True)
            model_name = st.text_input("Ollama Model", value="llama3")
            translator.model = model_name
        else:
            st.info("🟡 Ollama Offline - Using Built-in Deterministic Parser")
            use_ollama = False

        st.markdown("---")
        st.subheader("🛡️ Safe Dry Run")
        st.caption("Verifies network connection and captures login screen without credentials.")
        if st.button("🚀 Run Safe Dry Run", use_container_width=True):
            engine = VaadinEngine(reports_dir="reports", headless=headless_mode, browser_channel=channel_arg)
            with st.spinner("Executing connection and capturing login screen..."):
                rep = engine.run_dry_run()
            if rep.status == "PASSED":
                st.success("✅ Dry Run Completed Successfully!")
                if rep.final_screenshot and os.path.exists(rep.final_screenshot):
                    st.image(rep.final_screenshot, caption="Captured Login Screen", use_container_width=True)
            else:
                st.error(f"❌ Dry Run Failed: {rep.error_summary}")

    # Load persistent flows from flows.json
    flows_file = Path("flows.json")
    flows_dict = {}
    if flows_file.exists():
        try:
            with open(flows_file, "r", encoding="utf-8") as f:
                flows_dict = json.load(f)
        except Exception:
            pass
    if not flows_dict:
        flows_dict = {
            "New Sales Order": FEW_SHOT_EXAMPLE,
            "Customer Directory Lookup": "Go to https://delta.zerp.no/, use the login credentials provided, click 'Kunde', and verify the 'Kundenavn' field appears.",
            "Invoice Overview Audit": "Go to https://delta.zerp.no/, use the login credentials provided, click 'Faktura', and verify the 'Faktura' field appears.",
            "Safe Connection Dry Run": "Go to https://delta.zerp.no/, and verify the 'Logg inn' button appears."
        }

    # Main testing view
    col1, col2 = st.columns([3, 2])

    with col1:
        st.subheader("1. Test Flow Library & Scenario")

        flow_tab1, flow_tab2 = st.tabs(["🚀 Select & Run Flow", "➕ Add / Manage Flows"])

        with flow_tab1:
            flow_names = list(flows_dict.keys())
            selected_flow_name = st.selectbox(
                "Choose a Test Flow:",
                options=flow_names,
                index=0,
                key="flow_selector"
            )

            instruction_text = st.text_area(
                "Scenario instructions (editable):",
                value=flows_dict.get(selected_flow_name, ""),
                height=130,
                key=f"flow_text_{selected_flow_name}",
                help="You can tweak the instructions directly before clicking Execute."
            )

        with flow_tab2:
            st.markdown("**Create and Save a New Test Scenario**")
            with st.form("add_new_flow_form", clear_on_submit=True):
                new_flow_title = st.text_input("Flow Title", placeholder="e.g., Create Supplier Invoice")
                new_flow_prompt = st.text_area(
                    "English Test Scenario",
                    placeholder="e.g., Go to https://delta.zerp.no/, use the login credentials provided, click 'Leverandor', and verify 'Ny Leverandor' appears.",
                    height=100
                )
                submitted = st.form_submit_button("💾 Save to Flow Library", use_container_width=True)

                if submitted:
                    if new_flow_title.strip() and new_flow_prompt.strip():
                        flows_dict[new_flow_title.strip()] = new_flow_prompt.strip()
                        with open(flows_file, "w", encoding="utf-8") as f:
                            json.dump(flows_dict, f, indent=2)
                        st.success(f"✓ Saved flow '{new_flow_title}' successfully!")
                        st.rerun()
                    else:
                        st.error("Please enter both a Title and Scenario instructions.")

    with col2:
        st.subheader("2. Dynamic Credentials & 2FA")
        st.caption("Credentials are used strictly in-session and wiped from memory after execution.")
        username = st.text_input("Zirius Plus Email / Username", placeholder="e.g., dinesh.raj@zirius.in")
        password = st.text_input("Zirius Plus Password", type="password", placeholder="••••••••")
        totp_input = st.text_input(
            "Authenticator (2FA) Code or TOTP Secret Key (Optional)",
            type="password",
            placeholder="e.g., 123456 or Base32 Secret",
            help="Enter the current 6-digit Authenticator code, OR your 2FA Base32 Secret Key for 100% autonomous login."
        )

    st.markdown("---")
    run_btn = st.button("▶️ Execute Autonomous Test", type="primary", use_container_width=True)

    if run_btn:
        if not instruction_text.strip():
            st.warning("Please provide a test scenario instruction.")
            return

        with st.status("Running Autonomous Test Pipeline...", expanded=True) as status:
            st.write("🔄 Translating plain English scenario into Vaadin accessibility actions...")
            actions = translator.translate(instruction_text, prefer_ollama=use_ollama)
            st.write(f"✓ Generated {len(actions)} accessibility actions:")
            st.json(actions)

            credentials = {}
            if username and password:
                credentials = {"username": username, "password": password}
                if totp_input and totp_input.strip():
                    clean_totp = totp_input.strip().replace(" ", "")
                    if clean_totp.isdigit() and len(clean_totp) == 6:
                        credentials["otp"] = clean_totp
                    else:
                        credentials["totp_secret"] = clean_totp

            st.write("🌐 Launching hardened Playwright Vaadin engine...")
            engine = VaadinEngine(reports_dir="reports", headless=headless_mode, browser_channel=channel_arg)
            report = engine.execute_plan(actions, credentials=credentials, test_name="Zirius Plus Test")

            if report.status == "PASSED":
                status.update(label="✅ Test Completed: PASSED", state="complete")
            else:
                status.update(label="❌ Test Completed: FAILED", state="error")

        # Display results
        st.subheader("📊 Execution Results")
        res_col1, res_col2 = st.columns([3, 2])

        with res_col1:
            st.write(f"**Status:** `{report.status}` | **Duration:** `{report.total_duration_sec:.2f}s`")
            if report.error_summary:
                st.error(f"Error: {report.error_summary}")

            # Steps table
            steps_data = [
                {
                    "#": s.step_number,
                    "Action": s.action.upper(),
                    "Target": s.target,
                    "Status": s.status,
                    "Duration (ms)": f"{s.duration_ms:.1f}",
                }
                for s in report.steps
            ]
            st.dataframe(steps_data, use_container_width=True)

            if report.report_html_path:
                st.markdown(f"📄 **HTML Report:** `{report.report_html_path}`")
            if report.report_json_path:
                st.markdown(f"📦 **JSON Report:** `{report.report_json_path}`")

        with res_col2:
            if report.final_screenshot and os.path.exists(report.final_screenshot):
                st.image(report.final_screenshot, caption="Final Execution Screenshot", use_container_width=True)


# ==============================================================================
# 2. INTERACTIVE CLI INTERFACE
# ==============================================================================
def print_banner():
    """Prints standard ASCII banner for CLI."""
    print("=" * 75)
    print("   [+] VAADIN AUTONOMOUS TESTING AGENT - ZIRIUS PLUS QA SUITE [+]")
    print("   100% Free, Local, Self-Healing Playwright Automation Engine")
    print("   Zero CSS/ID Locators | Strict Accessibility | Pure Event Waiting")
    print("=" * 75)


def run_cli_interactive(args: argparse.Namespace):
    """Executes the interactive command-line interface."""
    print_banner()

    translator = LLMTranslator(ollama_url=args.ollama_url, model=args.model)
    ollama_ready = translator.is_ollama_available()

    if ollama_ready:
        print("[AI] Local Ollama LLM detected (model: {})".format(args.model))
    else:
        print("[AI] Ollama not detected. Using built-in deterministic NLP parser.")

    # Check for direct Dry Run flag
    if args.dry_run:
        print("\n[*] Launching Safe Dry Run against https://delta.zerp.no/...")
        engine = VaadinEngine(reports_dir="reports", headless=args.headless, browser_channel=args.browser)
        report = engine.run_dry_run()
        _print_cli_report(report)
        return

    # Check if input file was provided
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[!] Error: Specified input file '{file_path}' does not exist.")
            sys.exit(1)
        with open(file_path, "r", encoding="utf-8") as f:
            user_instruction = f.read().strip()
        print(f"\n[+] Loaded test scenario from '{file_path}':")
        print(f"    {user_instruction}\n")
    else:
        print("\nSelect an option:")
        print("  1. Run Few-Shot Example Scenario")
        print("  2. Type custom test scenario in plain English")
        print("  3. Run Safe Dry Run connection test (no credentials needed)")
        choice = input("\nChoice [1-3] (default: 1): ").strip() or "1"

        if choice == "3":
            print("\n[*] Launching Safe Dry Run against https://delta.zerp.no/...")
            engine = VaadinEngine(reports_dir="reports", headless=args.headless, browser_channel=args.browser)
            report = engine.run_dry_run()
            _print_cli_report(report)
            return
        elif choice == "2":
            print("\nEnter your test scenario in plain English (or paste Playwright actions):")
            user_instruction = input("> ").strip()
            if not user_instruction:
                print("[!] No instruction provided. Exiting.")
                return
        else:
            user_instruction = FEW_SHOT_EXAMPLE
            print(f"\n[+] Using Few-Shot Scenario:\n    {user_instruction}")

    # Determine if login credentials are required
    credentials = {}
    if any(k in user_instruction.lower() for k in ["login", "credential", "sign in", "auth"]):
        print("\n--- Dynamic Credential & 2FA Input (Security: Never Stored to Disk) ---")
        username = input("Enter Zirius Plus Email / Username: ").strip()
        password = getpass.getpass("Enter Zirius Plus Password (hidden): ").strip()
        totp_input = getpass.getpass("Enter Authenticator 6-digit Code or TOTP Secret Key (optional, press Enter to skip): ").strip()
        credentials = {"username": username, "password": password}
        if totp_input:
            clean_totp = totp_input.replace(" ", "")
            if clean_totp.isdigit() and len(clean_totp) == 6:
                credentials["otp"] = clean_totp
            else:
                credentials["totp_secret"] = clean_totp

    # Translate instruction
    print("\n[*] Translating scenario into Vaadin accessibility actions...")
    actions = translator.translate(user_instruction, prefer_ollama=ollama_ready)
    print(f"[+] Action Plan Generated ({len(actions)} steps):")
    for i, a in enumerate(actions, start=1):
        print(f"    Step {i}: {a.get('action', '').upper()} -> target='{a.get('target', '')}' role='{a.get('role', '')}'")

    # Execute
    print("\n[*] Launching Playwright Vaadin Execution Engine...")
    engine = VaadinEngine(reports_dir="reports", headless=args.headless, browser_channel=args.browser)
    report = engine.execute_plan(actions, credentials=credentials, test_name="Zirius Plus Test")

    _print_cli_report(report)


def _print_cli_report(report: ExecutionReport):
    """Outputs a clean terminal summary of the execution."""
    print("\n" + "=" * 75)
    print(f"TEST EXECUTION SUMMARY: {report.status}")
    print("=" * 75)
    print(f"  Test Name:       {report.test_name}")
    print(f"  Total Duration:  {report.total_duration_sec:.2f} seconds")
    print(f"  Start Time:      {report.start_time}")
    print(f"  End Time:        {report.end_time}")

    if report.error_summary:
        print(f"  Error Summary:   {report.error_summary}")

    print("\n  Steps Executed:")
    for s in report.steps:
        st_icon = "[PASS]" if s.status == "PASS" else "[FAIL]"
        print(f"    {st_icon} Step {s.step_number}: {s.action.upper():<10} | {s.target:<35} | {s.duration_ms:>6.1f}ms")

    print("\n  Generated Artifacts:")
    if report.report_html_path:
        print(f"    [HTML Report]: {report.report_html_path}")
    if report.report_json_path:
        print(f"    [JSON Report]: {report.report_json_path}")
    if report.final_screenshot:
        print(f"    [Screenshot] : {report.final_screenshot}")
    print("=" * 75 + "\n")


# ==============================================================================
# 3. ENTRY POINT
# ==============================================================================
def main():
    """Main CLI entry point."""
    # Check if invoked via Streamlit
    try:
        import streamlit as st
        if st.runtime.exists():
            run_streamlit_app()
            return
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Autonomous Vaadin Testing Agent targeting Zirius Plus (delta.zerp.no)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Execute safe connection and login screen capture.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--browser", type=str, default=None, choices=["msedge", "chrome"], help="Browser channel ('msedge' or 'chrome') to pass Windows Smart App Control.")
    parser.add_argument("--file", type=str, help="Path to text file containing test instructions.")
    parser.add_argument("--model", type=str, default="llama3", help="Ollama local model name.")
    parser.add_argument("--ollama-url", type=str, default=None, help="Ollama API URL.")
    parser.add_argument("--gui", action="store_true", help="Launch Streamlit Web UI.")

    args = parser.parse_args()

    if args.gui:
        print("[*] Launching Streamlit Web UI...")
        subprocess.run([sys.executable, "-m", "streamlit", "run", __file__])
    else:
        run_cli_interactive(args)


if __name__ == "__main__":
    main()
