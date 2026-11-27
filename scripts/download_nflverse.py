
#!/usr/bin/env python3
"""Download NFLverse data and build the Sleeper-based player ID map."""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import gzip

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "_site" / "data"
DATA.mkdir(parents=True, exist_ok=True)

CURRENT_YEAR = datetime.now().year
YEARS_BACK = 7
ROSTER_YEARS_BACK = 3

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"

SUPPORTED_POSITIONS = {
    "QB", "RB", "WR", "TE", "K",
    "DL", "EDGE", "DT", "NT", "DE",
    "LB", "DB", "S", "Safety", "CB", "Cornerback"
}


def request_bytes(url):
    request = Request(
        url,
        headers={"User-Agent": "Dynasty-IDP-Trade-Calculator/1.0"}
    )
    with urlopen(request, timeout=180) as response:
        return response.read()


def download(url, target, optional=False):
    try:
        data = request_bytes(url)
        if not data:
            raise RuntimeError(f"Empty download: {target.name}")
        target.write_bytes(data)
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

    try:
        compressed = request_bytes(url)
        target.write_bytes(gzip.decompress(compressed))
        if target.stat().st_size == 0:
            raise RuntimeError("Contract file is empty")
        print(f"OK: {target.name}")
    except Exception:
        target.unlink(missing_ok=True)
        raise


def clean(value):
    value = str(value or "").strip()
    return value if value and value.lower() not in {"nan", "none", "null"} else None


