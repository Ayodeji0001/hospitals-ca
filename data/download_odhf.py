import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

ZIP_URL = "https://www150.statcan.gc.ca/n1/en/pub/13-26-0001/2020001/ODHF_v1.1.zip"
OUTPUT_FILE = Path(__file__).parent / "odhf_ontario_hospitals.csv"

ENCODINGS = ["utf-8-sig", "cp1252", "latin-1"]

COLUMN_ALIASES = {
    "facility name": "facility_name",
    "postal code": "postal_code",
    "census subdivision name": "CSDname",
    "street number": "street_no",
    "street name": "street_name",
    "province or territory": "province",
    "odhf facility type": "odhf_facility_type",
}

OUTPUT_COLUMNS = [
    "facility_name",
    "city",
    "postal_code",
    "CSDname",
    "latitude",
    "longitude",
    "street_no",
    "street_name",
    "province",
    "odhf_facility_type",
]


def fetch_ontario_hospitals() -> pd.DataFrame:
    print(f"Downloading: {ZIP_URL}")
    response = requests.get(ZIP_URL, timeout=120)
    response.raise_for_status()
    print(f"  Downloaded {len(response.content) / 1_048_576:.1f} MB")

    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = zf.namelist()
    print(f"  Archive contents: {names}")

    csv_files = [n for n in names if n.lower().endswith(".csv")]
    if not csv_files:
        raise FileNotFoundError(f"No CSV found in ZIP. Contents: {names}")

    odhf_csvs = [n for n in csv_files if "odhf" in Path(n).name.lower()]
    csv_name = max(odhf_csvs or csv_files, key=lambda n: zf.getinfo(n).file_size)

    raw_bytes = zf.read(csv_name)
    df = None
    for enc in ENCODINGS:
        try:
            df = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc, low_memory=False)
            print(
                f"  Read '{csv_name}' ({enc}) — {len(df):,} rows, {len(df.columns)} cols"
            )
            break
        except Exception as exc:
            print(f"  Encoding '{enc}' failed: {exc}")
    if df is None:
        raise ValueError(f"Could not decode '{csv_name}' with any of: {ENCODINGS}")

    # Normalise column names
    renamed = {}
    for col in df.columns:
        cleaned = str(col).replace("\ufeff", "").strip()
        renamed[col] = COLUMN_ALIASES.get(cleaned.lower().replace("_", " "), cleaned)
    df = df.rename(columns=renamed)

    required = ["facility_name", "city", "province", "odhf_facility_type"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(
            f"Required columns missing after normalisation: {missing}. Got: {df.columns.tolist()}"
        )

    print(
        f"\nUnique province values: {sorted(df['province'].dropna().unique().tolist())}"
    )
    print(
        f"Unique odhf_facility_type values: {sorted(df['odhf_facility_type'].dropna().unique().tolist())}"
    )

    ontario_hospitals = df.loc[
        df["province"].str.strip().str.lower().eq("on")
        & df["odhf_facility_type"].str.contains("hospital", case=False, na=False)
    ].copy()

    print(f"\nRows after filtering (ON + Hospital): {len(ontario_hospitals):,}")

    missing_out = [c for c in OUTPUT_COLUMNS if c not in ontario_hospitals.columns]
    if missing_out:
        raise KeyError(
            f"Output columns missing: {missing_out}. Available: {ontario_hospitals.columns.tolist()}"
        )

    return ontario_hospitals[OUTPUT_COLUMNS]


def main() -> None:
    df = fetch_ontario_hospitals()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print(f"\nSaved to: {OUTPUT_FILE}")
    print(f"\nShape      : {df.shape}")
    print(f"Columns    : {df.columns.tolist()}")
    print("\nUnique odhf_facility_type values in output:")
    print(df["odhf_facility_type"].value_counts().to_string())
    print("\nFirst 5 rows:")
    print(df.head().to_string(index=False))


if __name__ == "__main__":
    main()
