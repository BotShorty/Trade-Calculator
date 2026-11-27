
#!/usr/bin/env python3
"""Download NFLverse data and build the Sleeper-based player ID map."""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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
# Zentrale Sleeper-Datenbank mit täglichem Cache
SLEEPER_CACHE_DIR = ROOT / "data"
SLEEPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SLEEPER_CACHE_FILE = SLEEPER_CACHE_DIR / "sleeper_players.json"
SLEEPER_UPDATE_FILE = SLEEPER_CACHE_DIR / "sleeper_last_update.json"

LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")


def load_sleeper_players():
    today = datetime.now(LOCAL_TIMEZONE).date().isoformat()

    # Prüfen, ob die Daten heute bereits erfolgreich gespeichert wurden.
    try:
        metadata = json.loads(
            SLEEPER_UPDATE_FILE.read_text(encoding="utf-8")
        )

        cached_players = json.loads(
            SLEEPER_CACHE_FILE.read_text(encoding="utf-8")
        )

        if (
            metadata.get("last_successful_date") == today
            and isinstance(cached_players, dict)
            and cached_players
        ):
            print(f"Sleeper-Daten bereits heute geladen ({today}).")
            print(f"Verwende gespeicherte {len(cached_players)} Spieler.")
            return cached_players

    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass

    # Kein gültiger Tages-Cache vorhanden: neu herunterladen.
    print(f"Sleeper-Daten müssen aktualisiert werden ({today}).")

    downloaded = request_bytes(SLEEPER_URL)
    fresh_players = json.loads(downloaded.decode("utf-8"))

    if not isinstance(fresh_players, dict) or not fresh_players:
        raise RuntimeError("Sleeper-Datenbank leer oder ungültig.")

    # Erst nach erfolgreicher Prüfung dauerhaft speichern.
    temp_data = SLEEPER_CACHE_FILE.with_suffix(".tmp")
    temp_data.write_bytes(downloaded)
    temp_data.replace(SLEEPER_CACHE_FILE)

    update_metadata = {
        "last_successful_date": today,
        "downloaded_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
        "source": SLEEPER_URL,
        "player_count": len(fresh_players)
    }

    temp_metadata = SLEEPER_UPDATE_FILE.with_suffix(".tmp")
    temp_metadata.write_text(
        json.dumps(update_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )
    temp_metadata.replace(SLEEPER_UPDATE_FILE)

    print(f"Sleeper-Daten erfolgreich gespeichert: {len(fresh_players)} Spieler.")

    return fresh_players
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
    raw_players = load_sleeper_players()
    public_sleeper_file = DATA / "sleeper_players.json"
    public_sleeper_file.write_bytes(SLEEPER_CACHE_FILE.read_bytes())
    nflverse_rows = load_nflverse_players()

    # Bisherige Datenbank laden.
    map_path = ROOT / "player_id_map.json"

    try:
        previous = json.loads(map_path.read_text(encoding="utf-8"))
        previous_players = previous.get("players", {})
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
        previous_players = {}

    # Bisherige Spielerdatensätze vereinheitlichen.
    old_records = []

    for old_key, old_record in previous_players.items():
        record = dict(old_record)
        record["internal_id"] = record.get("internal_id") or old_key
        old_records.append(record)

    # Einmalige Migration auf BS + vierstellige Hex-ID.
    migrate_ids = previous.get("id_format") != "BS0001-BSFFFF"

    if migrate_ids:
        print("Migriere bestehende Spieler-IDs auf Hexadezimalformat...")

        old_records.sort(
            key=lambda r: str(r.get("sleeper_id") or "")
        )

        if len(old_records) > 65535:
            raise RuntimeError("Mehr als 65.535 Spieler-IDs vorhanden.")

        for number, record in enumerate(old_records, start=1):
            record["internal_id"] = f"BS{number:04X}"

    # Alle jemals vergebenen BS-IDs dauerhaft reservieren.
    reserved_sleeper_ids = dict(previous.get("reserved_sleeper_ids", {}))
    reserved_internal_ids = set(previous.get("reserved_internal_ids", []))

    for record in old_records:
        sleeper_id = clean(record.get("sleeper_id"))
        internal_id = record.get("internal_id")

        if sleeper_id and internal_id:
            previous_id = reserved_sleeper_ids.get(sleeper_id)

            if previous_id and previous_id != internal_id:
                raise RuntimeError(
                    f"Widersprüchliche BS-ID für Sleeper-ID {sleeper_id}"
                )

            reserved_sleeper_ids[sleeper_id] = internal_id

        if internal_id:
            reserved_internal_ids.add(internal_id)

     # Doppelte BS-IDs in den bisherigen Spielerdatensätzen erkennen.
    old_internal_ids = [
        record.get("internal_id")
        for record in old_records
        if record.get("internal_id")
    ]

    if len(old_internal_ids) != len(set(old_internal_ids)):
        raise RuntimeError(
            "Doppelte BS-IDs in bisherigen Spielerdatensätzen."
        )

    id_owners = {}

    for sleeper_id, internal_id in reserved_sleeper_ids.items():
        if not isinstance(internal_id, str) or not (
            len(internal_id) == 6
            and internal_id.startswith("BS")
            and all(c in "0123456789ABCDEF" for c in internal_id[2:])
        ):
            raise RuntimeError(f"Ungültige reservierte BS-ID: {internal_id}")

        if internal_id in id_owners and id_owners[internal_id] != sleeper_id:
            raise RuntimeError(f"BS-ID mehrfach vergeben: {internal_id}")

        id_owners[internal_id] = sleeper_id

    used_numbers = {
        int(internal_id[2:], 16)
        for internal_id in reserved_internal_ids
    }

    next_number = max(used_numbers, default=0) + 1

    def new_internal_id():
        nonlocal next_number

        if next_number > 0xFFFF:
            raise RuntimeError("Alle verfügbaren BS-Hex-IDs sind vergeben.")

        result = f"BS{next_number:04X}"
        next_number += 1
        reserved_internal_ids.add(result)
        return result

    # Alte Datensätze anhand der Sleeper-ID wiederfinden.
    old_by_sleeper = {}

    for record in old_records:
        sleeper_id = clean(record.get("sleeper_id"))

        if sleeper_id:
            if sleeper_id in old_by_sleeper:
                raise RuntimeError(
                    f"Doppelte Sleeper-ID in bisheriger Datenbank: {sleeper_id}"
                )

            old_by_sleeper[sleeper_id] = record

    # NFLverse-Verknüpfungen vorbereiten.
    nflverse_by_gsis = defaultdict(set)
    otc_to_gsis = defaultdict(set)
    pfr_to_gsis = defaultdict(set)

    for row in nflverse_rows:
        gsis = clean(row.get("gsis_id"))
        otc_id = clean(row.get("otc_id"))
        pfr_id = clean(row.get("pfr_id"))

        if gsis:
            nflverse_by_gsis[gsis].add((otc_id, pfr_id))

            if otc_id:
                otc_to_gsis[otc_id].add(gsis)

            if pfr_id:
                pfr_to_gsis[pfr_id].add(gsis)

    # Doppelte GSIS-IDs in Sleeper erkennen.
    sleeper_gsis_counts = defaultdict(int)

    for player in raw_players.values():
        gsis = clean(player.get("gsis_id"))

        if gsis:
            sleeper_gsis_counts[gsis] += 1

        # Die zentrale Datenbank enthält ausschließlich aktuelle Sleeper-Spieler.
    # Alte Spieler-IDs bleiben lediglich reserviert.
    players = {}
    unresolved = []

    # Alle Sleeper-Spieler berücksichtigen, nicht nur bestimmte Positionen.
    for sleeper_id, player in raw_players.items():
        sleeper_id = str(sleeper_id)
        gsis = clean(player.get("gsis_id"))

        old_record = old_by_sleeper.get(sleeper_id)

        if old_record:
            internal_id = old_record["internal_id"]
            record = dict(old_record)
        elif sleeper_id in reserved_sleeper_ids:
            internal_id = reserved_sleeper_ids[sleeper_id]
            record = {}
        else:
            internal_id = new_internal_id()
            record = {}

        reserved_sleeper_ids[sleeper_id] = internal_id

        record.update({
            "internal_id": internal_id,
            "sleeper_id": sleeper_id,
            "gsis_id": gsis,
            "otc_id": None,
            "pfr_id": None,
            "name": clean(player.get("full_name")) or (
                f"{clean(player.get('first_name')) or ''} "
                f"{clean(player.get('last_name')) or ''}"
            ).strip(),
            "position": clean(player.get("position")),
            "team": clean(player.get("team")),
            "active": player.get("active"),
            "in_sleeper": True
        })

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

    # Abschließende Eindeutigkeitsprüfung.
    all_ids = [record["internal_id"] for record in players.values()]

    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("Doppelte interne BS-IDs erkannt.")

    result = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Sleeper NFL player database",
        "id_format": "BS0001-BSFFFF",
        "player_count": sum(
            1 for p in players.values() if p.get("in_sleeper")
        ),
        "registry_count": len(players),
        "reserved_sleeper_ids": reserved_sleeper_ids,
        "reserved_internal_ids": sorted(reserved_internal_ids),
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
