"""Capture screenshots + a walkthrough video of the running SolarTwin dashboard.

Usage:
    ./.venv/bin/python scripts/capture_dashboard.py            # default port 8521
    ./.venv/bin/python scripts/capture_dashboard.py 8530       # custom port
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8521
URL = f"http://127.0.0.1:{PORT}"

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "outputs" / "screens"
VIDEO = ROOT / "outputs" / "media"
SHOTS.mkdir(parents=True, exist_ok=True)
VIDEO.mkdir(parents=True, exist_ok=True)

VIEWPORT = {"width": 1600, "width": 1600, "height": 1000}
TABS = ["Plant Health", "Inverter Deep-Dive", "€ Ledger", "Ask the Plant"]


def wait_render(page, settle: float = 3.5) -> None:
    """Wait for Streamlit to finish a rerun, then let charts paint."""
    try:
        page.wait_for_selector("text=SolarTwin", timeout=30_000)
        # Streamlit shows a "Running" status while reruns are in flight.
        page.wait_for_function(
            "() => !document.querySelector('[data-testid=\"stStatusWidget\"]')"
            " || document.querySelector('[data-testid=\"stStatusWidget\"]').innerText.trim() === ''",
            timeout=15_000,
        )
    except Exception:
        pass
    time.sleep(settle)


def nudge_lazy_charts(page) -> None:
    """Scroll the page so every Plotly chart enters the viewport and paints,
    then return to the top for a clean full-page screenshot."""
    page.evaluate(
        """async () => {
            const step = Math.floor(window.innerHeight * 0.8);
            for (let y = 0; y <= document.body.scrollHeight; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 250));
            }
            window.scrollTo(0, 0);
        }"""
    )
    time.sleep(1.2)


def click_tab(page, name: str) -> None:
    page.get_by_role("tab", name=name, exact=True).click()
    wait_render(page, settle=3.0)
    nudge_lazy_charts(page)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=2,  # retina-crisp screenshots
            record_video_dir=str(VIDEO),
            record_video_size={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        print(f"Loading {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        wait_render(page, settle=5.0)
        nudge_lazy_charts(page)

        shots = {
            "Plant Health": "01_plant_health.png",
            "Inverter Deep-Dive": "02_inverter_deepdive.png",
            "€ Ledger": "03_ledger.png",
            "Ask the Plant": "04_ask_the_plant.png",
        }

        # First tab is already visible on load.
        page.screenshot(path=str(SHOTS / shots["Plant Health"]), full_page=True)
        print("captured Plant Health")

        for tab in TABS[1:]:
            click_tab(page, tab)
            if tab == "Ask the Plant":
                # Click the first example question so the screenshot shows an answer.
                try:
                    page.get_by_role(
                        "button", name="Which inverter should we service first and why?"
                    ).first.click()
                    wait_render(page, settle=3.0)
                    nudge_lazy_charts(page)
                except Exception as exc:
                    print(f"  (example-button click skipped: {exc})")
            page.screenshot(path=str(SHOTS / shots[tab]), full_page=True)
            print(f"captured {tab}")

        # A short scripted loop back through the tabs makes the recorded video
        # show the whole product, then close to flush the .webm to disk.
        for tab in TABS:
            click_tab(page, tab)
            time.sleep(0.6)

        video = page.video
        context.close()  # flush video
        browser.close()

        raw = Path(video.path()) if video else None
        if raw and raw.exists():
            target = VIDEO / "solartwin_walkthrough.webm"
            raw.replace(target)
            print(f"video: {target}")
        print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
