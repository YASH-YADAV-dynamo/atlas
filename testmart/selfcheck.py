"""Drive TestMart with Playwright role+name locators: one refund, one address change, then verify."""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

from testmart.tasks import Task, generate_tasks

SEED, PORT = 42, 8199
BASE = f"http://127.0.0.1:{PORT}"


def wait_healthy() -> None:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("TestMart did not start")


def login(page: Page, password: str) -> None:
    page.goto(f"{BASE}/login")
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()


def search_and_open(page: Page, section: str, label: str, term: str, link: str) -> None:
    page.get_by_role("navigation", name="Main").get_by_role("link", name=section).click()
    page.get_by_label(label).fill(term)
    page.get_by_role("button", name="Search").click()
    page.get_by_role("link", name=link).first.click()


def do_refund(page: Page, task: Task) -> None:
    search_and_open(page, "Orders", "Search orders", task.params["order_id"], "View order")
    page.get_by_role("link", name="Refund order").click()
    page.get_by_role("button", name="Confirm refund").click()
    expect(page.get_by_text("Refunded", exact=True)).to_be_visible()


def do_address(page: Page, task: Task) -> None:
    search_and_open(page, "Customers", "Search customers", task.params["name"], "View customer")
    page.get_by_role("link", name="Edit address").click()
    page.get_by_label("Shipping address").fill(task.params["address"])
    page.get_by_role("button", name="Save address").click()
    expect(page.get_by_text(task.params["address"])).to_be_visible()


def main() -> int:
    tasks = generate_tasks(50, SEED)
    refund = next(t for t in tasks if t.template_id == "refund_order")
    address = next(t for t in tasks if t.template_id == "change_address")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "selfcheck.db")
        env = {**os.environ, "TESTMART_DB": db_path, "TESTMART_PORT": str(PORT),
               "TESTMART_HOST": "127.0.0.1", "TESTMART_SEED": str(SEED)}
        server = subprocess.Popen([sys.executable, "-m", "testmart"], env=env)
        try:
            wait_healthy()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                login(page, env.get("TESTMART_PASSWORD", "testmart"))
                assert not refund.check(db_path), "refund check true before acting"
                do_refund(page, refund)
                do_address(page, address)
                browser.close()
            results = {"refund": refund.check(db_path), "address": address.check(db_path)}
        finally:
            server.terminate()
            server.wait(timeout=10)
    print(results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
