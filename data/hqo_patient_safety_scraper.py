import asyncio
import json
from pathlib import Path

import pandas as pd
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

BASE_URL = "https://www.hqontario.ca/System-Performance/Hospital-Patient-Safety/"
ENDPOINT_HOSPITALS = "GetTopHospitalsPS"
ENDPOINT_TIMESERIES = "GetPatientSafetyData"

OUTPUT_DIR = Path(__file__).parent
OUTPUT_HOSPITALS = OUTPUT_DIR / "hqo_patient_safety_hospitals.csv"
OUTPUT_TIMESERIES = OUTPUT_DIR / "hqo_patient_safety_timeseries.csv"

INDICATORS = [
    (
        1,
        "rate",
        "Antibiotic-Resistant Bloodstream Infections",
        "Antibiotic-Resistant-Bloodstream-Infections",
    ),
    (
        2,
        "rate",
        "C. difficile Infections",
        "C-difficile-Infections-in-Hospital-Patients",
    ),
    (
        3,
        "percentbefore",
        "Hand Hygiene (Before Contact)",
        "Hand-Washing-in-Ontario-Hospitals-by-Hospital-Care-Providers",
    ),
    (5, "percent", "Surgical Safety Checklist", "Surgical-Safety-Checklist"),
]

HOSPITAL_COLUMNS = {
    1: "antibiotic_resistant_rate",
    2: "cdiff_rate",
    3: "hand_hygiene_pct_before",
    5: "surgical_safety_pct",
}


def browser_options() -> ChromiumOptions:
    options = ChromiumOptions()
    # options.add_argument("--headless=new")
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


async def read_body(tab, request_id: str, label: str) -> list | None:
    await asyncio.sleep(0.5)
    for attempt in range(3):
        try:
            parsed = json.loads(await tab.get_network_response_body(request_id))
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception as exc:
            if attempt < 2:
                print(f"    [retry {attempt + 1}] body read failed for {label}: {exc}")
                await asyncio.sleep(1)
            else:
                print(
                    f"    WARNING: could not read response body after 3 attempts for {label}: {exc}"
                )
                return None


async def fetch_page_data(
    tab,
    indicator_num: int,
    field_name: str,
    label: str,
    page_slug: str,
) -> tuple[list | None, list | None]:
    loop = asyncio.get_event_loop()
    hospitals_future: asyncio.Future = loop.create_future()
    timeseries_future: asyncio.Future = loop.create_future()
    ind_str = str(indicator_num)

    async def on_response(event: dict) -> None:
        params = event.get("params", event)
        url = params.get("response", {}).get("url", "")

        if (
            ENDPOINT_HOSPITALS in url
            and f"indicator={ind_str}" in url
            and not hospitals_future.done()
        ):
            hospitals_future.set_result(params)
            return

        if (
            ENDPOINT_TIMESERIES in url
            and f"indicator={ind_str}" in url
            and "showOnlyLatestPeriod=false" in url
            and not timeseries_future.done()
        ):
            timeseries_future.set_result(params)

    cb_id = await tab.on("Network.responseReceived", on_response, temporary=False)

    page_url = BASE_URL + page_slug
    print(f"  Navigating to: {page_url}")
    await tab.go_to(page_url, timeout=60)
    await asyncio.sleep(4)

    hospitals_data = None
    try:
        h_params = await asyncio.wait_for(hospitals_future, timeout=25)
        request_id = h_params.get("requestId")
        if request_id:
            hospitals_data = await read_body(
                tab, request_id, f"{ENDPOINT_HOSPITALS}?indicator={ind_str}"
            )
            if hospitals_data:
                print(f"    [{label}] GetTopHospitalsPS -> {len(hospitals_data)} rows")
                print(f"    Sample keys: {list(hospitals_data[0].keys())}")
                print(f"    First row: {hospitals_data[0]}")
        else:
            print("    WARNING: no requestId in hospitals event params")
    except asyncio.TimeoutError:
        print(
            f"    WARNING: timed out waiting for {ENDPOINT_HOSPITALS}?indicator={ind_str}"
        )

    timeseries_data = None
    try:
        ts_params = await asyncio.wait_for(timeseries_future, timeout=25)
        request_id = ts_params.get("requestId")
        if request_id:
            timeseries_data = await read_body(
                tab,
                request_id,
                f"{ENDPOINT_TIMESERIES}?indicator={ind_str}&showOnlyLatestPeriod=false",
            )
            if timeseries_data:
                print(
                    f"    [{label}] GetPatientSafetyData (full series) -> {len(timeseries_data)} rows"
                )
                print(f"    Sample keys: {list(timeseries_data[0].keys())}")
                print(f"    First row: {timeseries_data[0]}")
        else:
            print("    WARNING: no requestId in timeseries event params")
    except asyncio.TimeoutError:
        print(
            f"    WARNING: timed out waiting for {ENDPOINT_TIMESERIES}?indicator={ind_str}&showOnlyLatestPeriod=false"
        )

    await tab.remove_callback(cb_id)
    return hospitals_data, timeseries_data


