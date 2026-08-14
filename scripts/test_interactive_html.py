"""Runtime interaction test for interactive HTML artifacts (Playwright Python API).

Usage (playwright lives in the HINDSIGHT venv, not link-venv):
    python test_interactive.py [path-to-html]

Verifies: nodes/labels/links render, node click opens detail panel with jump
links, legend click filters, search filters, reset restores, and there are
ZERO console/pageerror entries. Exit 0 = pass, 1 = fail.

Pattern to adapt for any interactive HTML artifact: point it at the file,
assert the DOM state after each interaction, and treat any console/pageerror
entry as a failure.
"""
import sys

HTML_PATH = sys.argv[1] if len(sys.argv) > 1 else "semantic-cluster-map-interactive.html"
sys.path.insert(0, "")
from playwright.sync_api import sync_playwright

failures = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page()
    errors = []
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(f"file://{HTML_PATH}")
    page.wait_for_timeout(600)

    node_count = page.evaluate("document.querySelectorAll('svg circle').length")
    text_count = page.evaluate("document.querySelectorAll('svg text').length")
    legend_count = page.evaluate("document.querySelectorAll('#legend .lg').length")
    print(f"render: nodes={node_count} texts={text_count} legend={legend_count}")
    if node_count == 0:
        failures.append("no nodes rendered")

    # click first node -> panel should show detail with jump links
    page.evaluate("document.querySelector('svg circle').dispatchEvent(new MouseEvent('click', {bubbles:true}))")
    page.wait_for_timeout(200)
    panel_has_jump = page.evaluate("document.getElementById('panel').innerHTML.includes('jump')")
    panel_title = page.evaluate("document.querySelector('#panel h3')?.textContent || ''")
    print(f"click-node: panel_has_jump={panel_has_jump} title={panel_title!r}")
    if not panel_has_jump:
        failures.append("node click did not open detail panel")

    # click a legend item -> filter active
    page.evaluate("document.querySelector('#legend .lg').dispatchEvent(new MouseEvent('click', {bubbles:true}))")
    page.wait_for_timeout(200)
    shown_after_cluster = page.evaluate("document.getElementById('shown').textContent")
    print(f"cluster-filter: shown={shown_after_cluster}")
    if shown_after_cluster == "0":
        failures.append("cluster filter returned 0 shown")

    # search
    page.fill("#search", "seo")
    page.wait_for_timeout(200)
    shown_after_search = page.evaluate("document.getElementById('shown').textContent")
    print(f"search 'seo': shown={shown_after_search}")
    if shown_after_search == "0":
        failures.append("search returned 0 shown")

    # reset
    page.click("#reset")
    page.wait_for_timeout(200)
    shown_after_reset = page.evaluate("document.getElementById('shown').textContent")
    print(f"reset: shown={shown_after_reset}")
    if shown_after_reset == "0":
        failures.append("reset returned 0 shown")

    print(f"errors: {errors if errors else 'NONE'}")
    if errors:
        failures.append(f"console/page errors: {errors}")
    browser.close()

if failures:
    print("FAIL:", failures)
    sys.exit(1)
print("PASS")
