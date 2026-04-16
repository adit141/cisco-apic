#!/usr/bin/env python3
"""
=============================================================================
  Cisco ACI - vPC Deployment Automation
  Version : 1.0.0
=============================================================================
  Automates the following tasks towards Cisco APIC:
    1. Create vPC Interface Policy Group (IPG)
    2. Assign interface selector to IPG on both leaf nodes
    3. Push EPG Static Path Binding (vPC / protpaths)

  Usage:
    python aci_deploy.py

  Excel template: ACI_VPC_DEPLOY_Template.xlsx
  Run 'python create_template.py' to generate a blank template.
=============================================================================
"""

import sys
import json
import getpass
import logging
import time
from datetime import datetime
from functools import wraps

import requests
import pandas as pd
import urllib3

# ── Suppress SSL certificate warnings ────────────────────────────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Logging Setup ─────────────────────────────────────────────────────────────
_LOG_FILE = f"aci_deploy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_LOG_FMT  = "%(asctime)s [%(levelname)-8s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FMT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("aci_deploy")


# ── Global Configuration ──────────────────────────────────────────────────────
EXCEL_FILE  = "ACI_VPC_DEPLOY_Template.xlsx"   # Input Excel filename
MAX_RETRIES = 3                                 # Max retry attempts on failure
RETRY_DELAY = 5                                 # Seconds between retries
TIMEOUT     = 30                                # HTTP request timeout (seconds)


# ── Retry Decorator ───────────────────────────────────────────────────────────
def retry(max_retries: int = MAX_RETRIES, delay: int = RETRY_DELAY):
    """
    Decorator that retries a function on exception.
    - Skips retry on HTTP 4xx (client/config error, not transient)
    - Retries on HTTP 5xx (server error) and network exceptions
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.HTTPError as exc:
                    # 4xx = wrong payload / not found → no point retrying
                    if exc.response is not None and 400 <= exc.response.status_code < 500:
                        log.error(
                            f"[{func.__name__}] HTTP {exc.response.status_code} – "
                            "client error, not retrying."
                        )
                        raise
                    last_exc = exc
                except Exception as exc:
                    last_exc = exc

                if attempt < max_retries:
                    log.warning(
                        f"[{func.__name__}] Attempt {attempt}/{max_retries} failed: "
                        f"{last_exc}. Retrying in {delay}s…"
                    )
                    time.sleep(delay)
                else:
                    log.error(
                        f"[{func.__name__}] All {max_retries} attempts exhausted."
                    )
                    raise last_exc

        return wrapper
    return decorator


# ── APIC Client ───────────────────────────────────────────────────────────────
class APICClient:
    """
    Thin, stateful REST client for Cisco APIC.
    Maintains a requests.Session with the APIC authentication cookie across
    all subsequent calls after login().
    """

    def __init__(self, apic_ip: str, username: str, password: str):
        self.base_url = f"https://{apic_ip}"
        self.username = username
        self.password = password

        self._session         = requests.Session()
        self._session.verify  = False       # SSL verify disabled – see urllib3 suppression above
        self._session.headers.update({"Content-Type": "application/json"})

    # ──────────────────────────────────────────────────────────────────────────
    #  Task 0 – Login
    # ──────────────────────────────────────────────────────────────────────────
    @retry()
    def login(self) -> bool:
        """
        POST /api/aaaLogin.json
        Stores the APIC-cookie token in the session for all subsequent calls.
        """
        url = f"{self.base_url}/api/aaaLogin.json"
        payload = {
            "aaaUser": {
                "attributes": {
                    "name": self.username,
                    "pwd":  self.password,
                }
            }
        }

        log.info(f"Connecting to APIC: {self.base_url}  (user: {self.username})")
        resp = self._session.post(url, json=payload, timeout=TIMEOUT)

        if resp.status_code == 200:
            try:
                token = resp.json()["imdata"][0]["aaaLogin"]["attributes"]["token"]
                # Attach token as both cookie and header for maximum compatibility
                self._session.cookies.set("APIC-cookie", token)
                self._session.headers.update({"APIC-cookie": token})
                log.info("Login SUCCESS")
                return True
            except (KeyError, IndexError) as exc:
                log.error(f"Login response parse error: {exc}")
                return False

        self._log_apic_error("LOGIN", url, resp)
        return False

    # ──────────────────────────────────────────────────────────────────────────
    #  Task 1 – Create vPC Interface Policy Group (IPG)
    # ──────────────────────────────────────────────────────────────────────────
    @retry()
    def create_ipg(self, row: dict) -> bool:
        """
        POST /api/node/mo/uni/infra/funcprof/accbundle-{ipg_name}.json

        Creates a vPC bundle interface policy group and attaches:
          - AAEP
          - CDP policy   (CDP-DISABLED)
          - LLDP policy  (LLDP-ENABLED)
          - LACP policy  (LACP-ACTIVE)
          - Link-Level policy (from Excel: link_speed)
        """
        ipg_name   = str(row["ipg_name"]).strip()
        aaep_name  = str(row["aaep_name"]).strip()
        link_speed = str(row["link_speed"]).strip()

        url = (
            f"{self.base_url}/api/node/mo/uni/infra/funcprof/"
            f"accbundle-{ipg_name}.json"
        )
        payload = {
            "infraAccBndlGrp": {
                "attributes": {
                    "name":   ipg_name,
                    "lagT":   "node",              # "node" = vPC
                    "status": "created,modified",
                },
                "children": [
                    {
                        "infraRsAttEntP": {
                            "attributes": {
                                "tDn": f"uni/infra/attentp-{aaep_name}"
                            }
                        }
                    },
                    {
                        "infraRsCdpIfPol": {
                            "attributes": {
                                "tnCdpIfPolName": "CDP-DISABLED"
                            }
                        }
                    },
                    {
                        "infraRsHIfPol": {
                            "attributes": {
                                "tnFabricHIfPolName": link_speed
                            }
                        }
                    },
                    {
                        "infraRsLldpIfPol": {
                            "attributes": {
                                "tnLldpIfPolName": "LLDP-ENABLED"
                            }
                        }
                    },
                    {
                        "infraRsLacpPol": {
                            "attributes": {
                                "tnLacpLagPolName": "LACP-ACTIVE"
                            }
                        }
                    },
                ],
            }
        }

        log.info(
            f"  → create_ipg     | IPG={ipg_name} | AAEP={aaep_name} | Speed={link_speed}"
        )
        resp = self._session.post(url, json=payload, timeout=TIMEOUT)
        return self._handle_response("CREATE_IPG", url, resp, ipg_name)

    # ──────────────────────────────────────────────────────────────────────────
    #  Task 2 – Assign Interface to IPG (per leaf)
    # ──────────────────────────────────────────────────────────────────────────
    @retry()
    def assign_interface(self, row: dict, leaf: str, int_profile: str) -> bool:
        """
        POST /api/node/mo/uni/infra/accportprof-{int_profile}/
             hports-sel-{ipg_name}-typ-range.json

        Creates an interface selector under the leaf's access port profile
        and binds it to the IPG. Must be called for both leaf1 and leaf2.

        int_profile: name of the leaf interface profile as it exists in APIC
                     (from Excel columns int_profile_leaf1 / int_profile_leaf2).
                     Do NOT use system-generated profiles – APIC will reject them.
        """
        ipg_name  = str(row["ipg_name"]).strip()
        from_port = str(int(float(row["from_port"])))
        to_port   = str(int(float(row["to_port"])))
        descr     = str(row.get("port_desc") or "").strip()

        if not int_profile:
            log.error(
                f"  [FAILED]  ASSIGN_INTERFACE | Leaf={leaf} | "
                "'int_profile_leaf1'/'int_profile_leaf2' kosong di Excel."
            )
            return False

        selector_name = f"PORTSEL-{ipg_name}"
        url = (
            f"{self.base_url}/api/node/mo/uni/infra/"
            f"accportprof-{int_profile}/"
            f"hports-{selector_name}-typ-range.json"
        )
        payload = {
            "infraHPortS": {
                "attributes": {
                    "name":   selector_name,
                    "descr":  descr,
                    "status": "created,modified",
                },
                "children": [
                    {
                        "infraRsAccBaseGrp": {
                            "attributes": {
                                "tDn": f"uni/infra/funcprof/accbundle-{ipg_name}"
                            }
                        }
                    },
                    {
                        "infraPortBlk": {
                            "attributes": {
                                "name":     "block1",
                                "fromCard": "1",
                                "toCard":   "1",
                                "fromPort": from_port,
                                "toPort":   to_port,
                            }
                        }
                    },
                ],
            }
        }

        log.info(
            f"  → assign_iface   | Leaf={leaf} | Profile={int_profile} "
            f"| IPG={ipg_name} | Port 1/{from_port}–1/{to_port}"
        )
        resp = self._session.post(url, json=payload, timeout=TIMEOUT)
        return self._handle_response(
            "ASSIGN_INTERFACE", url, resp,
            f"{ipg_name}@Leaf{leaf}({int_profile})"
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  Task 3 – Push EPG Static Path Binding (vPC / protpaths)
    # ──────────────────────────────────────────────────────────────────────────
    @retry()
    def push_epg(self, row: dict) -> bool:
        """
        POST /api/node/mo/uni/tn-{tenant}/ap-{ap}/epg-{epg}.json

        Creates a static path binding using protpaths (vPC) and the IPG name.
        Path format: topology/pod-{pod}/protpaths-{leaf1}-{leaf2}/pathep-[{ipg_name}]
        """
        tenant   = str(row["tenant_name"]).strip()
        ap       = str(row["app_profile"]).strip()
        epg      = str(row["epg_name"]).strip()
        vlan     = str(int(float(row["vlan_id"])))
        pod      = str(int(float(row.get("pod_id") or 1)))
        leaf1    = str(int(float(row["leaf1"])))
        leaf2    = str(int(float(row["leaf2"])))
        ipg_name = str(row["ipg_name"]).strip()
        mode     = str(row.get("mode") or "regular").strip() or "regular"

        tdn = (
            f"topology/pod-{pod}/protpaths-{leaf1}-{leaf2}"
            f"/pathep-[{ipg_name}]"
        )
        dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}/rspathAtt-[{tdn}]"
        url = f"{self.base_url}/api/node/mo/uni/tn-{tenant}/ap-{ap}/epg-{epg}.json"

        payload = {
            "fvRsPathAtt": {
                "attributes": {
                    "dn":          dn,
                    "encap":       f"vlan-{vlan}",
                    "instrImedcy": "immediate",
                    "mode":        mode,
                    "tDn":         tdn,
                    "status":      "created,modified",
                },
                "children": [],
            }
        }

        log.info(
            f"  → push_epg       | Tenant={tenant} | AP={ap} | EPG={epg} "
            f"| VLAN={vlan} | IPG={ipg_name} | Pod={pod} | Leaves={leaf1}-{leaf2}"
        )
        resp = self._session.post(url, json=payload, timeout=TIMEOUT)
        return self._handle_response(
            "PUSH_EPG", url, resp, f"{epg}@vlan-{vlan}"
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────────────────────
    def _handle_response(
        self, task: str, url: str, resp: requests.Response, label: str
    ) -> bool:
        """
        Returns True on HTTP 200/201.
        Checks for APIC-level error objects inside imdata.
        Raises HTTPError on non-2xx so the retry decorator can act on 5xx.
        """
        if resp.status_code not in (200, 201):
            self._log_apic_error(task, url, resp)
            resp.raise_for_status()   # triggers retry logic for 5xx
            return False

        # APIC may embed error details inside a 200 response
        try:
            body = resp.json()
            for item in body.get("imdata", []):
                if "error" in item:
                    attrs = item["error"].get("attributes", {})
                    code  = attrs.get("code", "?")
                    text  = attrs.get("text", "Unknown APIC error")
                    log.error(f"  [FAILED]  {task} | {label} | APIC Error {code}: {text}")
                    return False
        except ValueError:
            pass  # response body is not JSON – treat as success

        log.info(f"  [SUCCESS] {task} | {label}")
        return True

    def _log_apic_error(self, task: str, url: str, resp: requests.Response):
        """Logs a detailed error message for a failed APIC call."""
        log.error(f"  [FAILED]  {task} | HTTP {resp.status_code}")
        log.error(f"            URL  : {url}")
        try:
            body = resp.json()
            for item in body.get("imdata", []):
                if "error" in item:
                    attrs = item["error"].get("attributes", {})
                    log.error(
                        f"            Code : {attrs.get('code','?')} | "
                        f"Text : {attrs.get('text','?')}"
                    )
                else:
                    log.error(f"            Body : {json.dumps(item)}")
        except ValueError:
            log.error(f"            Body : {resp.text[:500]}")


# ── Excel Loader ──────────────────────────────────────────────────────────────
def load_excel(filepath: str) -> list:
    """
    Reads the deployment Excel file and returns a list of row dicts.
    Column names are normalised to lowercase with underscores.
    """
    log.info(f"Loading Excel: {filepath}")
    try:
        df = pd.read_excel(filepath, dtype=str, engine="openpyxl")
    except FileNotFoundError:
        log.error(
            f"File tidak ditemukan: {filepath}\n"
            "  → Jalankan 'python create_template.py' untuk membuat template."
        )
        sys.exit(1)
    except Exception as exc:
        log.error(f"Gagal membaca Excel: {exc}")
        sys.exit(1)

    # Normalise column names: strip whitespace, lowercase, spaces→underscore
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df.dropna(how="all", inplace=True)   # drop completely empty rows

    required_cols = {
        "tenant_name", "app_profile", "epg_name", "vlan_id",
        "leaf1", "leaf2", "ipg_name", "aaep_name", "link_speed",
        "from_port", "to_port",
        "int_profile_leaf1", "int_profile_leaf2",
    }
    missing = required_cols - set(df.columns)
    if missing:
        log.error(f"Kolom wajib tidak ditemukan di Excel: {missing}")
        sys.exit(1)

    rows = df.to_dict(orient="records")
    log.info(f"Loaded {len(rows)} row(s) from Excel")
    return rows


# ── CLI Helpers ───────────────────────────────────────────────────────────────
MENU_OPTIONS = {
    1: "End-to-End Deploy  (Create IPG → Assign Interface → Push EPG)",
    2: "Create IPG saja",
    3: "Assign Interface ke IPG saja",
    4: "Push EPG ke vPC saja",
}


def show_banner():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         Cisco ACI  –  vPC Deployment Automation             ║")
    print("╚══════════════════════════════════════════════════════════════╝")


def show_menu() -> int:
    print("\nPilih mode deployment:")
    print("─" * 64)
    for key, label in MENU_OPTIONS.items():
        print(f"  {key}. {label}")
    print("─" * 64)
    while True:
        try:
            choice = int(input("Masukkan pilihan (1-4): ").strip())
            if choice in MENU_OPTIONS:
                return choice
            print("  [!] Pilihan tidak valid. Masukkan angka 1-4.")
        except ValueError:
            print("  [!] Input tidak valid. Masukkan angka 1-4.")


def get_credentials() -> tuple:
    print("\nAPIC Credentials")
    print("─" * 64)
    apic_ip  = input("  APIC IP / Hostname : ").strip()
    username = input("  Username           : ").strip()
    password = getpass.getpass("  Password           : ")
    return apic_ip, username, password


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    show_banner()

    mode                    = show_menu()
    apic_ip, username, pwd  = get_credentials()

    client = APICClient(apic_ip, username, pwd)
    if not client.login():
        log.error("Login gagal. Script dihentikan.")
        sys.exit(1)

    rows = load_excel(EXCEL_FILE)

    print()
    print("─" * 64)
    log.info(f"Mode       : {MENU_OPTIONS[mode]}")
    log.info(f"Total rows : {len(rows)}")
    log.info(f"Log file   : {_LOG_FILE}")
    print("─" * 64)

    successes = 0
    failures  = 0

    for idx, row in enumerate(rows, start=1):
        ipg_name = str(row.get("ipg_name", f"Row-{idx}")).strip()
        epg_name = str(row.get("epg_name", "")).strip()
        vlan_id  = str(row.get("vlan_id",  "")).strip()

        print()
        log.info(
            f"━━━ Row {idx}/{len(rows)}  |  IPG={ipg_name}  "
            f"|  EPG={epg_name}  |  VLAN={vlan_id} ━━━"
        )

        row_ok = True
        try:
            # ── Task 1: Create vPC IPG ────────────────────────────────────
            if mode in (1, 2):
                ok      = client.create_ipg(row)
                row_ok  = row_ok and ok

            # ── Task 2: Assign interface on both leaf nodes ───────────────
            if mode in (1, 3):
                leaf1        = str(int(float(row["leaf1"])))
                leaf2        = str(int(float(row["leaf2"])))
                int_prof_l1  = str(row.get("int_profile_leaf1") or "").strip()
                int_prof_l2  = str(row.get("int_profile_leaf2") or "").strip()
                ok1          = client.assign_interface(row, leaf1, int_prof_l1)
                ok2          = client.assign_interface(row, leaf2, int_prof_l2)
                row_ok       = row_ok and ok1 and ok2

            # ── Task 3: Push EPG static path binding ─────────────────────
            if mode in (1, 4):
                ok      = client.push_epg(row)
                row_ok  = row_ok and ok

        except Exception as exc:
            log.error(f"Unexpected error on row {idx}: {exc}")
            row_ok = False

        if row_ok:
            successes += 1
        else:
            failures += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("═" * 64)
    log.info("DEPLOYMENT SUMMARY")
    log.info(f"  Mode        : {MENU_OPTIONS[mode]}")
    log.info(f"  Total rows  : {len(rows)}")
    log.info(f"  SUCCESS     : {successes}")
    log.info(f"  FAILED      : {failures}")
    log.info(f"  Log saved   : {_LOG_FILE}")
    print("═" * 64)


if __name__ == "__main__":
    main()