def build_hospitals_df(hospitals_by_indicator: dict[int, list[dict]]) -> pd.DataFrame:
    if not hospitals_by_indicator:
        return pd.DataFrame()

    frames = []
    for ind_num, rows in hospitals_by_indicator.items():
        if not rows:
            continue
        col_name = HOSPITAL_COLUMNS[ind_num]
        df = pd.DataFrame(
            [
                {
                    "hospital_name": r.get("Name", "").strip(),
                    col_name: pd.to_numeric(r.get("value"), errors="coerce"),
                }
                for r in rows
            ]
        )
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    result = frames[0]
    for df in frames[1:]:
        result = result.merge(df, on="hospital_name", how="outer")

    for col in HOSPITAL_COLUMNS.values():
        if col not in result.columns:
            result[col] = float("nan")

    ordered_cols = ["hospital_name"] + list(HOSPITAL_COLUMNS.values())
    result = result[[c for c in ordered_cols if c in result.columns]]
    return result.sort_values("hospital_name").reset_index(drop=True)


def build_timeseries_df(
    timeseries_by_indicator: dict[int, list[dict]],
    label_map: dict[int, str],
) -> pd.DataFrame:
    if not timeseries_by_indicator:
        return pd.DataFrame()

    frames = []
    for ind_num, rows in timeseries_by_indicator.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df.insert(0, "indicator", label_map[ind_num])
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True, sort=False)


async def scrape() -> None:
    hospitals_by_indicator: dict[int, list[dict]] = {}
    timeseries_by_indicator: dict[int, list[dict]] = {}
    label_map = {ind[0]: ind[2] for ind in INDICATORS}

    async with Chrome(options=browser_options()) as browser:
        tab = await browser.start()
        await tab.enable_network_events()

        for ind_num, field_name, label, page_slug in INDICATORS:
            print(f"\n--- Indicator {ind_num}: {label} ---")
            h_data, ts_data = await fetch_page_data(
                tab, ind_num, field_name, label, page_slug
            )

            if h_data is not None:
                hospitals_by_indicator[ind_num] = h_data
            else:
                print(f"  -> no hospitals data for indicator {ind_num}")

            if ts_data is not None:
                timeseries_by_indicator[ind_num] = ts_data
            else:
                print(f"  -> no time-series data for indicator {ind_num}")

        print("\n--- Building output files ---")

        df_hosp = build_hospitals_df(hospitals_by_indicator)
        if df_hosp.empty:
            print("WARNING: hospitals DataFrame is empty — check scraper logs above")
        else:
            df_hosp.to_csv(OUTPUT_HOSPITALS, index=False)
            print(f"Saved {len(df_hosp)} hospitals -> {OUTPUT_HOSPITALS}")
            print(df_hosp.head(5).to_string())

        df_ts = build_timeseries_df(timeseries_by_indicator, label_map)
        if df_ts.empty:
            print("WARNING: time-series DataFrame is empty — check scraper logs above")
        else:
            df_ts.to_csv(OUTPUT_TIMESERIES, index=False)
            print(f"Saved {len(df_ts)} rows -> {OUTPUT_TIMESERIES}")
            print(df_ts.head(10).to_string())


async def main() -> None:
    print("Starting HQO Patient Safety scraper...")
    print(f"  Indicators : {[ind[2] for ind in INDICATORS]}")
    print(f"  Output dir : {OUTPUT_DIR}\n")
    try:
        await scrape()
    except PermissionError as exc:
        if "BrowserMetrics" not in str(exc):
            raise
        print(f"WARNING: ignored Chromium temp-file cleanup error: {exc}")
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
