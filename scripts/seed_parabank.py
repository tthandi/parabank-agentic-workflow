#!/usr/bin/env python3
"""Seed a local ParaBank instance with deterministic fixture data.

NOT part of the system under evaluation. The agent loop and replay executor
reach ParaBank only through the UI surface (src/cua/surface); this script is
allowed to use the REST API because it is test-data setup, not automation
under test. Synthetic identities only.

    python scripts/seed_parabank.py --reset
    python scripts/seed_parabank.py --verify
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ROOT / "fixtures" / "personas.yaml"
MANIFEST = ROOT / "fixtures" / "seeded.json"
JSON = {"Accept": "application/json"}  # ParaBank returns XML without this


class ParaBank:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.svc = f"{self.base}/services/bank"
        self.http = requests.Session()

    # --- db lifecycle -----------------------------------------------------
    def reset(self) -> None:
        self.http.post(f"{self.svc}/cleanDB", headers=JSON, timeout=30).raise_for_status()
        self.http.post(f"{self.svc}/initializeDB", headers=JSON, timeout=30).raise_for_status()

    def set_parameter(self, name: str, value: str) -> None:
        self.http.post(f"{self.svc}/setParameter/{name}/{value}", headers=JSON, timeout=15)

    # --- registration is UI-only: form POST, no CSRF token -----------------
    def register(self, p: dict, defaults: dict) -> bool:
        addr = defaults["address"]
        form = {
            "customer.firstName": p["firstName"],
            "customer.lastName": p["lastName"],
            "customer.address.street": addr["street"],
            "customer.address.city": addr["city"],
            "customer.address.state": addr["state"],
            "customer.address.zipCode": addr["zipCode"],
            "customer.phoneNumber": p["phoneNumber"],
            "customer.ssn": p["ssn"],
            "customer.username": p["username"],
            "customer.password": defaults["password"],
            "repeatedPassword": defaults["password"],
        }
        # register.htm's controller uses Spring @SessionAttributes and expects
        # a 'customerForm' attribute already in session — a bare POST 500s with
        # "Expected session attribute 'customerForm'". A GET first (as a
        # browser landing on the page would do) seeds that session state.
        self.http.get(f"{self.base}/register.htm", timeout=15).raise_for_status()
        r = self.http.post(f"{self.base}/register.htm", data=form, timeout=30)
        r.raise_for_status()
        # ParaBank returns 200 for both success and duplicate-username.
        # Read the body, not the status code.
        if "This username already exists" in r.text:
            return False
        if "Your account was created successfully" not in r.text:
            raise RuntimeError(f"register failed for {p['username']}: unexpected body")
        return True

    def login(self, username: str, password: str) -> dict | None:
        r = self.http.get(f"{self.svc}/login/{username}/{password}", headers=JSON, timeout=15)
        if r.status_code != 200 or not r.text.strip():
            return None
        return r.json()

    def accounts(self, customer_id: int) -> list[dict]:
        r = self.http.get(
            f"{self.svc}/customers/{customer_id}/accounts", headers=JSON, timeout=15
        )
        r.raise_for_status()
        return r.json()

    def create_account(self, customer_id: int, kind: int, from_account_id: int) -> dict:
        r = self.http.post(
            f"{self.svc}/createAccount",
            headers=JSON,
            timeout=20,
            params={
                "customerId": customer_id,
                "newAccountType": kind,  # 0 CHECKING, 1 SAVINGS
                "fromAccountId": from_account_id,
            },
        )
        r.raise_for_status()
        return r.json()

    def deposit(self, account_id: int, amount: float) -> None:
        self.http.post(
            f"{self.svc}/deposit",
            headers=JSON,
            timeout=20,
            params={"accountId": account_id, "amount": amount},
        ).raise_for_status()

    def withdraw(self, account_id: int, amount: float) -> None:
        self.http.post(
            f"{self.svc}/withdraw",
            headers=JSON,
            timeout=20,
            params={"accountId": account_id, "amount": amount},
        ).raise_for_status()

    def transactions(self, account_id: int) -> list[dict]:
        r = self.http.get(
            f"{self.svc}/accounts/{account_id}/transactions", headers=JSON, timeout=15
        )
        r.raise_for_status()
        return r.json()


def seed_persona(pb: ParaBank, p: dict, defaults: dict) -> dict:
    pw = defaults["password"]

    if not p.get("register", True):
        return {
            "username": p["username"],
            "registered": False,
            "note": "intentionally absent - drives customer_not_found",
            "expect": p.get("expect", {}),
        }

    created = pb.register(p, defaults)
    customer = pb.login(p["username"], pw)
    if customer is None:
        raise RuntimeError(f"{p['username']} not resolvable after register")
    cid = customer["id"]

    checking = pb.accounts(cid)[0]["id"]

    savings = None
    if p.get("savings"):
        # createAccount funds the new account OUT OF fromAccountId,
        # which is why the deposit above has to come first.
        savings = pb.create_account(cid, kind=1, from_account_id=checking)["id"]

    for amount, kind in p.get("transactions", []):
        (pb.deposit if kind == "deposit" else pb.withdraw)(checking, amount)

    if p.get("drain_savings_below_minimum") and savings is not None:
        balance = next(a for a in pb.accounts(cid) if a["id"] == savings)["balance"]
        pb.withdraw(savings, round(balance - 1.00, 2))

    return {
        "username": p["username"],
        "registered": created,
        "customer_id": cid,
        "checking_account_id": checking,
        "savings_account_id": savings,
        "transaction_count": len(pb.transactions(checking)),
        "expect": p.get("expect", {}),
    }


def verify(pb: ParaBank, manifest: dict) -> int:
    ok = True
    for row in manifest["personas"]:
        if not row.get("registered") and row.get("customer_id") is None:
            continue
        txns = pb.transactions(row["checking_account_id"])
        over = sum(1 for t in txns if float(t["amount"]) > 100)
        want = row["expect"].get("over_100")
        status = "ok  " if over == want else "FAIL"
        ok = ok and (over == want)
        print(f"{status} {row['username']:12} over_100={over} (want {want})")
    return 0 if ok else 1


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="cleanDB + initializeDB before seeding")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="check the live instance against fixtures/seeded.json and exit",
    )
    args = ap.parse_args()

    base = os.environ.get("PARABANK_BASE_URL", "http://localhost:8080/parabank")
    if "parabank.parasoft.com" in base:
        sys.exit("refusing to seed the public ParaBank instance - set PARABANK_BASE_URL")

    pb = ParaBank(base)
    spec = yaml.safe_load(PERSONAS.read_text())

    if args.verify:
        return verify(pb, json.loads(MANIFEST.read_text()))

    if args.reset:
        pb.reset()
    pb.set_parameter("INITIAL_BALANCE", "500.00")
    pb.set_parameter("MIN_BALANCE", "100.00")

    manifest = {
        "base_url": base,
        "personas": [seed_persona(pb, p, spec["defaults"]) for p in spec["personas"]],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
