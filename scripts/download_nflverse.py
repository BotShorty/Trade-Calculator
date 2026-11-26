
#!/usr/bin/env python3
"""Download free NFLverse datasets for the Dynasty IDP Trade Calculator."""

import gzip
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "_site" / "data"
DATA.mkdir(parents=True, exist_ok=True)

CURRENT_YEAR = datetime.now().year
YEARS_BACK = 7
BASE = "https://github.com/nflverse/nflverse-data/releases/download"


def download(url, target, optional=False):
    request = Request(
        url,
        headers={"User-Agent": "Dynasty-IDP-Trade-Calculator/1.0"}
    )

    try:
        with urlopen(request, timeout=180) as response:
            with target.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Empty file: {target.name}")

        print(f"OK: {target.name}")

    except HTTPError as exc:
        target.unlink(missing_ok=True)

        if optional and exc.code in (404, 410):
            print(f"Optional file not available: {target.name}")
            return

        raise


def download_contracts():
    url = f"{BASE}/contracts/historical_contracts.csv.gz"
    target = DATA / "historical_contracts.csv"
    request = Request(
        url,
        headers={"User-Agent": "Dynasty-IDP-Trade-Calculator/1.0"}
    )

    try:
        with urlopen(request, timeout=180) as response:
            with gzip.GzipFile(fileobj=response) as source:
                with target.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)

        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("Contract file is empty")

        print(f"OK: {target.name}")

    except Exception:
        target.unlink(missing_ok=True)
        raise


download(
    f"{BASE}/players/players.csv",
    DATA / "players.csv"
)

download_contracts()

for year in range(CURRENT_YEAR, CURRENT_YEAR - YEARS_BACK - 1, -1):
    download(
        f"{BASE}/stats_player/stats_player_reg_{year}.csv",
        DATA / f"stats_player_reg_{year}.csv",
        optional=True
    )

    download(
        f"{BASE}/snap_counts/snap_counts_{year}.csv",
        DATA / f"snap_counts_{year}.csv",
        optional=True
    )