def load_nflverse_players():
    path = DATA / "players.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def build_player_id_map():
    print("Downloading Sleeper NFL player database...")
    raw_players = json.loads(request_bytes(SLEEPER_URL))

    nflverse_rows = load_nflverse_players()

    # Load the previous registry so existing BS IDs are never reassigned.
    map_path = ROOT / "player_id_map.json"
    try:
        previous = json.loads(map_path.read_text(encoding="utf-8"))
        previous_players = previous.get("players", {})
    except (FileNotFoundError, json.JSONDecodeError):
        previous_players = {}

    # Normalize previous records.
    old_records = []
    for old_key, old_record in previous_players.items():
        record = dict(old_record)
        record["internal_id"] = record.get("internal_id") or old_key
        old_records.append(record)

    # Find the highest already assigned BS number.
    used_numbers = set()
    for record in old_records:
        internal_id = record.get("internal_id", "")
        if internal_id.startswith("BS") and internal_id[2:].isdigit():
            used_numbers.add(int(internal_id[2:]))

    next_number = max(used_numbers, default=0) + 1

    def new_internal_id():
        nonlocal next_number
        if next_number > 9999:
            raise RuntimeError("All BS IDs from BS0001 to BS9999 are used.")
        result = f"BS{next_number:04d}"
        next_number += 1
        return result

    # Keep existing BS IDs. Older SLP IDs are migrated to BS IDs once.
    old_records.sort(key=lambda r: str(r.get("sleeper_id") or ""))
    for record in old_records:
        internal_id = record.get("internal_id", "")
        if not (internal_id.startswith("BS") and internal_id[2:].isdigit()):
            record["internal_id"] = new_internal_id()

    old_by_sleeper = {}
    old_by_gsis = defaultdict(list)

    for record in old_records:
        sleeper_id = clean(record.get("sleeper_id"))
        gsis = clean(record.get("gsis_id"))

        if sleeper_id:
            old_by_sleeper[sleeper_id] = record
        if gsis:
            old_by_gsis[gsis].append(record)

    # Build NFLverse GSIS index.
    nflverse_by_gsis = defaultdict(set)

    for row in nflverse_rows:
        gsis = clean(row.get("gsis_id"))
        if gsis:
            nflverse_by_gsis[gsis].add((
                clean(row.get("otc_id")),
                clean(row.get("pfr_id"))
            ))

    otc_to_gsis = defaultdict(set)
    pfr_to_gsis = defaultdict(set)

    for gsis, entries in nflverse_by_gsis.items():
        for otc_id, pfr_id in entries:
            if otc_id:
                otc_to_gsis[otc_id].add(gsis)
            if pfr_id:
                pfr_to_gsis[pfr_id].add(gsis)

    # Detect duplicate GSIS IDs in Sleeper.
    sleeper_gsis_counts = defaultdict(int)

    for player in raw_players.values():
        position = clean(player.get("position"))
        gsis = clean(player.get("gsis_id"))
        if position in SUPPORTED_POSITIONS and gsis:
            sleeper_gsis_counts[gsis] += 1

    players = {}
    unresolved = []
    assigned_this_run = set()

    # Keep old records, including players no longer listed by Sleeper.
    for record in old_records:
        internal_id = record["internal_id"]
        record["in_sleeper"] = False
        players[internal_id] = record

    # Process the current Sleeper player universe.
    for sleeper_id, player in raw_players.items():
        position = clean(player.get("position"))

        if position not in SUPPORTED_POSITIONS:
            continue

        sleeper_id = str(sleeper_id)
        gsis = clean(player.get("gsis_id"))

        # First choice: exact Sleeper ID.
        previous_record = old_by_sleeper.get(sleeper_id)

        # Second choice: carry over an ID only if GSIS is unique
        # in both the old registry and current Sleeper data.
        if (
            previous_record is None
            and gsis
            and sleeper_gsis_counts[gsis] == 1
            and len(old_by_gsis.get(gsis, [])) == 1
        ):
            previous_record = old_by_gsis[gsis][0]

        if previous_record:
            internal_id = previous_record["internal_id"]
        else:
            internal_id = new_internal_id()

        if internal_id in assigned_this_run:
            unresolved.append({
                "sleeper_id": sleeper_id,
                "gsis_id": gsis,
                "reason": "Internal ID would be assigned more than once"
            })
            internal_id = new_internal_id()

        assigned_this_run.add(internal_id)

        record = {
            "internal_id": internal_id,
            "sleeper_id": sleeper_id,
            "gsis_id": gsis,
            "otc_id": None,
            "pfr_id": None,
            "name": clean(player.get("full_name")) or (
                f"{clean(player.get('first_name')) or ''} "
                f"{clean(player.get('last_name')) or ''}"
            ).strip(),
            "position": position,
            "team": clean(player.get("team")),
            "active": player.get("active"),
            "in_sleeper": True
        }

        if not gsis:
            unresolved.append({
                "internal_id": internal_id,
                "sleeper_id": sleeper_id,
                "reason": "Sleeper player has no GSIS ID"
            })

        elif sleeper_gsis_counts[gsis] != 1:
            unresolved.append({
                "internal_id": internal_id,
                "sleeper_id": sleeper_id,
                "gsis_id": gsis,
                "reason": "GSIS ID is shared by multiple Sleeper players"
            })

        else:
            nflverse_matches = nflverse_by_gsis.get(gsis, set())

            if len(nflverse_matches) == 1:
                otc_id, pfr_id = next(iter(nflverse_matches))

                if otc_id and otc_to_gsis[otc_id] == {gsis}:
                    record["otc_id"] = otc_id

                if pfr_id and pfr_to_gsis[pfr_id] == {gsis}:
                    record["pfr_id"] = pfr_id

            elif len(nflverse_matches) > 1:
                unresolved.append({
                    "internal_id": internal_id,
                    "sleeper_id": sleeper_id,
                    "gsis_id": gsis,
                    "reason": "Multiple conflicting NFLverse records for GSIS ID"
                })

        players[internal_id] = record

    result = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Sleeper NFL player database",
        "id_format": "BS0001-BS9999",
        "player_count": sum(
            1 for p in players.values() if p.get("in_sleeper")
        ),
        "registry_count": len(players),
        "unresolved_count": len(unresolved),
        "players": players,
        "unresolved": unresolved
    }

    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"

    map_path.write_text(output, encoding="utf-8")
    (ROOT / "_site" / "player_id_map.json").write_text(
        output, encoding="utf-8"
    )

    print(f"Player ID map created: {result['player_count']} Sleeper players")
    print(f"Total registered IDs: {result['registry_count']}")
    print(f"Unresolved ID links: {len(unresolved)}")

# Download source data first.
download(
    f"{BASE}/players/players.csv",
    DATA / "players.csv"
)

download_contracts()

download(
    f"{BASE}/schedules/games.csv",
    DATA / "games.csv"
)

for year in range(CURRENT_YEAR - ROSTER_YEARS_BACK, CURRENT_YEAR + 1):
    download(
        f"{BASE}/weekly_rosters/roster_weekly_{year}.csv",
        DATA / f"roster_weekly_{year}.csv",
        optional=True
    )

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

# Build the central player database after players.csv is available.
build_player_id_map()
