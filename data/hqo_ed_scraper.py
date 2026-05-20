import asyncio
import json
from pathlib import Path

import pandas as pd
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

URL = "https://www.hqontario.ca/System-Performance/Time-Spent-in-Emergency-Departments"
ENDPOINT = "GetTopHospitalsEDCombined"
OUTPUT_FILE = Path(__file__).parent / "hqo_ed_waittimes.csv"

INDICATORS = [
    ("First assessment by a doctor", "PIA"),
    ("Low-urgency patients not admitted", "L"),
    ("High-urgency patients not admitted", "NAH"),
    ("Admitted patients", "A"),
]


def browser_options() -> ChromiumOptions:
    options = ChromiumOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.browser_preferences = {
        "profile": {
            "default_content_setting_values": {
                "notifications": 2,
                "popups": 0,
                "geolocation": 2,
            }
        }
    }
    return options


async def fetch_indicator(tab, result_type: str) -> list[dict] | None:
    loop = asyncio.get_event_loop()
    response_future: asyncio.Future = loop.create_future()

    async def on_response(event: dict) -> None:
        params = event.get("params", event)
        url = params.get("response", {}).get("url", "")
        if ENDPOINT in url and f"ResultType={result_type}" in url:
            if not response_future.done():
                response_future.set_result(params)

    cb_id = await tab.on("Network.responseReceived", on_response, temporary=False)

    await tab.execute_script(f"""
    (function() {{
        var sel = document.getElementById('priorityLevel');
        if (sel) {{
            sel.value = '{result_type}';
            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
        var btn = document.getElementById('updateTopHosiptal');
        if (btn) {{ btn.click(); }}
    }})();
    """)

    try:
        params = await asyncio.wait_for(response_future, timeout=20)
    except asyncio.TimeoutError:
        print(f"    WARNING: timed out waiting for {ENDPOINT}?ResultType={result_type}")
        await tab.remove_callback(cb_id)
        return None
    finally:
        await tab.remove_callback(cb_id)

    request_id = params.get("requestId")
    if not request_id:
        print("    WARNING: no requestId in event params")
        return None

    await asyncio.sleep(0.5)

    for attempt in range(3):
        try:
            return json.loads(await tab.get_network_response_body(request_id))
        except Exception as exc:
            if attempt < 2:
                await asyncio.sleep(1)
            else:
                print(
                    f"    WARNING: could not read response body after 3 attempts: {exc}"
                )
                return None


async def scrape() -> list[dict]:
    all_rows = []

    async with Chrome(options=browser_options()) as browser:
        tab = await browser.start()
        await tab.enable_network_events()

        print("Navigating to HQO ED page...")
        await tab.go_to(URL, timeout=60)
        await asyncio.sleep(4)

        for label, result_type in INDICATORS:
            print(f"  Scraping: {label} (ResultType={result_type})")
            rows = await fetch_indicator(tab, result_type)

            if rows is None:
                print("    -> no data received, skipping")
                continue

            for row in rows:
                all_rows.append(
                    {
                        "hospital_name": row.get("Name", "").strip(),
                        "average_hours": row.get("LOS_mean"),
                        "indicator": label,
                    }
                )
            print(f"    -> {len(rows)} hospitals found")

        df = pd.DataFrame(all_rows)
        if not df.empty:
            df = df.pivot_table(
                index="hospital_name",
                columns="indicator",
                values="average_hours",
                aggfunc="first",
            ).reset_index()
            df.columns.name = None
            df.columns = [
                c.replace(" ", "_").replace("-", "_").lower() for c in df.columns
            ]

        df.to_csv(OUTPUT_FILE, index=False)
        print(f"\nSaved {len(df)} hospitals -> {OUTPUT_FILE}")
        print(df.head().to_string())

    return all_rows


async def main():
    print("Starting scraper...")
    all_rows = await scrape()
    if not all_rows:
        print(
            "No data collected. Try uncommenting --headless to inspect the page visually."
        )


if __name__ == "__main__":
    asyncio.run(main())
