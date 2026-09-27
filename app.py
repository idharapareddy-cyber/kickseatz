import os
import json
import sqlite3
import re
import csv
import io
import statistics
import math
import zlib
from urllib.parse import urlencode
from datetime import datetime
import requests
import streamlit as st

# Streamlit page configuration must happen before other Streamlit UI calls.
TICKETMASTER_API_KEY = st.secrets.get("TICKETMASTER_API_KEY", "")


TOP_PICKS_API_KEY = st.secrets.get(
    "TICKETMASTER_TOP_PICKS_API_KEY",
    TICKETMASTER_API_KEY,
)

TOP_PICKS_ENABLED = str(
    st.secrets.get(
        "TICKETMASTER_TOP_PICKS_ENABLED",
        "false",
    )
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _add_api_key_to_image_url(url, api_key):
    """Return only a public image URL; never expose an API key in the browser."""
    if not url:
        return None
    return str(url)


@st.cache_data(ttl=60)
def load_top_picks(
    event_id,
    quantity=2,
    max_price=None,
    sections=None,
):
    """
    Optional Ticketmaster Top Picks adapter.

    It stays completely dormant until the app owner enables
    TICKETMASTER_TOP_PICKS_ENABLED in Streamlit secrets. This keeps
    the MVP safe to run while partner access is still pending.
    """
    if not TOP_PICKS_ENABLED:
        return [], "Top Picks access is not enabled."

    if not TOP_PICKS_API_KEY:
        return [], "No Top Picks API key is configured."

    event_id = str(event_id or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9]{16}", event_id):
        return [], "The matchup does not have a valid 16-character Ticketmaster event ID."

    params = {
        "apikey": TOP_PICKS_API_KEY,
        "quantity": max(1, int(quantity)),
        "limit": 30,
        "sort": "quality",
        "selection": "Standard",
    }

    if max_price is not None:
        params["prices"] = f"0,{float(max_price):.2f}"

    if sections:
        params["sections"] = ",".join(
            str(section).strip()
            for section in sections
            if str(section).strip()
        )

    try:
        response = requests.get(
            f"https://app.ticketmaster.com/top-picks/v1/events/{event_id}",
            params=params,
            timeout=10,
        )

        if response.status_code == 403:
            return [], "Ticketmaster Top Picks access is not enabled for this API key."

        if response.status_code == 401:
            return [], "Ticketmaster rejected the Top Picks API key."

        if response.status_code == 429:
            return [], "Ticketmaster Top Picks rate limit was reached. Try again later."

        response.raise_for_status()

        payload = response.json()

        offer_lookup = {
            str(offer.get("offerId")): offer
            for offer in payload.get("_embedded", {}).get("offer", [])
            if isinstance(offer, dict) and offer.get("offerId")
        }

        normalized_picks = []

        for pick in payload.get("picks", []):
            if not isinstance(pick, dict):
                continue

            offer = None

            for offer_id in pick.get("offers", []) or []:
                if str(offer_id) in offer_lookup:
                    offer = offer_lookup[str(offer_id)]
                    break

            total_price = None
            face_value = None
            currency = None
            offer_name = None

            if offer:
                total_price = offer.get("totalPrice")
                face_value = offer.get("faceValue")
                currency = offer.get("currency")
                offer_name = offer.get("name")

            normalized_picks.append(
                {
                    "section": pick.get("section"),
                    "row": pick.get("row"),
                    "seats": pick.get("seats", []),
                    "quality": round(
                        float(pick.get("quality", 0) or 0) * 100
                    ),
                    "selection": pick.get("selection"),
                    "type": pick.get("type"),
                    "area": (
                        pick.get("area", {}).get("name")
                        if isinstance(pick.get("area"), dict)
                        else None
                    ),
                    "area_description": (
                        pick.get("area", {}).get("description")
                        if isinstance(pick.get("area"), dict)
                        else None
                    ),
                    "description": " • ".join(
                        str(value)
                        for value in pick.get("descriptions", []) or []
                        if value
                    ),
                    "listing_details": pick.get("listingDetails")
                    or pick.get("listing_details"),
                    "total_price": total_price,
                    "face_value": face_value,
                    "currency": currency,
                    "offer_name": offer_name,
                    "snapshot_url": _add_api_key_to_image_url(
                        pick.get("snapshotImageUrl"),
                        TOP_PICKS_API_KEY,
                    ),
                    "vfs_url": _add_api_key_to_image_url(
                        pick.get("largeVFSImageUrl"),
                        TOP_PICKS_API_KEY,
                    ),
                }
            )

        normalized_picks.sort(
            key=lambda item: (
                item.get("quality", 0),
                -float(item.get("total_price") or 999999),
            ),
            reverse=True,
        )

        return normalized_picks, None

    except (requests.RequestException, ValueError, TypeError) as exc:
        return [], f"Top Picks request failed: {exc}"



@st.cache_data(ttl=600)
def load_ticketmaster_events_for_team(team_name):
    """Load Ticketmaster Discovery event metadata for one NFL team.

    Discovery is the temporary live-event layer. It provides event metadata
    and purchase links, but it is intentionally NOT treated as seat-level
    inventory.
    """
    if not TICKETMASTER_API_KEY:
        return []

    team_name = _nfl_full_team_name(team_name)
    if team_name not in NFL_TEAM_NAMES:
        return []

    try:
        response = requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={
                "apikey": TICKETMASTER_API_KEY,
                "keyword": team_name,
                "source": "ticketmaster",
                "size": 100,
                "sort": "eventDate,date.asc",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("_embedded", {}).get("events", [])
    except (requests.RequestException, ValueError, TypeError):
        return []


def find_ticketmaster_event_for_game(game):
    """Best-effort match between an NFL schedule row and Discovery metadata."""
    if not isinstance(game, dict) or not TICKETMASTER_API_KEY:
        return None

    team = _nfl_full_team_name(game.get("team"))
    opponent = _nfl_full_team_name(game.get("opponent"))
    game_date = str(game.get("game_date") or "").strip()[:10]
    events = load_ticketmaster_events_for_team(team)

    team_tokens = {
        team.lower(),
        str(team).split()[-1].lower(),
    }
    opponent_tokens = {
        opponent.lower(),
        str(opponent).split()[-1].lower(),
    }

    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("name") or "").lower()
        start_data = event.get("dates", {}).get("start", {}) or {}
        event_date = str(start_data.get("localDate") or "")[:10]
        if game_date and event_date and event_date != game_date:
            continue
        if not any(token in name for token in team_tokens):
            continue
        if opponent and not any(token in name for token in opponent_tokens):
            continue
        return event

    return None


@st.cache_data(ttl=600)
def enrich_game_with_ticketmaster(game):
    """Attach live Discovery metadata without replacing synthetic ticket rows."""
    if not isinstance(game, dict):
        return game
    copy = dict(game)
    event = find_ticketmaster_event_for_game(copy)
    if not event:
        copy["ticketmaster_available"] = False
        return copy

    copy["ticketmaster_event_id"] = event.get("id")
    copy["ticketmaster_url"] = event.get("url")
    copy["ticketmaster_available"] = True
    status = ((event.get("dates") or {}).get("status") or {}).get("code")
    if status:
        copy["ticketmaster_status"] = status
    price_ranges = event.get("priceRanges") or []
    if price_ranges:
        first = price_ranges[0]
        copy["ticketmaster_min_price"] = first.get("min")
        copy["ticketmaster_max_price"] = first.get("max")
        copy["ticketmaster_currency"] = first.get("currency")
    return copy


# Backward-compatible alias used by a few legacy functions.
def load_ticketmaster_events():
    """Deprecated compatibility wrapper; active UI uses team-scoped Discovery."""
    if not TICKETMASTER_API_KEY:
        return []
    events = []
    seen = set()
    for team in NFL_TEAM_NAMES:
        for event in load_ticketmaster_events_for_team(team):
            event_id = str(event.get("id") or "")
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            events.append(event)
    return events


def enrich_games_with_ticketmaster(
    games,
    ticketmaster_events,
):

    if not isinstance(games, list):
        return games

    enriched_games = []

    for game in games:

        if not isinstance(game, dict):
            enriched_games.append(game)
            continue

        game_copy = dict(game)

        opponent = str(
            game.get(
                "opponent",
                "",
            )
        ).lower()

        game_date = str(
            game.get(
                "game_date",
                "",
            )
        )[:10]

        matching_event = None

        for event in ticketmaster_events:

            event_name = str(
                event.get(
                    "name",
                    "",
                )
            ).lower()

            event_dates = (
                event.get(
                    "dates",
                    {}
                )
                .get(
                    "start",
                    {}
                )
            )

            event_date = str(
                event_dates.get(
                    "localDate",
                    "",
                )
            )

            if game_date and event_date != game_date:
                continue

            if opponent and opponent not in event_name:
                continue

            # Match the current team/opponent rather than a single NFL club.
            team_name = str(game.get("team") or "").lower()
            opponent_name = str(game.get("opponent") or "").lower()
            if team_name and team_name.split()[-1] not in event_name and team_name not in event_name:
                continue
            if opponent and not any(token in event_name for token in {opponent_name, opponent_name.split()[-1]}):
                continue

            matching_event = event
            break

        if matching_event:

            game_copy["ticketmaster_event_id"] = (
                matching_event.get("id")
            )

            game_copy["ticketmaster_url"] = (
                matching_event.get("url")
            )

            game_copy["ticketmaster_status"] = (
                matching_event.get(
                    "dates",
                    {}
                )
                .get(
                    "status",
                    {}
                )
                .get(
                    "code"
                )
            )

            price_ranges = matching_event.get(
                "priceRanges",
                [],
            )

            if price_ranges:

                first_range = price_ranges[0]

                game_copy[
                    "ticketmaster_min_price"
                ] = first_range.get("min")

                game_copy[
                    "ticketmaster_max_price"
                ] = first_range.get("max")

                game_copy[
                    "ticketmaster_currency"
                ] = first_range.get("currency")

        enriched_games.append(game_copy)

    return enriched_games

# ============================================================
# KICKSEATZ — SMART SPORTS TICKET FINDER
# ============================================================

st.set_page_config(
    page_title="KickSeatz",
    page_icon="🏟️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# PATHS
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def find_file(filename, possible_paths):
    checked_paths = []

    for path in possible_paths:
        absolute_path = os.path.abspath(path)
        checked_paths.append(absolute_path)

        if os.path.isfile(absolute_path):
            return absolute_path

    raise FileNotFoundError(
        f"Could not find {filename}.\n\n"
        f"Checked these locations:\n"
        + "\n".join(f"- {path}" for path in checked_paths)
    )


DB_PATH = find_file(
    "tickets.db",
    [
        os.path.join(BASE_DIR, "tickets.db"),
        os.path.join(BASE_DIR, "falcons-ticket-analytics", "tickets.db"),
        os.path.join(os.path.dirname(BASE_DIR), "tickets.db"),
        os.path.join(
            os.path.dirname(BASE_DIR),
            "falcons-ticket-analytics",
            "tickets.db",
        ),
    ],
)


MASTER_DATA_PATH = find_file(
    "falcons_master_dataset.json",
    [
        os.path.join(BASE_DIR, "data", "falcons_master_dataset.json"),
        os.path.join(
            BASE_DIR,
            "falcons-ticket-analytics",
            "data",
            "falcons_master_dataset.json",
        ),
        os.path.join(
            os.path.dirname(BASE_DIR),
            "data",
            "falcons_master_dataset.json",
        ),
        os.path.join(
            os.path.dirname(BASE_DIR),
            "falcons-ticket-analytics",
            "data",
            "falcons_master_dataset.json",
        ),
    ],
)

# ============================================================
# NFL-WIDE DEMO INVENTORY ARCHITECTURE
# ============================================================
# Ticketmaster Discovery supplies event metadata when configured. The seat
# listings below are intentionally synthetic demo inventory for the MVP.
# They are generated for every scheduled NFL matchup and every team-facing
# browsing perspective. This keeps the recommendation engine independent of
# any one club or stadium and makes the future switch to authorized live seat
# inventory straightforward.

# NFL-WIDE VENUE REGISTRY
# ============================================================
#
# KickSeatz is being structured as an NFL-wide platform. The registry
# covers all 32 clubs and their current primary home venue, including
# shared stadiums. It is intentionally separate from seat-level
# Venue metadata is kept separate from seat-level inventory. Synthetic
# inventory is generated across the league, while venue-specific maps can
# be upgraded later when authoritative seating data is available.
#
# Venue names are descriptive metadata; this block does not claim that
# any section/row is currently for sale.

NFL_TEAM_VENUES = {
    "Arizona Cardinals": {
        "venue": "State Farm Stadium",
        "location": "Glendale, AZ",
        "venue_key": "state_farm_stadium",
        "map_status": "venue_registered",
    },
    "Atlanta Falcons": {
        "venue": "Mercedes-Benz Stadium",
        "location": "Atlanta, GA",
        "venue_key": "mercedes_benz_stadium",
        "map_status": "interactive_demo",
    },
    "Baltimore Ravens": {
        "venue": "M&T Bank Stadium",
        "location": "Baltimore, MD",
        "venue_key": "mt_bank_stadium",
        "map_status": "venue_registered",
    },
    "Buffalo Bills": {
        "venue": "Highmark Stadium",
        "location": "Orchard Park, NY",
        "venue_key": "highmark_stadium",
        "map_status": "venue_registered",
    },
    "Carolina Panthers": {
        "venue": "Bank of America Stadium",
        "location": "Charlotte, NC",
        "venue_key": "bank_of_america_stadium",
        "map_status": "venue_registered",
    },
    "Chicago Bears": {
        "venue": "Soldier Field",
        "location": "Chicago, IL",
        "venue_key": "soldier_field",
        "map_status": "venue_registered",
    },
    "Cincinnati Bengals": {
        "venue": "Paycor Stadium",
        "location": "Cincinnati, OH",
        "venue_key": "paycor_stadium",
        "map_status": "venue_registered",
    },
    "Cleveland Browns": {
        "venue": "Huntington Bank Field",
        "location": "Cleveland, OH",
        "venue_key": "huntington_bank_field",
        "map_status": "venue_registered",
    },
    "Dallas Cowboys": {
        "venue": "AT&T Stadium",
        "location": "Arlington, TX",
        "venue_key": "att_stadium",
        "map_status": "venue_registered",
    },
    "Denver Broncos": {
        "venue": "Empower Field at Mile High",
        "location": "Denver, CO",
        "venue_key": "empower_field",
        "map_status": "venue_registered",
    },
    "Detroit Lions": {
        "venue": "Ford Field",
        "location": "Detroit, MI",
        "venue_key": "ford_field",
        "map_status": "venue_registered",
    },
    "Green Bay Packers": {
        "venue": "Lambeau Field",
        "location": "Green Bay, WI",
        "venue_key": "lambeau_field",
        "map_status": "venue_registered",
    },
    "Houston Texans": {
        "venue": "NRG Stadium",
        "location": "Houston, TX",
        "venue_key": "nrg_stadium",
        "map_status": "venue_registered",
    },
    "Indianapolis Colts": {
        "venue": "Lucas Oil Stadium",
        "location": "Indianapolis, IN",
        "venue_key": "lucas_oil_stadium",
        "map_status": "venue_registered",
    },
    "Jacksonville Jaguars": {
        "venue": "EverBank Stadium",
        "location": "Jacksonville, FL",
        "venue_key": "everbank_stadium",
        "map_status": "venue_registered",
    },
    "Kansas City Chiefs": {
        "venue": "GEHA Field at Arrowhead Stadium",
        "location": "Kansas City, MO",
        "venue_key": "arrowhead_stadium",
        "map_status": "venue_registered",
    },
    "Las Vegas Raiders": {
        "venue": "Allegiant Stadium",
        "location": "Las Vegas, NV",
        "venue_key": "allegiant_stadium",
        "map_status": "venue_registered",
    },
    "Los Angeles Chargers": {
        "venue": "SoFi Stadium",
        "location": "Inglewood, CA",
        "venue_key": "sofi_stadium",
        "shared_with": "Los Angeles Rams",
        "map_status": "venue_registered",
    },
    "Los Angeles Rams": {
        "venue": "SoFi Stadium",
        "location": "Inglewood, CA",
        "venue_key": "sofi_stadium",
        "shared_with": "Los Angeles Chargers",
        "map_status": "venue_registered",
    },
    "Miami Dolphins": {
        "venue": "Hard Rock Stadium",
        "location": "Miami Gardens, FL",
        "venue_key": "hard_rock_stadium",
        "map_status": "venue_registered",
    },
    "Minnesota Vikings": {
        "venue": "U.S. Bank Stadium",
        "location": "Minneapolis, MN",
        "venue_key": "us_bank_stadium",
        "map_status": "venue_registered",
    },
    "New England Patriots": {
        "venue": "Gillette Stadium",
        "location": "Foxborough, MA",
        "venue_key": "gillette_stadium",
        "map_status": "venue_registered",
    },
    "New Orleans Saints": {
        "venue": "Caesars Superdome",
        "location": "New Orleans, LA",
        "venue_key": "caesars_superdome",
        "map_status": "venue_registered",
    },
    "New York Giants": {
        "venue": "MetLife Stadium",
        "location": "East Rutherford, NJ",
        "venue_key": "metlife_stadium",
        "shared_with": "New York Jets",
        "map_status": "venue_registered",
    },
    "New York Jets": {
        "venue": "MetLife Stadium",
        "location": "East Rutherford, NJ",
        "venue_key": "metlife_stadium",
        "shared_with": "New York Giants",
        "map_status": "venue_registered",
    },
    "Philadelphia Eagles": {
        "venue": "Lincoln Financial Field",
        "location": "Philadelphia, PA",
        "venue_key": "lincoln_financial_field",
        "map_status": "venue_registered",
    },
    "Pittsburgh Steelers": {
        "venue": "Acrisure Stadium",
        "location": "Pittsburgh, PA",
        "venue_key": "acrisure_stadium",
        "map_status": "venue_registered",
    },
    "San Francisco 49ers": {
        "venue": "Levi's Stadium",
        "location": "Santa Clara, CA",
        "venue_key": "levis_stadium",
        "map_status": "venue_registered",
    },
    "Seattle Seahawks": {
        "venue": "Lumen Field",
        "location": "Seattle, WA",
        "venue_key": "lumen_field",
        "map_status": "venue_registered",
    },
    "Tampa Bay Buccaneers": {
        "venue": "Raymond James Stadium",
        "location": "Tampa, FL",
        "venue_key": "raymond_james_stadium",
        "map_status": "venue_registered",
    },
    "Tennessee Titans": {
        "venue": "Nissan Stadium",
        "location": "Nashville, TN",
        "venue_key": "nissan_stadium",
        "map_status": "venue_registered",
    },
    "Washington Commanders": {
        "venue": "Northwest Stadium",
        "location": "Landover, MD",
        "venue_key": "northwest_stadium",
        "map_status": "venue_registered",
    },
}

NFL_UNIQUE_VENUES = sorted({
    data["venue"]
    for data in NFL_TEAM_VENUES.values()
})

# 2026 international host venues used by the NFL schedule. These are
# registered so international/neutral games can use the same venue layer.
NFL_INTERNATIONAL_VENUES_2026 = {
    "Tottenham Hotspur Stadium": {"location": "London, UK", "map_status": "venue_registered"},
    "Wembley Stadium": {"location": "London, UK", "map_status": "venue_registered"},
    "Bernabéu Stadium": {"location": "Madrid, Spain", "map_status": "venue_registered"},
    "Melbourne Cricket Ground": {"location": "Melbourne, Australia", "map_status": "venue_registered"},
    "Estadio Banorte": {"location": "Mexico City, Mexico", "map_status": "venue_registered"},
    "FC Bayern Munich Arena": {"location": "Munich, Germany", "map_status": "venue_registered"},
    "Stade de France": {"location": "Paris, France", "map_status": "venue_registered"},
    "Maracanã Stadium": {"location": "Rio de Janeiro, Brazil", "map_status": "venue_registered"},
}


def get_team_venue_info(team_name):
    """Return venue metadata for an NFL club without fabricating seat inventory."""
    info = NFL_TEAM_VENUES.get(str(team_name or "").strip())
    return dict(info) if info else None


def get_game_venue_info(game):
    """Resolve the venue metadata used by a schedule/game record."""
    if not isinstance(game, dict):
        return None

    venue_name = str(game.get("venue", "")).strip()

    for team_name, info in NFL_TEAM_VENUES.items():
        if info.get("venue") == venue_name:
            resolved = dict(info)
            resolved["team"] = team_name
            return resolved

    if venue_name in NFL_INTERNATIONAL_VENUES_2026:
        resolved = dict(NFL_INTERNATIONAL_VENUES_2026[venue_name])
        resolved["venue"] = venue_name
        return resolved

    return None





NFL_TEAM_CODES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "JAC": "Jacksonville Jaguars", "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders",
    "LVR": "Las Vegas Raiders", "LAC": "Los Angeles Chargers", "LA": "Los Angeles Rams",
    "LAR": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
NFL_TEAM_NAMES = sorted(set(NFL_TEAM_CODES.values()))
NFL_TEAM_DIVISIONS = {
    "Arizona Cardinals": "NFC West", "Atlanta Falcons": "NFC South", "Baltimore Ravens": "AFC North",
    "Buffalo Bills": "AFC East", "Carolina Panthers": "NFC South", "Chicago Bears": "NFC North",
    "Cincinnati Bengals": "AFC North", "Cleveland Browns": "AFC North", "Dallas Cowboys": "NFC East",
    "Denver Broncos": "AFC West", "Detroit Lions": "NFC North", "Green Bay Packers": "NFC North",
    "Houston Texans": "AFC South", "Indianapolis Colts": "AFC South", "Jacksonville Jaguars": "AFC South",
    "Kansas City Chiefs": "AFC West", "Las Vegas Raiders": "AFC West", "Los Angeles Chargers": "AFC West",
    "Los Angeles Rams": "NFC West", "Miami Dolphins": "AFC East", "Minnesota Vikings": "NFC North",
    "New England Patriots": "AFC East", "New Orleans Saints": "NFC South", "New York Giants": "NFC East",
    "New York Jets": "AFC East", "Philadelphia Eagles": "NFC East", "Pittsburgh Steelers": "AFC North",
    "San Francisco 49ers": "NFC West", "Seattle Seahawks": "NFC West", "Tampa Bay Buccaneers": "NFC South",
    "Tennessee Titans": "AFC South", "Washington Commanders": "NFC East",
}
NFL_INTERNATIONAL_STADIUM_NAMES = {
    "Tottenham Hotspur Stadium", "Wembley Stadium", "Bernabéu Stadium", "Santiago Bernabéu Stadium",
    "Melbourne Cricket Ground", "Estadio Banorte", "FC Bayern Munich Arena", "FC Bayern Munich Stadium",
    "Stade de France", "Maracanã Stadium",
}


def _nfl_full_team_name(value):
    value = str(value or "").strip()
    if value in NFL_TEAM_NAMES:
        return value
    return NFL_TEAM_CODES.get(value.upper(), value)


@st.cache_data(ttl=1800)
def load_nfl_schedule_catalog():
    """Load the current 2026 NFL regular-season schedule for all clubs."""
    url = "https://raw.githubusercontent.com/leesharpe/nfldata/master/data/games.csv"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
    except (requests.RequestException, ValueError, TypeError):
        return []

    games = []
    for raw in reader:
        if str(raw.get("season", "")).strip() != "2026":
            continue
        if str(raw.get("game_type", "")).strip().upper() != "REG":
            continue

        away_team = _nfl_full_team_name(raw.get("away_team"))
        home_team = _nfl_full_team_name(raw.get("home_team"))
        if away_team not in NFL_TEAM_NAMES or home_team not in NFL_TEAM_NAMES:
            continue

        stadium = str(raw.get("stadium", "") or "").strip()
        neutral = (
            str(raw.get("location", "") or "").strip().lower() == "neutral"
            or stadium in NFL_INTERNATIONAL_STADIUM_NAMES
        )
        home_info = NFL_TEAM_VENUES.get(home_team, {})
        venue = stadium or home_info.get("venue") or "Venue TBD"
        location = home_info.get("location", "Location TBD")
        if stadium in NFL_INTERNATIONAL_STADIUM_NAMES:
            location = NFL_INTERNATIONAL_VENUES_2026.get(stadium, {}).get("location", location)

        week_text = str(raw.get("week", "") or "").strip()
        week_match = re.search(r"\d+", week_text)
        week = int(week_match.group()) if week_match else week_text
        game_date = str(raw.get("gameday", "") or "").strip() or None
        game_time = str(raw.get("gametime", "") or "").strip() or None
        game_id = str(raw.get("game_id", "") or f"2026_{week}_{away_team}_{home_team}")
        base = {
            "game_id": game_id,
            "week": week,
            "game_date": game_date,
            "game_time": game_time,
            "kickoff": f"{game_date} {game_time}" if game_date and game_time else (game_date or "Date TBD"),
            "away_team": away_team,
            "home_team": home_team,
            "venue": venue,
            "location": location,
            "stadium_name": stadium,
            "neutral_site": neutral,
            "international_game": stadium in NFL_INTERNATIONAL_STADIUM_NAMES,
        }
        for team, opponent, is_home in ((home_team, away_team, True), (away_team, home_team, False)):
            item = dict(base)
            item.update({
                "team": team,
                "opponent": opponent,
                "home_game": bool(is_home and not neutral),
                "selected_team_home": bool(is_home),
                "matchup": f"{team} vs {opponent}" if is_home or neutral else f"{team} at {opponent}",
            })
            games.append(item)

    games.sort(key=lambda g: (str(g.get("game_date") or "9999"), str(g.get("game_time") or "99:99"), str(g.get("team"))))
    return games


@st.cache_data(ttl=900)
def load_espn_nfl_schedule_catalog():
    """Fallback 2026 NFL schedule feed using ESPN's public scoreboard endpoint."""
    try:
        response = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"limit": 1000, "dates": "20260101-20271231"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return []

    games = []
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        season = (event.get("season") or {}).get("year")
        season_type = str((event.get("season") or {}).get("slug") or "").lower()
        if str(season) != "2026" or (season_type and season_type not in {"regular-season", "regular"}):
            continue
        competitions = event.get("competitions") or []
        if not competitions or not isinstance(competitions[0], dict):
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_name = _nfl_full_team_name((home.get("team") or {}).get("abbreviation"))
        away_name = _nfl_full_team_name((away.get("team") or {}).get("abbreviation"))
        if home_name not in NFL_TEAM_NAMES or away_name not in NFL_TEAM_NAMES:
            continue
        date_value = str(competition.get("date") or event.get("date") or "")
        game_date = date_value[:10] or None
        game_time = date_value[11:16] if len(date_value) >= 16 else None
        week_data = (event.get("week") or {}).get("number")
        week = int(week_data) if str(week_data).isdigit() else week_data or "—"
        venue_obj = competition.get("venue") or {}
        venue = str(venue_obj.get("fullName") or "").strip()
        address = venue_obj.get("address") or {}
        location = ", ".join(
            str(value).strip()
            for value in (address.get("city"), address.get("state"), address.get("country"))
            if value
        ) or NFL_TEAM_VENUES.get(home_name, {}).get("location", "Location TBD")
        neutral = bool(competition.get("neutralSite"))
        base = {
            "game_id": str(event.get("id") or f"2026_{week}_{away_name}_{home_name}"),
            "week": week,
            "game_date": game_date,
            "game_time": game_time,
            "kickoff": f"{game_date} {game_time}" if game_date and game_time else (game_date or "Date TBD"),
            "away_team": away_name,
            "home_team": home_name,
            "venue": venue or NFL_TEAM_VENUES.get(home_name, {}).get("venue", "Venue TBD"),
            "location": location,
            "stadium_name": venue,
            "neutral_site": neutral,
            "international_game": bool(neutral),
        }
        for team, opponent, is_home in ((home_name, away_name, True), (away_name, home_name, False)):
            item = dict(base)
            item.update({
                "team": team,
                "opponent": opponent,
                "home_game": bool(is_home and not neutral),
                "selected_team_home": bool(is_home),
                "matchup": f"{team} vs {opponent}" if is_home or neutral else f"{team} at {opponent}",
            })
            games.append(item)
    games.sort(key=lambda g: (str(g.get("game_date") or "9999"), str(g.get("game_time") or "99:99"), str(g.get("team"))))
    return games


def _nfl_schedule_from_master_dataset():
    """Convert any compatible local schedule cache into the NFL schedule shape."""
    try:
        raw_games = master_dataset.get("games", [])
    except NameError:
        raw_games = []
    if not isinstance(raw_games, list):
        return []

    result = []
    for raw in raw_games:
        if not isinstance(raw, dict):
            continue
        home = _nfl_full_team_name(raw.get("home_team"))
        away = _nfl_full_team_name(raw.get("away_team"))
        if home not in NFL_TEAM_NAMES or away not in NFL_TEAM_NAMES:
            continue
        base = dict(raw)
        base.setdefault("game_id", f"local_{raw.get('week')}_{away}_{home}")
        base.setdefault("venue", NFL_TEAM_VENUES.get(home, {}).get("venue", "Venue TBD"))
        base.setdefault("location", NFL_TEAM_VENUES.get(home, {}).get("location", "Location TBD"))
        base.setdefault("neutral_site", False)
        base.setdefault("international_game", False)
        for team, opponent, is_home in ((home, away, True), (away, home, False)):
            item = dict(base)
            item.update({
                "team": team,
                "opponent": opponent,
                "home_game": bool(is_home and not base.get("neutral_site")),
                "selected_team_home": bool(is_home),
                "matchup": f"{team} vs {opponent}" if is_home else f"{team} at {opponent}",
            })
            result.append(item)
    return result


def get_nfl_schedule_games():
    games = load_nfl_schedule_catalog()
    if games:
        return games
    games = load_espn_nfl_schedule_catalog()
    if games:
        return games
    return _nfl_schedule_from_master_dataset()


def get_team_schedule(team_name, include_completed=True):
    team_name = _nfl_full_team_name(team_name)
    games = [g for g in get_nfl_schedule_games() if g.get("team") == team_name]
    if include_completed:
        return games
    today = datetime.now().strftime("%Y-%m-%d")
    return [g for g in games if not g.get("game_date") or str(g.get("game_date")) >= today]


def get_upcoming_nfl_games(team_name=None, limit=12):
    games = get_nfl_schedule_games()
    today = datetime.now().strftime("%Y-%m-%d")
    games = [g for g in games if not g.get("game_date") or str(g.get("game_date")) >= today]
    if team_name and team_name != "All NFL":
        games = [g for g in games if g.get("team") == _nfl_full_team_name(team_name)]
    else:
        unique = {}
        for g in games:
            key = g.get("game_id")
            if key in unique:
                continue
            game = dict(g)
            game["team"] = game.get("away_team")
            game["opponent"] = game.get("home_team")
            game["home_game"] = False
            game["selected_team_home"] = False
            game["matchup"] = f"{game.get('away_team')} at {game.get('home_team')}"
            unique[key] = game
        games = list(unique.values())
    games.sort(key=lambda g: (str(g.get("game_date") or "9999"), str(g.get("game_time") or "99:99"), str(g.get("team"))))
    return games[:max(1, int(limit))]


def get_nfl_game_for_ticket(ticket):
    team = _nfl_full_team_name(ticket.get("team"))
    if team not in NFL_TEAM_NAMES:
        return None
    week = normalize_week(ticket.get("week"))
    opponent = str(ticket.get("opponent") or "").strip().lower()
    game_date = str(ticket.get("game_date") or "").strip()
    for game in get_team_schedule(team):
        if week is not None and normalize_week(game.get("week")) != week:
            continue
        if opponent and str(game.get("opponent") or "").strip().lower() != opponent:
            continue
        if game_date and str(game.get("game_date") or "")[:10] not in {"", game_date[:10]}:
            continue
        return game
    return None


def _format_nfl_matchup(game):
    connector = "vs" if game.get("neutral_site") or game.get("home_game") else "at"
    return f"{game.get('team')} {connector} {game.get('opponent')}"


def render_nfl_matchup_card(game):
    team = game.get("team") or game.get("home_team") or "NFL Team"
    opponent = game.get("opponent") or "Opponent"
    team_logo = get_nfl_logo_url(team)
    opponent_logo = get_nfl_logo_url(opponent)
    logos = ""
    if team_logo:
        logos += f'<img src="{team_logo}" width="42" height="42" loading="lazy" decoding="async" alt="{team} logo">'
    if opponent_logo:
        logos += f'<img src="{opponent_logo}" width="42" height="42" loading="lazy" decoding="async" alt="{opponent} logo">'
    status = "Neutral" if game.get("neutral_site") else ("Home" if game.get("home_game") else "Away")
    intl = " • International" if game.get("international_game") else ""
    return (
        '<div class="market-matchup-card">'
        f'<div class="market-matchup-top"><div><div class="market-matchup-label">Week {game.get("week", "—")} • {status}{intl}</div>'
        f'<div class="market-matchup-title">{_format_nfl_matchup(game)}</div></div><div class="market-logos">{logos}</div></div>'
        f'<div class="market-matchup-meta">📅 {game.get("game_date") or "Date TBD"} • ⏰ {game.get("game_time") or "Time TBD"}</div>'
        f'<div class="market-matchup-meta">🏟️ {game.get("venue") or "Venue TBD"} • {game.get("location") or "Location TBD"}</div>'
        '</div>'
    )

NFL_DEMO_SECTION_BLUEPRINT = [
    ("101", "Lower Bowl", 72),
    ("125", "Lower Bowl", 88),
    ("201", "Upper Bowl", 58),
    ("225", "Upper Bowl", 68),
    ("305", "Upper Bowl", 50),
    ("325", "Upper Bowl", 62),
]
NFL_DEMO_ROWS = ["4", "8", "12"]


def _demo_normalize_week(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else str(value or "").strip().lower()


def _demo_nfl_price(team, opponent, week, section_base, international=False):
    """Create a stable synthetic price; never represents live market pricing."""
    seed = zlib.crc32(
        f"2026|{team}|{opponent}|{week}".encode("utf-8")
    ) % 21
    matchup_multiplier = 0.90 + (seed / 100.0)
    if international:
        matchup_multiplier += 0.06
    if team and opponent and NFL_TEAM_DIVISIONS.get(team) == NFL_TEAM_DIVISIONS.get(opponent):
        matchup_multiplier += 0.05
    row_multiplier = {"4": 1.16, "8": 1.00, "12": 0.92}
    price = section_base * matchup_multiplier * row_multiplier.get("12", 1.0)
    return max(35, round(price / 5) * 5)


def ensure_nfl_demo_inventory(db_path, schedule_games):
    """
    Seed synthetic seat inventory for upcoming games for every NFL club.

    Each scheduled matchup is represented from both teams' perspectives so a
    user can browse any NFL team, including road games. These are intentionally
    labeled demo inventory and are not live Ticketmaster availability.
    """
    if not schedule_games:
        return 0

    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ticket_inventory'"
        ).fetchone()
        if table_check is None:
            return 0

        columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket_inventory)").fetchall()}
        if "team" not in columns:
            conn.execute("ALTER TABLE ticket_inventory ADD COLUMN team TEXT")
            columns.add("team")

        # Migrate legacy rows without assuming a particular NFL club.
        legacy_rows = conn.execute(
            "SELECT id, week, opponent, game_date FROM ticket_inventory WHERE team IS NULL OR TRIM(team)=''"
        ).fetchall()
        for row_id, legacy_week, legacy_opponent, legacy_date in legacy_rows:
            candidate_teams = []
            week_key = _demo_normalize_week(legacy_week)
            opponent_key = _nfl_full_team_name(legacy_opponent)
            date_key = str(legacy_date or "")[:10]
            for schedule_row in schedule_games:
                if _demo_normalize_week(schedule_row.get("week")) != week_key:
                    continue
                for candidate in (schedule_row.get("team"), schedule_row.get("away_team"), schedule_row.get("home_team")):
                    candidate = _nfl_full_team_name(candidate)
                    if candidate not in NFL_TEAM_NAMES:
                        continue
                    if _nfl_full_team_name(schedule_row.get("opponent")) == opponent_key or _nfl_full_team_name(schedule_row.get("home_team")) == opponent_key or _nfl_full_team_name(schedule_row.get("away_team")) == opponent_key:
                        if candidate not in candidate_teams:
                            candidate_teams.append(candidate)
            if date_key:
                dated = [c for c in schedule_games if str(c.get("game_date") or "")[:10] == date_key and _nfl_full_team_name(c.get("opponent")) == opponent_key]
                dated_teams = []
                for c in dated:
                    for candidate in (c.get("team"), c.get("home_team"), c.get("away_team")):
                        candidate = _nfl_full_team_name(candidate)
                        if candidate in NFL_TEAM_NAMES and candidate not in dated_teams:
                            dated_teams.append(candidate)
                if dated_teams:
                    candidate_teams = dated_teams
            if candidate_teams:
                conn.execute("UPDATE ticket_inventory SET team=? WHERE id=?", (candidate_teams[0], row_id))

        required = {"id", "week", "opponent", "game_date", "section", "row", "price", "quantity"}
        if not required.issubset(columns):
            return 0

        existing_rows = conn.execute(
            "SELECT team, week, opponent, section, row FROM ticket_inventory"
        ).fetchall()
        existing_keys = set()
        for team, week, opponent, section, row in existing_rows:
            existing_keys.add((
                _nfl_full_team_name(team) if team else "",
                _demo_normalize_week(week),
                str(opponent or "").strip().lower(),
                str(section or "").strip(),
                str(row or "").strip(),
            ))

        next_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM ticket_inventory").fetchone()[0] or 0
        ) + 1
        has_source = "source" in columns
        has_last_updated = "last_updated" in columns
        has_event_id = "event_id" in columns
        today = datetime.now().date()

        # Collapse the team-centric schedule back to one physical matchup.
        physical_games = {}
        for game in schedule_games:
            if not isinstance(game, dict):
                continue
            game_id = str(game.get("game_id") or "").strip()
            if not game_id:
                game_id = (
                    f"2026_{game.get('week')}_"
                    f"{game.get('away_team')}_{game.get('home_team')}"
                )
            physical_games[game_id] = game

        for base_game in physical_games.values():
            game_date = str(base_game.get("game_date") or "").strip()
            if game_date:
                try:
                    if datetime.fromisoformat(game_date[:10]).date() < today:
                        continue
                except ValueError:
                    pass

            away = _nfl_full_team_name(base_game.get("away_team"))
            home = _nfl_full_team_name(base_game.get("home_team"))
            if away not in NFL_TEAM_NAMES or home not in NFL_TEAM_NAMES:
                continue

            week = _demo_normalize_week(base_game.get("week"))
            venue = base_game.get("venue") or NFL_TEAM_VENUES.get(home, {}).get("venue", "NFL Venue")
            international = bool(base_game.get("international_game"))

            for viewing_team, opponent in ((home, away), (away, home)):
                for section, area, base_price in NFL_DEMO_SECTION_BLUEPRINT:
                    for row in NFL_DEMO_ROWS:
                        key = (
                            viewing_team,
                            week,
                            opponent.strip().lower(),
                            section,
                            row,
                        )
                        if key in existing_keys:
                            continue

                        # Stable row variation keeps the inventory varied without randomness.
                        row_multiplier = {"4": 1.16, "8": 1.00, "12": 0.92}[row]
                        seed = zlib.crc32(
                            f"{home}|{away}|{week}|{section}|{row}".encode("utf-8")
                        ) % 21
                        matchup_multiplier = 0.90 + (seed / 100.0)
                        if international:
                            matchup_multiplier += 0.06
                        if NFL_TEAM_DIVISIONS.get(viewing_team) == NFL_TEAM_DIVISIONS.get(opponent):
                            matchup_multiplier += 0.05

                        price = max(
                            35,
                            round((base_price * matchup_multiplier * row_multiplier) / 5) * 5,
                        )
                        quantity = {"4": 2, "8": 4, "12": 6}[row]

                        fields = [
                            "id", "week", "opponent", "game_date", "section", "row", "price", "quantity", "team"
                        ]
                        values = [
                            next_id, week, opponent, game_date or None, section, row, price, quantity, viewing_team
                        ]

                        if has_event_id:
                            fields.append("event_id")
                            values.append(f"DEMO-NFL-2026-{str(base_game.get('game_id'))[:48]}")
                        if has_source:
                            fields.append("source")
                            values.append(f"KickSeatz Demo Inventory • {venue} baseline")
                        if has_last_updated:
                            fields.append("last_updated")
                            values.append(datetime.now().isoformat())

                        placeholders = ", ".join("?" for _ in fields)
                        conn.execute(
                            f"INSERT INTO ticket_inventory ({', '.join(fields)}) VALUES ({placeholders})",
                            values,
                        )
                        existing_keys.add(key)
                        next_id += 1
                        inserted += 1

        conn.commit()
        return inserted
    finally:
        conn.close()


# ============================================================
# VISUAL BRANDING / MATCHUP HELPERS
# ============================================================

NFL_LOGO_CODES = {
    "Arizona Cardinals": "ari",
    "Atlanta Falcons": "atl",
    "Baltimore Ravens": "bal",
    "Buffalo Bills": "buf",
    "Carolina Panthers": "car",
    "Chicago Bears": "chi",
    "Cincinnati Bengals": "cin",
    "Cleveland Browns": "cle",
    "Dallas Cowboys": "dal",
    "Denver Broncos": "den",
    "Detroit Lions": "det",
    "Green Bay Packers": "gb",
    "Houston Texans": "hou",
    "Indianapolis Colts": "ind",
    "Jacksonville Jaguars": "jax",
    "Kansas City Chiefs": "kc",
    "Las Vegas Raiders": "lv",
    "Los Angeles Chargers": "lac",
    "Los Angeles Rams": "lar",
    "Miami Dolphins": "mia",
    "Minnesota Vikings": "min",
    "New England Patriots": "ne",
    "New Orleans Saints": "no",
    "New York Giants": "nyg",
    "New York Jets": "nyj",
    "Philadelphia Eagles": "phi",
    "Pittsburgh Steelers": "pit",
    "San Francisco 49ers": "sf",
    "Seattle Seahawks": "sea",
    "Tampa Bay Buccaneers": "tb",
    "Tennessee Titans": "ten",
    "Washington Commanders": "wsh",
}


def get_nfl_logo_url(team_name):
    """Return an ESPN-hosted NFL team logo URL for visual branding."""
    code = NFL_LOGO_CODES.get(str(team_name or "").strip())
    if not code:
        return None
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"


def _visual_game_date(value):
    if not value:
        return "Date TBD"
    try:
        parsed = datetime.fromisoformat(str(value)).date()
        return parsed.strftime("%b %d, %Y").replace(" 0", " ")
    except (ValueError, TypeError):
        return str(value)


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown("""
<style>

.hero {
    padding: 30px 34px;
    border-radius: 20px;
    background: linear-gradient(135deg, #2563eb 0%, #111111 100%);
    color: white;
    margin-bottom: 24px;
    box-shadow: 0 8px 28px rgba(0,0,0,.12);
}

.hero h1 {
    font-size: 46px;
    margin: 0 0 4px 0;
    font-weight: 850;
}

.hero p {
    margin: 5px 0;
    font-size: 18px;
}

.hero .tagline {
    color: #f1f5f9;
    font-size: 15px;
}

.mobile-note {
    font-size: 0.85rem;
}

.matchup-card {
    border: 1px solid rgba(100,116,139,.20);
    border-radius: 20px;
    padding: 18px 18px 16px;
    background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
    box-shadow: 0 8px 24px rgba(15,23,42,.07);
    margin: 6px 0 14px;
}

.matchup-topline {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    color: #64748b;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: .08em;
}

.matchup-teams {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 10px;
    margin: 15px 0 12px;
}

.matchup-team {
    text-align: center;
    min-width: 0;
}

.matchup-logo {
    display: block;
    width: 72px;
    height: 72px;
    object-fit: contain;
    margin: 0 auto 7px;
}

.logo-fallback {
    width: 72px;
    height: 72px;
    margin: 0 auto 7px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    background: #111827;
    color: white;
    font-weight: 900;
    font-size: 13px;
}

.matchup-team-name {
    font-weight: 850;
    font-size: 14px;
    line-height: 1.2;
}

.matchup-vs {
    font-size: 12px;
    font-weight: 900;
    color: #2563eb;
    letter-spacing: .08em;
}

.matchup-status {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid rgba(100,116,139,.14);
    color: #64748b;
    font-size: 12px;
    font-weight: 650;
}

.matchup-meta {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 6px 12px;
    color: #475569;
    font-size: 12px;
    text-align: center;
}

.recommendation-matchup {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 14px;
    padding: 8px 0 4px;
}

.recommendation-matchup img {
    display: block;
    width: 56px;
    height: 56px;
    object-fit: contain;
}

.mbs-visual {
    border: 1px solid rgba(100,116,139,.20);
    border-radius: 20px;
    padding: 18px;
    background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
    color: #f8fafc;
    box-shadow: 0 8px 26px rgba(15,23,42,.10);
}

.mbs-field {
    text-align: center;
    border: 2px solid rgba(255,255,255,.55);
    border-radius: 14px;
    padding: 13px 10px;
    margin-bottom: 12px;
    font-weight: 900;
    letter-spacing: .10em;
    font-size: 12px;
}

.baseline-level {
    border-top: 1px solid rgba(255,255,255,.10);
    padding: 13px 0;
}

.baseline-level > div:first-child {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 9px;
}

.baseline-level > div:first-child span {
    color: #cbd5e1;
    font-size: 12px;
}

.baseline-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
}

.section-pill {
    display: inline-flex;
    min-width: 42px;
    min-height: 32px;
    align-items: center;
    justify-content: center;
    padding: 5px 9px;
    border-radius: 999px;
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.12);
    font-size: 12px;
    font-weight: 800;
}

.mbs-note {
    padding-top: 10px;
    color: #cbd5e1;
    font-size: 11px;
    text-align: center;
}


.feature-card {
    border: 1px solid rgba(100,116,139,.22);
    border-radius: 16px;
    padding: 16px 18px;
    margin: 8px 0;
    background: rgba(255,255,255,.92);
}

.seat-map-card {
    border: 1px solid rgba(100,116,139,.25);
    border-radius: 18px;
    padding: 14px;
    background: #fafafa;
}

.watch-card {
    border: 1px solid rgba(167,25,48,.28);
    border-radius: 16px;
    padding: 16px 18px;
    margin: 8px 0;
    background: rgba(167,25,48,.035);
}

.status-pill {
    display: inline-block;
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .02em;
    background: #f1f5f9;
}

.small-muted {
    color: #64748b;
    font-size: 13px;
}

@media (max-width: 768px) {
    .hero {
        padding: 22px 20px;
        border-radius: 16px;
    }

    .hero h1 {
        font-size: 34px;
    }

    .hero p {
        font-size: 15px;
    }

    .section-title {
        font-size: 20px;
    }

    div[data-testid="stMetricValue"] {
        font-size: 1.25rem;
    }
}

    .interactive-map-field {
        margin: 10px 0 14px 0;
        padding: 14px 18px;
        border-radius: 16px;
        background: linear-gradient(135deg, #111827, #1f2937);
        color: white;
        text-align: center;
        font-weight: 900;
        letter-spacing: .06em;
        border: 1px solid rgba(255,255,255,.08);
    }

    .map-level-label {
        font-size: 12px;
        font-weight: 900;
        letter-spacing: .08em;
        text-transform: uppercase;
        color: #64748b;
        margin: 12px 0 6px 0;
    }

</style>
""", unsafe_allow_html=True)

# ============================================================
# DATA LOADING
# ============================================================

@st.cache_data
def load_master_dataset(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def ensure_inventory_team_column(db_path):
    conn = sqlite3.connect(db_path)
    try:
        table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ticket_inventory'").fetchone()
        if table_check is None:
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket_inventory)").fetchall()}
        if "team" not in columns:
            conn.execute("ALTER TABLE ticket_inventory ADD COLUMN team TEXT")
            conn.execute("UPDATE ticket_inventory SET team = 'Unknown' WHERE team IS NULL OR TRIM(team) = ''")
            conn.commit()
    finally:
        conn.close()

def load_inventory(path):

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Database file does not exist:\n{path}"
        )

    conn = sqlite3.connect(path)

    try:
        table_check = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'ticket_inventory'
        """).fetchone()

        if table_check is None:
            raise RuntimeError(
                f"The database was found, but the table "
                f"'ticket_inventory' does not exist.\n\n"
                f"Database:\n{path}"
            )

        columns = conn.execute(
            "PRAGMA table_info(ticket_inventory)"
        ).fetchall()

        column_names = {column[1] for column in columns}

        required_columns = {
            "id",
            "week",
            "opponent",
            "game_date",
            "section",
            "row",
            "price",
            "quantity",
        }

        missing_columns = required_columns - column_names

        if missing_columns:
            raise RuntimeError(
                "The ticket_inventory table is missing required "
                f"column(s): {', '.join(sorted(missing_columns))}\n\n"
                f"Actual columns found:\n"
                f"{', '.join(sorted(column_names))}"
            )

        optional_selects = []
        if "team" in column_names:
            optional_selects.append("team")
        else:
            optional_selects.append("'Unknown' AS team")

        if "source" in column_names:
            optional_selects.append("source")
        else:
            optional_selects.append("NULL AS source")

        if "last_updated" in column_names:
            optional_selects.append("last_updated")
        else:
            optional_selects.append("NULL AS last_updated")

        rows = conn.execute(
            """
            SELECT
                id,
                week,
                opponent,
                game_date,
                section,
                row,
                price,
                quantity AS available_quantity,
                {optional_selects}
            FROM ticket_inventory
            """.format(
                optional_selects=",\n                ".join(optional_selects)
            )
        ).fetchall()

    except sqlite3.Error as e:
        raise RuntimeError(
            f"SQLite database error while loading ticket inventory:\n"
            f"{e}\n\n"
            f"Database:\n{path}"
        ) from e

    finally:
        conn.close()

    inventory_rows = []

    for r in rows:
        try:
            inventory_rows.append(
                {
                    "id": r[0],
                    "week": r[1],
                    "opponent": r[2],
                    "game_date": r[3],
                    "section": r[4],
                    "row": r[5],
                    "price": float(r[6]),
                    "available_quantity": int(r[7]),
                    "team": r[8] or "Unknown",
                    "source": r[9],
                    "last_updated": r[10],
                }
            )
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"Invalid ticket_inventory data encountered.\n"
                f"Database row:\n{r}\n\n"
                f"Error:\n{e}"
            ) from e

    return inventory_rows
def record_price_history(inventory):
    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                price REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )

        recorded_at = datetime.now().isoformat()

        for ticket in inventory:

            latest = conn.execute(
                """
                SELECT price
                FROM price_history
                WHERE ticket_id = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (ticket["id"],),
            ).fetchone()

            current_price = float(
                ticket["price"]
            )

            if latest is None or float(latest[0]) != current_price:

                conn.execute(
                    """
                    INSERT INTO price_history (
                        ticket_id,
                        price,
                        recorded_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        ticket["id"],
                        current_price,
                        recorded_at,
                    ),
                )

        conn.commit()

    finally:
        conn.close()

try:
    master_dataset = load_master_dataset(
        MASTER_DATA_PATH
    )

    # Use the league-wide schedule as the source of truth.
    nfl_schedule_now = get_nfl_schedule_games()
    if isinstance(master_dataset, dict) and nfl_schedule_now:
        master_dataset["games"] = nfl_schedule_now

    # Make the demo marketplace NFL-wide. Existing rows are preserved, while
    # synthetic upcoming inventory is added for every scheduled matchup.
    ensure_inventory_team_column(DB_PATH)
    nfl_schedule_for_inventory = get_nfl_schedule_games()
    ensure_nfl_demo_inventory(DB_PATH, nfl_schedule_for_inventory)

    inventory = load_inventory(
        DB_PATH
    )

    record_price_history(
        inventory
    )

except Exception as e:
    st.error("KickSeatz could not load its data.")
    st.exception(e)
    st.stop()

# ============================================================
# SCORING DATA
# ============================================================

OPPONENT_POWER_RANKINGS = {
    "Pittsburgh Steelers": 21,
    "Carolina Panthers": 23,
    "Green Bay Packers": 12,
    "New Orleans Saints": 22,
    "Baltimore Ravens": 6,
    "Chicago Bears": 11,
    "San Francisco 49ers": 17,
    "Tampa Bay Buccaneers": 18,
    "Cincinnati Bengals": 7,
    "Kansas City Chiefs": 14,
    "Minnesota Vikings": 19,
    "Detroit Lions": 15,
    "Cleveland Browns": 30,
    "Washington Commanders": 31,
}

DIVISION_RIVALS = {
    "Carolina Panthers",
    "New Orleans Saints",
    "Tampa Bay Buccaneers",
}

WEIGHTS = {
    "Best Overall Value": {
        "game": 0.40,
        "price": 0.25,
        "seat": 0.20,
        "availability": 0.15,
    },
    "Lowest Price": {
        "game": 0.10,
        "price": 0.70,
        "seat": 0.10,
        "availability": 0.10,
    },
    "Best Game": {
        "game": 0.75,
        "price": 0.05,
        "seat": 0.10,
        "availability": 0.10,
    },
    "Best Seats": {
        "game": 0.15,
        "price": 0.10,
        "seat": 0.65,
        "availability": 0.10,
    },
}

# ============================================================
# GAME LOOKUP
# ============================================================

def normalize_week(week):
    """
    Makes all of these equivalent:

        2
        "2"
        "Week 2"
        "week 02"
        "Week 02.0"
    """

    if week is None:
        return None

    text = str(week).strip().lower()

    match = re.search(r"\d+", text)

    if match:
        return int(match.group())

    return text


def get_game_by_week(week, team=None, opponent=None, game_date=None):
    """Find the exact NFL game when team/opponent details are available."""
    normalized_target = normalize_week(week)
    team_name = _nfl_full_team_name(team) if team else None
    opponent_name = _nfl_full_team_name(opponent) if opponent else None
    date_key = str(game_date or "").strip()[:10]

    if team_name:
        games = get_team_schedule(team_name)
    else:
        games = (
            master_dataset.get("games", [])
            if isinstance(master_dataset, dict)
            else master_dataset
        )
        if not isinstance(games, list):
            games = []

    for game in games:
        if not isinstance(game, dict):
            continue
        if normalize_week(game.get("week")) != normalized_target:
            continue
        if team_name and _nfl_full_team_name(game.get("team")) != team_name:
            continue
        if opponent_name and _nfl_full_team_name(game.get("opponent")) != opponent_name:
            continue
        if date_key and str(game.get("game_date") or "").strip()[:10] not in {"", date_key}:
            continue
        return game

    return None


# ============================================================
# SCORING FUNCTIONS
# ============================================================

def is_division_rival(game):
    """Return True when the matchup is between two clubs in the same division."""
    if not isinstance(game, dict):
        return False
    team = _nfl_full_team_name(game.get("team"))
    opponent = _nfl_full_team_name(game.get("opponent"))
    if not team or not opponent or team == opponent:
        return False
    return (
        NFL_TEAM_DIVISIONS.get(team) is not None
        and NFL_TEAM_DIVISIONS.get(team) == NFL_TEAM_DIVISIONS.get(opponent)
    )


def calculate_game_score(game):

    if not game:
        return 0

    opponent = game.get("opponent", "")
    score = 50

    opponent_rank = OPPONENT_POWER_RANKINGS.get(
        opponent,
        32,
    )

    opponent_strength = round(
        100
        - (
            (opponent_rank - 1)
            / 31
        ) * 50
    )

    score += (
        opponent_strength - 70
    ) * 0.30

    if game.get("home_game"):
        score += 15

    if is_division_rival(game):
        score += 15

    if game.get("ticketmaster_available"):
        score += 5

    if game.get("seatmap_url"):
        score += 5

    if game.get("safe_tix_enabled"):
        score += 5

    if game.get("all_inclusive_pricing"):
        score += 5

    return round(
        max(
            0,
            min(
                score,
                100,
            ),
        )
    )

def calculate_seat_quality(ticket):

    section = str(ticket.get("section", ""))
    row = str(ticket.get("row", ""))

    score = 5

    try:
        section_number = int(
            "".join(
                c for c in section
                if c.isdigit()
            )
        )

        if 101 <= section_number <= 134:
            score += 3

    except ValueError:
        pass

    try:
        row_number = int(
            "".join(
                c for c in row
                if c.isdigit()
            )
        )

        if row_number <= 5:
            score += 2

        elif row_number <= 10:
            score += 1

    except ValueError:
        pass

    return min(score, 10)


def calculate_price_score(price, comparable_prices):
    """
    Data-driven price value score.

    The score is based on where this ticket's price ranks
    compared with the actual available ticket inventory.
    """

    if not comparable_prices:
        return 50

    prices = sorted(
        float(p)
        for p in comparable_prices
        if p is not None
    )

    if not prices:
        return 50

    price = float(price)

    cheaper_or_equal = sum(
        1 for p in prices
        if p >= price
    )

    percentile = cheaper_or_equal / len(prices)

    return round(
        max(
            0,
            min(
                percentile * 100,
                100
            )
        )
    )


def _same_inventory_game(a, b):
    """Match inventory rows from the same team/opponent/season week."""
    a_team = _nfl_full_team_name(a.get("team") or "Unknown")
    b_team = _nfl_full_team_name(b.get("team") or "Unknown")
    return (
        a_team == b_team
        and normalize_week(a.get("week")) == normalize_week(b.get("week"))
        and str(a.get("opponent") or "").strip().lower()
        == str(b.get("opponent") or "").strip().lower()
    )


def calculate_confidence(
    ticket,
    game,
):

    confidence = 40

    comparable_count = sum(
        1
        for t in inventory
        if _same_inventory_game(t, ticket)
        and int(t.get("available_quantity", 0)) > 0
    )

    if comparable_count >= 10:
        confidence += 30

    elif comparable_count >= 6:
        confidence += 25

    elif comparable_count >= 3:
        confidence += 15

    elif comparable_count >= 2:
        confidence += 5

    history_conn = sqlite3.connect(DB_PATH)

    try:
        history_count = history_conn.execute(
            """
            SELECT COUNT(*)
            FROM price_history
            WHERE ticket_id = ?
            """,
            (ticket["id"],),
        ).fetchone()[0]

    finally:
        history_conn.close()

    if history_count >= 10:
        confidence += 20

    elif history_count >= 5:
        confidence += 15

    elif history_count >= 2:
        confidence += 10

    if game:
        confidence += 10

    return min(
        confidence,
        100,
    )


def calculate_availability(
    ticket,
    requested_quantity,
):

    available = ticket.get(
        "available_quantity",
        0,
    )

    if available < requested_quantity:
        return 0

    return min(
        100,
        available * 25,
    )


def calculate_budget_opportunity(
    price,
    budget,
):

    if budget <= 0 or price > budget:
        return 0

    utilization = price / budget

    if utilization < 0.45:
        return 55

    elif utilization < 0.60:
        return 70

    elif utilization < 0.75:
        return 85

    elif utilization < 0.90:
        return 100

    return 90


def _clamp(value, low=0, high=100):
    return max(low, min(high, float(value)))


def get_ticket_history_stats(ticket_id):
    """Return observed price-history statistics for one ticket.

    This function intentionally uses only recorded observations. It does not
    predict future prices.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """
            SELECT price
            FROM price_history
            WHERE ticket_id = ?
            ORDER BY recorded_at
            """,
            (ticket_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    prices = [float(row[0]) for row in rows if row and row[0] is not None]
    if not prices:
        return {
            "sample_size": 0,
            "low": None,
            "high": None,
            "median": None,
            "change": 0.0,
        }

    return {
        "sample_size": len(prices),
        "low": min(prices),
        "high": max(prices),
        "median": statistics.median(prices),
        "change": prices[-1] - prices[0],
    }


def calculate_opportunity_score(ticket, game, eligible_pairs, budget, ticket_count):
    """Find unusual value that a normal cheapest-ticket sort would miss.

    The score is intentionally separate from the user's selected priority. It
    combines relative price, seat value, budget efficiency, observed history,
    and inventory scarcity. All signals are descriptive rather than predictive.
    """
    current_price = float(ticket.get("price", 0))
    seat_score = calculate_seat_quality(ticket) * 10

    same_game = [
        t for t, g in eligible_pairs
        if _same_inventory_game(t, ticket)
        and int(t.get("available_quantity", 0)) >= ticket_count
    ]
    same_game_prices = [float(t.get("price", 0)) for t in same_game if float(t.get("price", 0)) > 0]

    if same_game_prices:
        median_price = statistics.median(same_game_prices)
        relative_price_score = _clamp(50 + ((median_price - current_price) / median_price) * 180) if median_price else 50
    else:
        median_price = current_price
        relative_price_score = 50

    value_ratios = []
    for t in same_game:
        price = float(t.get("price", 0))
        if price > 0:
            value_ratios.append((calculate_seat_quality(t) * 10) / price)

    candidate_ratio = (seat_score / current_price) if current_price > 0 else 0
    if value_ratios and candidate_ratio > 0:
        better_or_equal = sum(1 for ratio in value_ratios if ratio <= candidate_ratio)
        seat_value_score = (better_or_equal / len(value_ratios)) * 100
    else:
        seat_value_score = 50

    if budget > 0:
        utilization = current_price / budget
        budget_efficiency_score = _clamp(100 - abs(utilization - 0.75) * 140)
    else:
        budget_efficiency_score = 50

    history = get_ticket_history_stats(ticket.get("id"))
    if history["sample_size"] >= 2 and history["median"]:
        history_score = _clamp(50 + ((history["median"] - current_price) / history["median"]) * 180)
    else:
        history_score = 50

    available = int(ticket.get("available_quantity", 0))
    if available <= ticket_count:
        scarcity_score = 90
    elif available <= ticket_count + 1:
        scarcity_score = 78
    elif available <= 4:
        scarcity_score = 62
    else:
        scarcity_score = 45

    opportunity_score = round(
        relative_price_score * 0.30
        + seat_value_score * 0.30
        + budget_efficiency_score * 0.15
        + history_score * 0.15
        + scarcity_score * 0.10
    )

    if opportunity_score >= 85:
        label = "Hidden Opportunity"
    elif opportunity_score >= 70:
        label = "Strong Opportunity"
    elif opportunity_score >= 55:
        label = "Worth Considering"
    else:
        label = "Standard Market Value"

    reasons = []
    if current_price < median_price:
        reasons.append(f"${median_price - current_price:.0f} below the same-game median")
    if seat_value_score >= 75:
        reasons.append("strong seat quality for the price")
    if history["sample_size"] >= 2 and history["median"] and current_price < history["median"]:
        reasons.append("below this ticket's observed historical median")
    if available <= ticket_count + 1:
        reasons.append("limited inventory for your requested quantity")
    if not reasons:
        reasons.append("balanced price, seat, budget, and availability signals")

    return {
        "score": int(_clamp(opportunity_score)),
        "label": label,
        "same_game_median": round(median_price, 2),
        "relative_price_score": round(relative_price_score),
        "seat_value_score": round(seat_value_score),
        "budget_efficiency_score": round(budget_efficiency_score),
        "history_score": round(history_score),
        "scarcity_score": round(scarcity_score),
        "history": history,
        "reason": " • ".join(reasons[:2]),
    }


def get_opportunity_upgrade(candidate, candidates):
    """Identify the cheapest meaningful upgrade from the selected ticket."""
    base_ticket = candidate["ticket"]
    base_seat = calculate_seat_quality(base_ticket) * 10
    base_price = float(base_ticket["price"])

    upgrades = []
    for other in candidates:
        if other["ticket"]["id"] == base_ticket["id"]:
            continue
        other_price = float(other["ticket"]["price"])
        other_seat = calculate_seat_quality(other["ticket"]) * 10
        if other_price <= base_price or other_seat <= base_seat:
            continue
        extra_cost = other_price - base_price
        seat_gain = other_seat - base_seat
        efficiency = seat_gain / extra_cost if extra_cost > 0 else 0
        upgrades.append((efficiency, extra_cost, seat_gain, other))

    if not upgrades:
        return None

    upgrades.sort(key=lambda x: (x[0], x[2], -x[1]), reverse=True)
    _efficiency, extra_cost, seat_gain, upgrade = upgrades[0]
    return {
        "candidate": upgrade,
        "extra_cost": round(extra_cost, 2),
        "seat_gain": round(seat_gain),
    }


def calculate_ticket_score(
    ticket,
    game,
    budget,
    ticket_count,
    priority,
):

    if not game:
        return -1

    price = float(
        ticket.get("price", 0)
    )

    available = int(
        ticket.get(
            "available_quantity",
            0,
        )
    )

    if available < ticket_count:
        return -1

    if price > budget:
        return -1

    game_quality = calculate_game_score(game)

    price_score = calculate_price_score(
        price,
        [
            float(t.get("price", 0))
            for t in inventory
            if _same_inventory_game(t, ticket)
            and int(t.get("available_quantity", 0)) >= ticket_count
        ]
    )

    seat_quality = (
        calculate_seat_quality(ticket)
        * 10
    )

    availability = calculate_availability(
        ticket,
        ticket_count,
    )

    w = WEIGHTS[priority]

    score = (
        game_quality * w["game"]
        + price_score * w["price"]
        + seat_quality * w["seat"]
        + availability * w["availability"]
    )

    confidence = calculate_confidence(
        ticket,
        game,
    )

    confidence_multiplier = (
        0.85
        + (confidence / 100) * 0.15
    )

    score = score * confidence_multiplier

    return round(
        max(
            0,
            min(score, 100)
        )
    )

# ============================================================
# RECOMMENDATION ENGINE
# ============================================================

def get_eligible_tickets(
    budget,
    ticket_count,
    selected_week=None,
    seat_area_filter="Any",
    min_seat_quality=0,
    rivals_only=False,
):

    eligible = []

    for ticket in inventory:

        price = float(
            ticket.get("price", 0)
        )

        quantity = int(
            ticket.get(
                "available_quantity",
                0,
            )
        )

        if price > budget:
            continue

        if quantity < ticket_count:
            continue

        if selected_week is not None and normalize_week(ticket.get("week")) != normalize_week(selected_week):
            continue

        if seat_area_filter != "Any" and get_seat_area(ticket.get("section")) != seat_area_filter:
            continue

        if calculate_seat_quality(ticket) * 10 < int(min_seat_quality):
            continue

        game = get_game_by_week(
            ticket.get("week"),
            team=ticket.get("team"),
            opponent=ticket.get("opponent"),
            game_date=ticket.get("game_date"),
        )

        if game is None:
            continue

        if rivals_only and game.get("opponent") not in DIVISION_RIVALS:
            continue

        eligible.append(
            (ticket, game)
        )

    return eligible


def score_candidates(
    budget,
    ticket_count,
    priority,
    selected_week=None,
    seat_area_filter="Any",
    min_seat_quality=0,
    rivals_only=False,
):

    candidates = []

    for ticket, game in get_eligible_tickets(
        budget,
        ticket_count,
        selected_week,
        seat_area_filter,
        min_seat_quality,
        rivals_only,
    ):

        score = calculate_ticket_score(
            ticket,
            game,
            budget,
            ticket_count,
            priority,
        )

        if score >= 0:

            candidates.append(
                {
                    "ticket": ticket,
                    "game": game,
                    "score": score,
                }
            )

    # Opportunity analysis is deliberately calculated after the candidate
    # pool is known, so it can compare each ticket against the actual options
    # available to this user rather than using an arbitrary fixed benchmark.
    eligible_pairs = get_eligible_tickets(
        budget,
        ticket_count,
        selected_week,
        seat_area_filter,
        min_seat_quality,
        rivals_only,
    )

    for candidate in candidates:
        candidate["opportunity"] = calculate_opportunity_score(
            candidate["ticket"],
            candidate["game"],
            eligible_pairs,
            budget,
            ticket_count,
        )

    if priority == "Lowest Price":

        candidates.sort(
            key=lambda x: (
                x["ticket"]["price"],
                -calculate_game_score(
                    x["game"]
                ),
                -calculate_seat_quality(
                    x["ticket"]
                ),
            )
        )

    elif priority == "Best Game":

        candidates.sort(
            key=lambda x: (
                calculate_game_score(
                    x["game"]
                ),
                calculate_seat_quality(
                    x["ticket"]
                ),
                x["score"],
                -x["ticket"]["price"],
            ),
            reverse=True,
        )

    else:

        candidates.sort(
            key=lambda x: (
                x["score"],
                calculate_game_score(
                    x["game"]
                ),
                calculate_seat_quality(
                    x["ticket"]
                ),
                -x["ticket"]["price"],
            ),
            reverse=True,
        )

    return candidates


# ============================================================
# SMART EXPLANATION
# ============================================================

def get_reasons(
    ticket,
    game,
    budget,
    ticket_count,
    priority,
):

    price = float(
        ticket["price"]
    )

    opponent = game.get(
        "opponent",
        ""
    )

    game_score = calculate_game_score(
        game
    )

    seat_score = (
        calculate_seat_quality(ticket)
        * 10
    )

    availability = int(
        ticket["available_quantity"]
    )

    budget_left = (
        budget - price
    )

    reasons = []

    opponent_rating = OPPONENT_POWER_RANKINGS.get(
        opponent,
        70
    )

    if opponent_rating >= 90:
        reasons.append(
            f"{opponent} has a strong opponent rating "
            f"of {opponent_rating}/100."
        )

    elif opponent_rating >= 80:
        reasons.append(
            f"{opponent} has an above-average opponent rating "
            f"of {opponent_rating}/100."
        )

    else:
        reasons.append(
            f"{opponent} has an opponent rating of "
            f"{opponent_rating}/100."
        )

    if game.get("home_game"):
        reasons.append(
            f"{game.get('team', 'The selected team')} is playing at home, which adds value "
            "to the matchup."
        )

    else:
        reasons.append(
            "This is an away game, so KickSeatz does not "
            "apply the home-game advantage."
        )

    if is_division_rival(game):
        reasons.append(
            f"{opponent} is a division rival, giving the "
            f"matchup additional rivalry value."
        )

    if priority == "Lowest Price":
        reasons.append(
            f"At ${price:.0f}/ticket, this is one of the "
            f"lowest-priced eligible options."
        )

    elif priority == "Best Game":
        reasons.append(
            f"This matchup earns a {game_score}/100 "
            f"Game Quality score."
        )

    elif priority == "Best Seats":
        reasons.append(
            f"Section {ticket['section']} and Row {ticket['row']} "
            f"drive a {seat_score}/100 Seat Quality score."
        )

    elif priority == "Custom Mix":
        reasons.append(
            f"Your custom scoring mix is driving the recommendation with "
            f"a {game_score}/100 game score and {seat_score}/100 seat score."
        )

    else:
        reasons.append(
            f"It combines a {game_score}/100 Game Quality "
            f"score with a ${price:.0f} ticket."
        )

    if seat_score >= 80:
        reasons.append(
            f"Section {ticket['section']} and Row {ticket['row']} "
            f"provide a strong seat-quality score."
        )

    elif seat_score >= 60:
        reasons.append(
            f"Section {ticket['section']} provides a solid "
            f"seat-quality score."
        )

    if availability >= max(
        ticket_count,
        3,
    ):
        reasons.append(
            f"{availability} tickets are available, giving "
            f"your group flexibility."
        )

    if budget_left > 0:
        reasons.append(
            f"It stays ${budget_left:.0f} under your maximum "
            f"budget per ticket."
        )

    else:
        reasons.append(
            "It fits your maximum budget exactly."
        )

    return reasons[:4]


# ============================================================
# RATE MY TICKET
# ============================================================

def rate_ticket(
    ticket,
    game,
    purchased=False,
):

    game_score = calculate_game_score(
        game
    )

    price_score = calculate_price_score(
        float(ticket["price"]),
        [
            float(t.get("price", 0))
            for t in inventory
            if normalize_week(t.get("week"))
            == normalize_week(ticket.get("week"))
            and int(t.get("available_quantity", 0)) > 0
        ]
    )

    seat_score = (
        calculate_seat_quality(ticket)
        * 10
    )

    if purchased:
        # A ticket the user already owns should not be penalized because
        # it is no longer available for purchase.
        availability_score = 100
    else:
        availability_score = min(
            100,
            int(
                ticket.get(
                    "available_quantity",
                    0,
                )
            ) * 25,
        )

    score = round(
        game_score * 0.40
        + price_score * 0.25
        + seat_score * 0.20
        + availability_score * 0.15
    )

    if score >= 90:
        verdict = "🔥 Excellent Deal"

    elif score >= 80:
        verdict = "✅ Great Deal"

    elif score >= 70:
        verdict = "👍 Good Deal"

    elif score >= 60:
        verdict = "⚖️ Fair Deal"

    else:
        verdict = "💡 Could Be Better"

    return {
        "score": score,
        "verdict": verdict,
        "game": game_score,
        "price": price_score,
        "seat": seat_score,
        "availability": availability_score,
    }

def get_rate_reasons(
    ticket,
    game,
    rating,
):

    reasons = []

    price = float(
        ticket["price"]
    )

    if rating["price"] >= 80:

        reasons.append(
            f"${price:.0f}/ticket gives this ticket "
            f"a strong price score."
        )

    elif rating["price"] < 60:

        reasons.append(
            f"The ${price:.0f} price is the biggest factor "
            f"holding the rating down."
        )

    if rating["game"] >= 80:

        reasons.append(
            f"The matchup has a strong "
            f"{rating['game']}/100 game-quality score."
        )

    elif rating["game"] < 65:

        reasons.append(
            f"The matchup scores "
            f"{rating['game']}/100 for game quality."
        )

    if rating["seat"] >= 80:

        reasons.append(
            f"Section {ticket['section']} and Row {ticket['row']} "
            f"score strongly for seat quality."
        )

    elif rating["seat"] < 70:

        reasons.append(
            f"Seat quality is more average at Section "
            f"{ticket['section']}, Row {ticket['row']}."
        )

    if rating["availability"] >= 75:

        reasons.append(
            f"There are {ticket['available_quantity']} tickets "
            f"available, providing good availability."
        )

    return reasons[:3] or [
        "The ticket earns its rating from a balanced "
        "combination of price, game, seat quality, and availability."
    ]


# ============================================================
# DEAL ANALYSIS
# ============================================================

def get_deal_assessment(
    rating,
):

    score = rating["score"]

    if score >= 90:

        return (
            "Excellent deal",
            "Strong overall value across price, game quality, "
            "seat quality, and availability.",
        )

    if score >= 80:

        return (
            "Very good deal",
            "Strong value and worth serious consideration.",
        )

    if score >= 70:

        return (
            "Good deal",
            "Solid value, although another option may be "
            "stronger in one or more categories.",
        )

    if score >= 60:

        return (
            "Fair deal",
            "Reasonable value, but KickSeatz would compare "
            "alternatives before buying.",
        )

    return (
        "Could be better",
        "Not a standout value based on KickSeatz's "
        "current scoring model.",
    )


# ============================================================
# VALUE / UI HELPERS
# ============================================================

def get_price_benchmark(ticket):
    comparable = [
        float(t.get("price", 0))
        for t in inventory
        if normalize_week(t.get("week")) == normalize_week(ticket.get("week"))
        and int(t.get("available_quantity", 0)) > 0
    ]
    if not comparable:
        return None

    current = float(ticket["price"])
    median_price = statistics.median(comparable)
    cheapest = min(comparable)
    highest = max(comparable)
    below_median = sum(1 for p in comparable if current <= p) / len(comparable) * 100

    return {
        "current": current,
        "median": median_price,
        "cheapest": cheapest,
        "highest": highest,
        "sample_size": len(comparable),
        "percentile": round(below_median),
        "difference": current - median_price,
    }


def get_budget_insights(candidates, recommended_ticket, budget):
    current_price = float(recommended_ticket["price"])
    cheaper = [
        c for c in candidates
        if float(c["ticket"]["price"]) < current_price
    ]
    upgrades = [
        c for c in candidates
        if float(c["ticket"]["price"]) > current_price
        and float(c["ticket"]["price"]) <= float(budget)
    ]

    cheaper_option = min(
        cheaper,
        key=lambda c: float(c["ticket"]["price"]),
        default=None,
    )
    upgrade = min(
        upgrades,
        key=lambda c: float(c["ticket"]["price"]),
        default=None,
    )
    return cheaper_option, upgrade


def build_candidate_csv(candidates, limit=15):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Rank",
        "Week",
        "Opponent",
        "Game Date",
        "Section",
        "Row",
        "Price Per Ticket",
        "Tickets Available",
        "KickSeatz Score",
        "Opportunity Score",
        "Opportunity Label",
        "Game Quality",
        "Price Score",
        "Seat Quality",
        "Availability",
        "Inventory Source",
        "Inventory Last Updated",
        "Ticketmaster Link",
    ])

    for rank, candidate in enumerate(candidates[:limit], start=1):
        t = candidate["ticket"]
        g = candidate["game"]
        rating = rate_ticket(t, g)
        writer.writerow([
            rank,
            g.get("week"),
            g.get("opponent"),
            g.get("game_date"),
            t.get("section"),
            t.get("row"),
            round(float(t.get("price", 0)), 2),
            t.get("available_quantity"),
            candidate["score"],
            candidate.get("opportunity", {}).get("score", ""),
            candidate.get("opportunity", {}).get("label", ""),
            rating["game"],
            rating["price"],
            rating["seat"],
            rating["availability"],
            t.get("source", ""),
            t.get("last_updated", ""),
            g.get("ticketmaster_url", ""),
        ])

    return output.getvalue()


def get_ticketmaster_status_text(game):
    code = str(game.get("ticketmaster_status", "")).upper()
    if code == "ONSALE":
        return "Ticketmaster event found • on sale"
    if code:
        return f"Ticketmaster event found • status: {code}"
    if game.get("ticketmaster_event_id"):
        return "Ticketmaster event found"
    return "Ticketmaster event link not available"


def get_data_freshness():
    """Return simple, honest freshness information for the MVP data sources."""
    db_modified = None
    try:
        db_modified = datetime.fromtimestamp(
            os.path.getmtime(DB_PATH)
        )
    except (OSError, ValueError):
        pass

    inventory_updates = []
    for t in inventory:
        raw = t.get("last_updated")
        if not raw:
            continue
        try:
            inventory_updates.append(
                datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
            )
        except (ValueError, TypeError):
            continue

    latest_inventory = max(inventory_updates) if inventory_updates else db_modified
    return latest_inventory



# ============================================================
# ADVANCED FILTERS / PERSONALIZATION
# ============================================================

def get_section_number(section):
    digits = "".join(
        character
        for character in str(section or "")
        if character.isdigit()
    )

    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def get_seat_area(section):
    section_number = get_section_number(section)

    if section_number is None:
        return "Other"

    if 101 <= section_number <= 134:
        return "Lower Bowl"

    if 201 <= section_number <= 299:
        return "Upper Bowl"

    if section_number >= 300:
        return "Upper Bowl"

    return "Other"


def normalize_weights(values):
    total = sum(max(0, float(value)) for value in values)

    if total <= 0:
        return {
            "game": 0.25,
            "price": 0.25,
            "seat": 0.25,
            "availability": 0.25,
        }

    return {
        "game": max(0, float(values[0])) / total,
        "price": max(0, float(values[1])) / total,
        "seat": max(0, float(values[2])) / total,
        "availability": max(0, float(values[3])) / total,
    }


def get_schematic_seat_map_svg(ticket):
    section = str(ticket.get("section", "Unknown"))
    area = get_seat_area(section)

    section_number = get_section_number(section)

    if area == "Lower Bowl":
        highlight = "LOWER BOWL"
        subtitle = "Approximate lower-bowl placement"
    elif area == "Upper Bowl":
        highlight = "UPPER BOWL"
        subtitle = "Approximate upper-bowl placement"
    else:
        highlight = "OTHER"
        subtitle = "Approximate seating-zone placement"

    circles = [
        (110, 110, "Upper"),
        (150, 70, "Upper"),
        (190, 110, "Upper"),
        (210, 160, "Upper"),
        (190, 210, "Upper"),
        (150, 250, "Upper"),
        (110, 210, "Upper"),
        (90, 160, "Upper"),
    ]

    # Emphasize the recommended zone without pretending the schematic is an
    # exact real-world stadium map.
    lower_opacity = "1" if area == "Lower Bowl" else ".28"
    upper_opacity = "1" if area == "Upper Bowl" else ".28"

    return f"""
    <div class="seat-map-card">
        <div style="text-align:center;font-weight:800;margin-bottom:3px;">
            🗺️ Seat Zone Preview
        </div>
        <div style="text-align:center;color:#64748b;font-size:12px;margin-bottom:8px;">
            Schematic only — not to scale
        </div>
        <svg viewBox="0 0 300 320" width="100%" role="img"
             aria-label="Schematic seating zone preview for Section {section}">
            <rect x="115" y="125" width="70" height="70" rx="16"
                  fill="#111111" opacity=".92"/>
            <text x="150" y="157" text-anchor="middle"
                  fill="white" font-size="11" font-weight="700">
                FIELD
            </text>
            <text x="150" y="174" text-anchor="middle"
                  fill="white" font-size="9">
                HOME VENUE
            </text>

            <circle cx="150" cy="160" r="118"
                    fill="none" stroke="#2563eb"
                    stroke-width="25" opacity="{lower_opacity}"/>
            <circle cx="150" cy="160" r="85"
                    fill="none" stroke="#111111"
                    stroke-width="18" opacity="{upper_opacity}"/>

            {"".join(
                f'<circle cx="{x}" cy="{y}" r="8" fill="#2563eb" opacity=".75"/>'
                for x, y, _ in circles
            )}

            <rect x="19" y="276" width="262" height="28" rx="14"
                  fill="#f1f5f9"/>
            <text x="150" y="294" text-anchor="middle"
                  fill="#111111" font-size="11" font-weight="700">
                Section {section} • {highlight}
            </text>
        </svg>
        <div style="text-align:center;color:#64748b;font-size:12px;">
            {subtitle}{f" • Section {section_number}" if section_number else ""}
        </div>
    </div>
    """


def render_interactive_stadium_map(ticket, game, ticket_count=1):
    """Render an interactive demo section map for any NFL matchup.

    The map is intentionally schematic: it is a product prototype based on
    the synthetic section blueprint, not a claim about exact real-world seat
    geometry or live availability.
    """
    game_week = normalize_week(ticket.get("week") if ticket else game.get("week"))
    game_team = _nfl_full_team_name(game.get("team"))
    game_opponent = _nfl_full_team_name(game.get("opponent"))

    section_inventory = [
        item for item in inventory
        if normalize_week(item.get("week")) == game_week
        and int(item.get("available_quantity", 0)) >= int(ticket_count)
        and _nfl_full_team_name(item.get("team")) == game_team
        and _nfl_full_team_name(item.get("opponent")) == game_opponent
    ]

    if not section_inventory:
        st.info("No demo section inventory is available for this matchup yet.")
        return

    section_names = sorted(
        {str(item.get("section")) for item in section_inventory},
        key=lambda value: int("".join(ch for ch in value if ch.isdigit()) or 9999),
    )

    current_section = str(st.session_state.get("kz_map_section", ticket.get("section", "") if ticket else ""))
    if current_section not in section_names:
        current_section = section_names[0]
        st.session_state["kz_map_section"] = current_section

    st.markdown("### 🗺️ Choose a section")
    st.caption(
        f"Interactive demo seating for {game.get('venue', 'this venue')} • "
        "section shapes are schematic and inventory is synthetic."
    )
    st.markdown(
        '<div class="interactive-map-field">🏈 FIELD / PLAYING SURFACE</div>',
        unsafe_allow_html=True,
    )

    levels = {"Lower Bowl": [], "Upper Bowl": []}
    for section_name in section_names:
        area = get_seat_area(section_name)
        levels.setdefault(area if area in levels else "Upper Bowl", []).append(section_name)

    for level_name, sections in levels.items():
        if not sections:
            continue
        st.markdown(f"**{level_name}**")
        columns = st.columns(min(6, max(1, len(sections))))
        for index, section_name in enumerate(sections):
            with columns[index % len(columns)]:
                if st.button(
                    section_name,
                    key=f"kz_map_{game.get('game_id')}_{level_name}_{section_name}",
                    use_container_width=True,
                    type="primary" if current_section == section_name else "secondary",
                ):
                    st.session_state["kz_map_section"] = section_name
                    st.session_state.pop("kz_selected_ticket_id", None)
                    st.rerun()

    selected_rows = [
        item for item in section_inventory
        if str(item.get("section")) == current_section
    ]

    st.markdown(f"**Section {current_section}**")
    st.caption(f"{len(selected_rows)} synthetic listing(s) in this section")
    for option_index, section_ticket in enumerate(selected_rows[:6], start=1):
        cols = st.columns([2.2, 1, 1, 1])
        with cols[0]:
            st.write(f"Section {section_ticket.get('section')} • Row {section_ticket.get('row')}")
        with cols[1]:
            st.markdown(f"**${float(section_ticket.get('price', 0)):.0f}**")
        with cols[2]:
            st.caption(f"{int(section_ticket.get('available_quantity', 0))} available")
        with cols[3]:
            if st.button("Select", key=f"kz_map_select_{section_ticket.get('id')}_{option_index}", use_container_width=True):
                st.session_state["kz_selected_ticket_id"] = section_ticket.get("id")
                st.rerun()


def get_price_history_for_ticket(ticket_id):
    history_conn = sqlite3.connect(DB_PATH)

    try:
        return history_conn.execute(
            """
            SELECT price, recorded_at
            FROM price_history
            WHERE ticket_id = ?
            ORDER BY recorded_at
            """,
            (ticket_id,),
        ).fetchall()
    finally:
        history_conn.close()


def ensure_price_watch_table():
    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_watchlist (
                ticket_id INTEGER PRIMARY KEY,
                target_price REAL NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def set_price_watch(ticket_id, target_price):
    ensure_price_watch_table()
    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute(
            """
            INSERT INTO price_watchlist (
                ticket_id,
                target_price,
                created_at,
                active
            )
            VALUES (?, ?, ?, 1)
            ON CONFLICT(ticket_id) DO UPDATE SET
                target_price = excluded.target_price,
                created_at = excluded.created_at,
                active = 1
            """,
            (
                int(ticket_id),
                float(target_price),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def remove_price_watch(ticket_id):
    ensure_price_watch_table()
    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute(
            """
            DELETE FROM price_watchlist
            WHERE ticket_id = ?
            """,
            (int(ticket_id),),
        )
        conn.commit()
    finally:
        conn.close()


def get_price_watch(ticket_id):
    ensure_price_watch_table()
    conn = sqlite3.connect(DB_PATH)

    try:
        row = conn.execute(
            """
            SELECT ticket_id, target_price, created_at, active
            FROM price_watchlist
            WHERE ticket_id = ?
            """,
            (int(ticket_id),),
        ).fetchone()

        return row
    finally:
        conn.close()


def get_all_price_watches():
    ensure_price_watch_table()
    conn = sqlite3.connect(DB_PATH)

    try:
        return conn.execute(
            """
            SELECT ticket_id, target_price, created_at, active
            FROM price_watchlist
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()


def get_price_timing_snapshot(ticket):
    history_rows = get_price_history_for_ticket(ticket["id"])
    prices = [float(row[0]) for row in history_rows]

    if not prices:
        return {
            "label": "No history yet",
            "detail": "KickSeatz needs more price snapshots before showing a historical trend.",
            "change": None,
            "low": None,
        }

    current = prices[-1]
    lowest = min(prices)

    if len(prices) < 2:
        return {
            "label": "Building history",
            "detail": f"Current price is ${current:.0f}; only one snapshot is available so far.",
            "change": None,
            "low": lowest,
        }

    previous = prices[-2]
    change = current - previous

    if change < 0:
        label = "Historical trend: falling"
    elif change > 0:
        label = "Historical trend: rising"
    else:
        label = "Historical trend: stable"

    detail = (
        f"Latest snapshot moved ${abs(change):.0f} "
        f"{'down' if change < 0 else 'up' if change > 0 else 'with no change'} "
        f"from the previous snapshot. Historical low: ${lowest:.0f}."
    )

    return {
        "label": label,
        "detail": detail,
        "change": change,
        "low": lowest,
    }


def get_price_timing_advice(ticket):
    """Summarize historical price position without forecasting future prices."""
    history_rows = get_price_history_for_ticket(ticket["id"])
    prices = [float(row[0]) for row in history_rows]

    if len(prices) < 2:
        return {
            "headline": "⏳ Not enough history yet",
            "detail": (
                "KickSeatz does not have enough recorded snapshots to make a useful "
                "historical timing assessment."
            ),
            "tone": "info",
        }

    current = prices[-1]
    previous = prices[-2]
    low = min(prices)
    high = max(prices)
    median = statistics.median(prices)

    distance_from_low = ((current - low) / low * 100) if low > 0 else 0
    distance_from_median = ((current - median) / median * 100) if median > 0 else 0

    if current <= low:
        headline = "🟢 At the lowest recorded price"
        detail = (
            f"The current price of ${current:.0f} matches the lowest price "
            f"KickSeatz has recorded for this ticket."
        )
        tone = "success"
    elif current <= low * 1.05:
        headline = "🟢 Near the lowest recorded price"
        detail = (
            f"The current price of ${current:.0f} is only "
            f"{distance_from_low:.1f}% above the observed low of ${low:.0f}."
        )
        tone = "success"
    elif current < median and current <= previous:
        headline = "🟢 Historically favorable"
        detail = (
            f"The current price of ${current:.0f} is below the recorded median "
            f"of ${median:.0f} and did not rise in the latest snapshot."
        )
        tone = "success"
    elif current > median * 1.10:
        headline = "🟡 Historically higher"
        detail = (
            f"The current price of ${current:.0f} is {max(0, distance_from_median):.1f}% "
            f"above the recorded median of ${median:.0f}."
        )
        tone = "warning"
    else:
        headline = "🔵 Mixed historical signal"
        detail = (
            f"The current price of ${current:.0f} sits between the observed low "
            f"(${low:.0f}) and high (${high:.0f}); KickSeatz does not predict future prices."
        )
        tone = "info"

    return {
        "headline": headline,
        "detail": detail,
        "tone": tone,
    }


def get_watch_status(ticket):
    watch = get_price_watch(ticket["id"])

    if not watch:
        return None

    target_price = float(watch[1])
    current_price = float(ticket["price"])

    return {
        "target": target_price,
        "current": current_price,
        "triggered": current_price <= target_price,
        "created_at": watch[2],
    }


def get_last_updated_display(value):
    if not value:
        return "Timestamp unavailable"

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).replace(tzinfo=None)

        return parsed.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return str(value)


def clear_app_cache_and_rerun():
    st.cache_data.clear()
    st.rerun()


# ============================================================
# KICKSEATZ PROFILE / PERSISTENT PREFERENCES
# ============================================================

NFL_PROFILE_TEAMS = [
    "Arizona Cardinals",
    "Atlanta Falcons",
    "Baltimore Ravens",
    "Buffalo Bills",
    "Carolina Panthers",
    "Chicago Bears",
    "Cincinnati Bengals",
    "Cleveland Browns",
    "Dallas Cowboys",
    "Denver Broncos",
    "Detroit Lions",
    "Green Bay Packers",
    "Houston Texans",
    "Indianapolis Colts",
    "Jacksonville Jaguars",
    "Kansas City Chiefs",
    "Las Vegas Raiders",
    "Los Angeles Chargers",
    "Los Angeles Rams",
    "Miami Dolphins",
    "Minnesota Vikings",
    "New England Patriots",
    "New Orleans Saints",
    "New York Giants",
    "New York Jets",
    "Philadelphia Eagles",
    "Pittsburgh Steelers",
    "San Francisco 49ers",
    "Seattle Seahawks",
    "Tampa Bay Buccaneers",
    "Tennessee Titans",
    "Washington Commanders",
]

PROFILE_DEFAULTS = {
    "favorite_team": "",
    "favorite_opponents": [],
    "location": "",
    "travel_radius": 50,
    "home_away": "Either",
    "seat_area": "No preference",
    "typical_budget": 100.0,
    "ticket_count": 2,
    "fan_type": "Casual / social fan",
    "experience": "No strong preference",
    "priority": "Best Overall Value",
}


def get_profile_identity():
    """
    Return a stable identity when Streamlit authentication is configured.

    Guests intentionally use session state only. This avoids treating a
    shared SQLite database as a secure customer account system before the
    production website has a real authentication/database layer.
    """
    try:
        user = st.user
        if getattr(user, "is_logged_in", False):
            for attribute in ("sub", "email", "username", "name"):
                value = getattr(user, attribute, None)
                if value:
                    return f"user:{str(value).strip().lower()}"
    except Exception:
        pass

    return None


def profile_auth_configured():
    try:
        auth_config = st.secrets.get("auth", None)
        return bool(auth_config)
    except Exception:
        return False


def init_profile_store():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                profile_id TEXT PRIMARY KEY,
                favorite_team TEXT,
                favorite_opponents TEXT,
                location TEXT,
                travel_radius INTEGER,
                home_away TEXT,
                seat_area TEXT,
                typical_budget REAL,
                ticket_count INTEGER,
                fan_type TEXT,
                experience TEXT,
                priority TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def load_saved_profile():
    if "kz_profile_preferences" in st.session_state:
        return dict(st.session_state["kz_profile_preferences"])

    profile_id = get_profile_identity()
    if not profile_id:
        return dict(PROFILE_DEFAULTS)

    init_profile_store()
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT
                favorite_team,
                favorite_opponents,
                location,
                travel_radius,
                home_away,
                seat_area,
                typical_budget,
                ticket_count,
                fan_type,
                experience,
                priority
            FROM user_preferences
            WHERE profile_id = ?
            """,
            (profile_id,),
        ).fetchone()
    finally:
        conn.close()

    preferences = dict(PROFILE_DEFAULTS)

    if row:
        try:
            preferences.update(
                {
                    "favorite_team": row[0] or PROFILE_DEFAULTS["favorite_team"],
                    "favorite_opponents": json.loads(row[1] or "[]"),
                    "location": row[2] or "",
                    "travel_radius": int(row[3] or 50),
                    "home_away": row[4] or "Either",
                    "seat_area": row[5] or "No preference",
                    "typical_budget": float(row[6] or 100),
                    "ticket_count": int(row[7] or 2),
                    "fan_type": row[8] or PROFILE_DEFAULTS["fan_type"],
                    "experience": row[9] or PROFILE_DEFAULTS["experience"],
                    "priority": row[10] or PROFILE_DEFAULTS["priority"],
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            preferences = dict(PROFILE_DEFAULTS)

    st.session_state["kz_profile_preferences"] = dict(preferences)
    return preferences


def save_profile_preferences(preferences):
    clean_preferences = dict(PROFILE_DEFAULTS)
    clean_preferences.update(preferences)

    favorite_opponents = [
        str(team).strip()
        for team in clean_preferences.get("favorite_opponents", [])
        if str(team).strip()
    ]
    clean_preferences["favorite_opponents"] = favorite_opponents

    st.session_state["kz_profile_preferences"] = dict(clean_preferences)

    profile_id = get_profile_identity()
    if not profile_id:
        return False

    init_profile_store()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO user_preferences (
                profile_id,
                favorite_team,
                favorite_opponents,
                location,
                travel_radius,
                home_away,
                seat_area,
                typical_budget,
                ticket_count,
                fan_type,
                experience,
                priority,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                favorite_team = excluded.favorite_team,
                favorite_opponents = excluded.favorite_opponents,
                location = excluded.location,
                travel_radius = excluded.travel_radius,
                home_away = excluded.home_away,
                seat_area = excluded.seat_area,
                typical_budget = excluded.typical_budget,
                ticket_count = excluded.ticket_count,
                fan_type = excluded.fan_type,
                experience = excluded.experience,
                priority = excluded.priority,
                updated_at = excluded.updated_at
            """,
            (
                profile_id,
                clean_preferences["favorite_team"],
                json.dumps(favorite_opponents),
                clean_preferences["location"],
                int(clean_preferences["travel_radius"]),
                clean_preferences["home_away"],
                clean_preferences["seat_area"],
                float(clean_preferences["typical_budget"]),
                int(clean_preferences["ticket_count"]),
                clean_preferences["fan_type"],
                clean_preferences["experience"],
                clean_preferences["priority"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return True


def render_platform_profile():
    preferences = load_saved_profile()

    st.markdown("## 👤 My KickSeatz Profile")
    st.write(
        "Save the preferences KickSeatz should remember when finding games and tickets for you."
    )

    profile_id = get_profile_identity()
    if profile_id:
        st.success("Your preferences are linked to your signed-in profile.")
    elif profile_auth_configured():
        st.info(
            "You're using guest mode. Sign in to save your preferences across sessions."
        )
        if st.button("Sign in to save my profile", use_container_width=True, key="kz_profile_login"):
            st.login()
    else:
        st.info(
            "Guest mode: your preferences are saved for this browser session. "
            "Full account persistence can be enabled when authentication is configured."
        )

    with st.form("kz_profile_form"):
        st.markdown("### 🏈 Your NFL preferences")

        left, right = st.columns(2)

        with left:
            favorite_team = st.selectbox(
                "Favorite NFL team",
                NFL_PROFILE_TEAMS,
                index=(
                    NFL_PROFILE_TEAMS.index(preferences["favorite_team"])
                    if preferences.get("favorite_team") in NFL_PROFILE_TEAMS
                    else 0
                ),
                key="kz_profile_favorite_team",
            )

            favorite_opponents = st.multiselect(
                "Teams you'd especially like to see",
                NFL_PROFILE_TEAMS,
                default=[
                    team
                    for team in preferences.get("favorite_opponents", [])
                    if team in NFL_PROFILE_TEAMS
                ],
                key="kz_profile_favorite_opponents",
            )

            location = st.text_input(
                "Home city or ZIP code",
                value=str(preferences.get("location", "")),
                placeholder="Example: City or ZIP code",
                help="Use a city or ZIP code. KickSeatz does not need your street address.",
                key="kz_profile_location",
            )

            travel_radius = st.slider(
                "How far are you willing to travel?",
                min_value=0,
                max_value=2500,
                value=int(preferences.get("travel_radius", 50)),
                step=25,
                format="%d miles",
                key="kz_profile_radius",
            )

        with right:
            home_away = st.selectbox(
                "Game preference",
                ["Prefer home", "Either", "Prefer away"],
                index=["Prefer home", "Either", "Prefer away"].index(
                    preferences.get("home_away", "Either")
                    if preferences.get("home_away", "Either") in ["Prefer home", "Either", "Prefer away"]
                    else "Either"
                ),
                key="kz_profile_home_away",
            )

            seat_area = st.selectbox(
                "Preferred seating area",
                ["No preference", "Lower Bowl", "Upper Bowl", "Club / Premium"],
                index=["No preference", "Lower Bowl", "Upper Bowl", "Club / Premium"].index(
                    preferences.get("seat_area", "No preference")
                    if preferences.get("seat_area", "No preference") in ["No preference", "Lower Bowl", "Upper Bowl", "Club / Premium"]
                    else "No preference"
                ),
                key="kz_profile_area",
            )

            typical_budget = st.number_input(
                "Typical ticket budget",
                min_value=25.0,
                max_value=2000.0,
                value=float(preferences.get("typical_budget", 100.0)),
                step=5.0,
                key="kz_profile_budget",
            )

            ticket_count = st.selectbox(
                "Typical number of tickets",
                [1, 2, 3, 4],
                index=max(0, min(3, int(preferences.get("ticket_count", 2)) - 1)),
                key="kz_profile_count",
            )

            fan_type = st.selectbox(
                "What type of fan are you?",
                [
                    "Die-hard fan",
                    "Rivalry fan",
                    "Big matchup / star-game fan",
                    "Casual / social fan",
                    "Road-trip fan",
                ],
                index=(
                    [
                        "Die-hard fan",
                        "Rivalry fan",
                        "Big matchup / star-game fan",
                        "Casual / social fan",
                        "Road-trip fan",
                    ].index(preferences.get("fan_type"))
                    if preferences.get("fan_type") in [
                        "Die-hard fan",
                        "Rivalry fan",
                        "Big matchup / star-game fan",
                        "Casual / social fan",
                        "Road-trip fan",
                    ]
                    else 3
                ),
                key="kz_profile_fan_type",
            )

        experience = st.selectbox(
            "What kind of experience do you usually want?",
            [
                "No strong preference",
                "Rivalry atmosphere",
                "Elite opponent / marquee matchup",
                "Affordable / value-focused",
                "Home-field experience",
                "Unique travel experience",
            ],
            index=(
                [
                    "No strong preference",
                    "Rivalry atmosphere",
                    "Elite opponent / marquee matchup",
                    "Affordable / value-focused",
                    "Home-field experience",
                    "Unique travel experience",
                ].index(preferences.get("experience"))
                if preferences.get("experience") in [
                    "No strong preference",
                    "Rivalry atmosphere",
                    "Elite opponent / marquee matchup",
                    "Affordable / value-focused",
                    "Home-field experience",
                    "Unique travel experience",
                ]
                else 0
            ),
            key="kz_profile_experience",
        )

        priority = st.selectbox(
            "What should KickSeatz prioritize?",
            [
                "Best Overall Value",
                "Lowest Price",
                "Best Game",
                "Best Seats",
            ],
            index=(
                [
                    "Best Overall Value",
                    "Lowest Price",
                    "Best Game",
                    "Best Seats",
                ].index(preferences.get("priority"))
                if preferences.get("priority") in [
                    "Best Overall Value",
                    "Lowest Price",
                    "Best Game",
                    "Best Seats",
                ]
                else 0
            ),
            key="kz_profile_priority",
        )

        saved = st.form_submit_button(
            "Save My Preferences",
            use_container_width=True,
            type="primary",
        )

    if saved:
        saved_preferences = {
            "favorite_team": favorite_team,
            "favorite_opponents": favorite_opponents,
            "location": location.strip(),
            "travel_radius": travel_radius,
            "home_away": home_away,
            "seat_area": seat_area,
            "typical_budget": typical_budget,
            "ticket_count": ticket_count,
            "fan_type": fan_type,
            "experience": experience,
            "priority": priority,
        }
        persistent = save_profile_preferences(saved_preferences)
        if persistent:
            st.success("✅ Preferences saved to your KickSeatz profile.")
        else:
            st.success("✅ Preferences saved for this session.")

    saved_now = load_saved_profile()
    if saved_now:
        st.markdown("### Your saved setup")
        profile_cols = st.columns(4)
        with profile_cols[0]:
            st.metric("Favorite Team", saved_now.get("favorite_team", "—"))
        with profile_cols[1]:
            st.metric("Travel Radius", f"{saved_now.get('travel_radius', 0)} mi")
        with profile_cols[2]:
            st.metric("Typical Budget", f"${float(saved_now.get('typical_budget', 0)):.0f}")
        with profile_cols[3]:
            st.metric("Tickets", int(saved_now.get("ticket_count", 0)))

    st.caption(
        "Profile fields are designed for personalization: favorite teams, approximate location, travel range, seating preferences, fan type, experience, budget, ticket quantity, and scoring priority."
    )


# ============================================================

# ============================================================
# KICKSEATZ PLATFORM NAVIGATION / MARKETPLACE SHELL
# ============================================================

st.markdown("""
<style>

/* Consumer-style top navigation */
.kz-nav {
    position: sticky;
    top: 0;
    z-index: 999;
    padding: 8px 0 12px 0;
    margin: -8px 0 18px 0;
    background: rgba(248,250,252,.96);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid rgba(148,163,184,.20);
}

.kz-brand {
    font-size: 24px;
    font-weight: 900;
    letter-spacing: -.03em;
    color: #0f172a !important;
    margin: 0;
}

.kz-brand .kz-brand-accent {
    color: #2563eb !important;
}

.kz-subbrand {
    font-size: 12px;
    color: #64748b;
    margin: -2px 0 0 0;
}

.market-hero {
    padding: 34px 36px;
    border-radius: 24px;
    background: linear-gradient(135deg, #2563eb 0%, #171717 100%);
    color: white;
    box-shadow: 0 12px 34px rgba(15,23,42,.15);
    margin: 8px 0 24px 0;
}

.market-hero h1 {
    font-size: 46px;
    margin: 0 0 8px 0;
    font-weight: 900;
    letter-spacing: -.035em;
}

.market-hero p {
    margin: 0;
    font-size: 18px;
}

.market-card {
    border: 1px solid rgba(148,163,184,.22);
    border-radius: 20px;
    padding: 22px;
    background: white;
    box-shadow: 0 7px 24px rgba(15,23,42,.07);
    min-height: 160px;
    margin-bottom: 14px;
}

.market-card h3 {
    margin: 0 0 6px 0;
    font-size: 22px;
}

.market-card p {
    color: #64748b;
    margin: 0;
}

.market-section-label {
    font-size: 13px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #64748b;
    margin: 6px 0 10px 0;
}

.market-ticket-card {
    border: 1px solid rgba(148,163,184,.20);
    border-radius: 18px;
    padding: 18px;
    background: white;
    box-shadow: 0 5px 18px rgba(15,23,42,.06);
    margin-bottom: 12px;
}

.market-ticket-card .market-price {
    font-size: 30px;
    font-weight: 900;
    color: #111827;
}

.market-ticket-card .market-score {
    font-size: 18px;
    font-weight: 800;
    color: #2563eb;
}

@media (max-width: 768px) {
    .market-hero {
        padding: 24px 20px;
        border-radius: 18px;
    }

    .market-hero h1 {
        font-size: 34px;
    }
}

</style>
""", unsafe_allow_html=True)


def _kz_navigate(page_name):
    st.session_state["kz_page"] = page_name
    st.rerun()


def render_platform_nav():
    current_page = st.session_state.get("kz_page", "home")

    st.markdown(
        '<div class="kz-nav">'
        '<div class="kz-brand">🏟️ Kick<span class="kz-brand-accent">Seatz</span></div>'
        '<div class="kz-subbrand">NFL tickets, simplified</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    items = [
        ("🏠 Home", "home"),
        ("🎯 Find My Game", "find_game"),
        ("🎟️ Find Tickets", "find_tickets"),
        ("🧾 My Tickets", "my_tickets"),
        ("👤 Profile", "profile"),
    ]
    nav = st.columns(len(items))

    for column, (label, page_name) in zip(nav, items):
        with column:
            st.button(
                label,
                key=f"kz_nav_{page_name}",
                use_container_width=True,
                type="primary" if current_page == page_name else "secondary",
                on_click=_kz_navigate,
                args=(page_name,),
            )




def _profile_travel_preference(preferences=None):
    """Convert a saved mileage radius into the Find My Game travel choice."""
    preferences = preferences or {}
    try:
        radius = int(preferences.get("travel_radius", 50))
    except (TypeError, ValueError):
        radius = 50

    if radius <= 0:
        return "Home area"
    if radius <= 500:
        return "Up to 500 miles"
    if radius <= 1000:
        return "Up to 1,000 miles"
    if radius <= 3000:
        return "Anywhere in the U.S."
    return "Anywhere, including international"


def _profile_home_away_preference(preferences=None):
    """Translate saved profile wording into the game matcher wording."""
    value = str((preferences or {}).get("home_away", "Either"))
    if value == "Prefer home":
        return "Prefer home games"
    if value == "Prefer away":
        return "Prefer away games"
    return "Either"


def _profile_experience_preference(preferences=None):
    """Choose the best matching Find My Game experience from the saved profile."""
    preferences = preferences or {}
    experience = str(preferences.get("experience", "No strong preference"))
    direct_map = {
        "Rivalry atmosphere": "Rivalry atmosphere",
        "Elite opponent / marquee matchup": "Elite opponent / marquee matchup",
        "Affordable / value-focused": "Affordable / value-focused",
        "Home-field experience": "Home-field experience",
        "Unique travel experience": "Unique travel experience",
    }
    if experience in direct_map:
        return direct_map[experience]

    fan_type = str(preferences.get("fan_type", "Casual / social fan"))
    fan_map = {
        "Die-hard fan": "Home-field experience",
        "Rivalry fan": "Rivalry atmosphere",
        "Big matchup / star-game fan": "Elite opponent / marquee matchup",
        "Road-trip fan": "Unique travel experience",
        "Casual / social fan": "Affordable / value-focused",
    }
    return fan_map.get(fan_type, "Elite opponent / marquee matchup")


def _profile_state_hint(location):
    """Return a normalized US state abbreviation when a city/ZIP profile gives one."""
    text = str(location or "").strip().lower()
    if not text:
        return ""

    state_map = {
        "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
        "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
        "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
        "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
        "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
        "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
        "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
        "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
        "new york": "ny", "north carolina": "nc", "north dakota": "nd",
        "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
        "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
        "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
        "virginia": "va", "washington": "wa", "west virginia": "wv",
        "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
    }
    for state_name, abbreviation in state_map.items():
        if state_name in text:
            return abbreviation

    import re as _re
    matches = _re.findall(r"\b([a-z]{2})\b", text)
    valid = set(state_map.values())
    for token in matches:
        if token in valid:
            return token
    return ""


def _profile_location_affinity(game, location):
    """Give a modest boost when a saved location shares the game's home state."""
    state = _profile_state_hint(location)
    if not state:
        return 0

    game_location = str(game.get("location") or "").lower()
    venue_info = NFL_TEAM_VENUES.get(game.get("home_team"), {})
    venue_location = str(venue_info.get("location") or "").lower()
    combined = f"{game_location} {venue_location}"
    import re as _re
    states_in_game = set(_re.findall(r"\b([a-z]{2})\b", combined))
    return 10 if state in states_in_game else 0


def calculate_profile_game_bonus(game, preferences):
    """Calculate an extra personalization signal from the saved profile."""
    if not preferences:
        return 0, []

    bonus = 0
    reasons = []
    favorite_team = str(preferences.get("favorite_team") or "").strip()
    favorite_opponents = set(preferences.get("favorite_opponents") or [])

    if favorite_team and game.get("team") == favorite_team:
        bonus += 12
    opponent = game.get("opponent") or ""
    if opponent in favorite_opponents:
        bonus += 8
        reasons.append("Matches one of your saved favorite opponents.")

    location_bonus = _profile_location_affinity(game, preferences.get("location"))
    if location_bonus:
        bonus += location_bonus
        reasons.append("The game's venue is in your saved home state.")

    return min(30, bonus), reasons[:2]


def calculate_profile_ticket_match(ticket, game, preferences):
    """Score how closely an individual ticket matches saved profile preferences."""
    if not preferences:
        return 50

    score = 50.0
    favorite_team = preferences.get("favorite_team")
    favorite_opponents = set(preferences.get("favorite_opponents") or [])

    if favorite_team and game.get("team") == favorite_team:
        score += 12
    if game.get("opponent") in favorite_opponents:
        score += 8

    saved_area = str(preferences.get("seat_area") or "No preference")
    if saved_area != "No preference":
        ticket_area = get_seat_area(ticket.get("section"))
        if saved_area == ticket_area:
            score += 18
        elif saved_area == "Club / Premium" and ticket_area == "Lower Bowl":
            score += 6

    saved_home_away = str(preferences.get("home_away") or "Either")
    if saved_home_away == "Prefer home" and game.get("home_game"):
        score += 6
    elif saved_home_away == "Prefer away" and not game.get("home_game") and not game.get("neutral_site"):
        score += 6

    return round(max(0, min(100, score)))


def _get_saved_favorite_team():
    saved = load_saved_profile()
    return (saved or {}).get("favorite_team") or ""

def _get_ticket_inventory_for_game(team, game, budget=None, ticket_count=1):
    """Return demo/live listings for a physical NFL matchup."""
    requested_team = _nfl_full_team_name(team) if team and team != "All NFL" else "All NFL"
    week = normalize_week(game.get("week"))
    game_team = _nfl_full_team_name(game.get("team"))
    game_opponent = _nfl_full_team_name(game.get("opponent"))
    allowed_pairs = {
        (game_team, game_opponent),
        (game_opponent, game_team),
    }

    matches = []
    seen_physical = set()
    for ticket in inventory:
        ticket_team = _nfl_full_team_name(ticket.get("team") or "Unknown")
        ticket_opponent = _nfl_full_team_name(ticket.get("opponent"))
        if normalize_week(ticket.get("week")) != week:
            continue

        if requested_team != "All NFL":
            if ticket_team != requested_team:
                continue
            if str(ticket.get("opponent") or "").strip().lower() != str(game.get("opponent") or "").strip().lower():
                continue
        else:
            if (ticket_team, ticket_opponent) not in allowed_pairs:
                continue

        if budget is not None and float(ticket.get("price", 0)) > float(budget):
            continue
        if int(ticket.get("available_quantity", 0)) < int(ticket_count):
            continue

        physical_key = (
            week,
            min(ticket_team.lower(), ticket_opponent.lower()),
            max(ticket_team.lower(), ticket_opponent.lower()),
            str(ticket.get("section") or "").strip().lower(),
            str(ticket.get("row") or "").strip().lower(),
            round(float(ticket.get("price", 0)), 2),
        )
        if requested_team == "All NFL" and physical_key in seen_physical:
            continue
        seen_physical.add(physical_key)
        matches.append(ticket)

    return matches

def calculate_travel_fit(game, travel_preference):
    """Estimate travel fit without assuming one NFL team's home city."""
    preference = str(travel_preference or "Anywhere, including international")
    is_home = bool(game.get("home_game"))
    is_international = bool(game.get("international_game") or game.get("neutral_site"))

    limits = {
        "Home area": 0,
        "Up to 500 miles": 500,
        "Up to 1,000 miles": 1000,
        "Anywhere in the U.S.": 3000,
        "Anywhere, including international": 10000,
    }
    limit = limits.get(preference, 10000)

    if is_home:
        miles = 0
    elif is_international:
        miles = 4500
    else:
        # The schedule layer may provide a better estimate; otherwise use a
        # neutral domestic away-game estimate instead of hard-coding Atlanta.
        miles = float(game.get("travel_miles") or 1000)

    if miles <= limit:
        return 100
    if limit == 0:
        return 0
    excess_ratio = (miles - limit) / max(miles, 1)
    return round(max(0, 100 - excess_ratio * 100))


def _generic_game_match_score(game, favorite_team, favorite_opponents, home_away, vibe, travel="Anywhere, including international"):
    score = 55.0
    reasons = []
    opponent = game.get("opponent") or ""
    if favorite_team and game.get("team") == favorite_team:
        score += 30
        reasons.append(f"Matches your favorite team: {favorite_team}.")
    if opponent in favorite_opponents:
        score += 18
        reasons.append(f"Matches a favorite opponent: {opponent}.")
    if home_away == "Prefer home games":
        score += 10 if game.get("home_game") else -8
    elif home_away == "Prefer away games":
        score += 10 if not game.get("home_game") and not game.get("neutral_site") else -8
    else:
        score += 4

    travel_fit = calculate_travel_fit(game, travel)
    score += (travel_fit - 70) * 0.22
    if travel_fit >= 82:
        reasons.append("The travel profile fits the range you selected.")
    if vibe == "Home-field experience" and game.get("home_game"):
        score += 10
    elif vibe == "Unique travel experience" and (game.get("international_game") or not game.get("home_game")):
        score += 14
    elif vibe == "Rivalry atmosphere" and NFL_TEAM_DIVISIONS.get(game.get("team")) == NFL_TEAM_DIVISIONS.get(opponent):
        score += 18
        reasons.append("The matchup is a same-division game.")
    elif vibe == "Elite opponent / marquee matchup" and opponent in {
        "Kansas City Chiefs", "Baltimore Ravens", "Buffalo Bills", "Philadelphia Eagles",
        "Dallas Cowboys", "San Francisco 49ers", "Los Angeles Rams",
    }:
        score += 12
        reasons.append("The opponent has strong national matchup appeal.")
    if game.get("international_game"):
        reasons.append("This game is part of the NFL international schedule.")
    return max(0, min(100, round(score))), reasons

def get_generic_nfl_game_recommendations(preferences):
    saved_profile = load_saved_profile() or {}
    profile = dict(saved_profile)
    profile.update({key: value for key, value in (preferences or {}).items() if value not in (None, "")})

    favorite_team = profile.get("team") or profile.get("favorite_team") or _get_saved_favorite_team()
    if favorite_team:
        games = get_team_schedule(favorite_team)
    else:
        games = []
        seen_games = set()
        for raw_game in get_nfl_schedule_games():
            key = str(raw_game.get("game_id") or "").strip()
            if not key:
                home = raw_game.get("home_team") or raw_game.get("team")
                away = raw_game.get("away_team") or raw_game.get("opponent")
                key = f"{raw_game.get('week')}|{home}|{away}"
            if key in seen_games:
                continue
            seen_games.add(key)
            if raw_game.get("away_team") and raw_game.get("home_team"):
                game = dict(raw_game)
                game.update({
                    "team": _nfl_full_team_name(raw_game.get("home_team")),
                    "opponent": _nfl_full_team_name(raw_game.get("away_team")),
                    "home_game": True,
                    "selected_team_home": True,
                    "matchup": f"{raw_game.get('home_team')} vs {raw_game.get('away_team')}",
                })
                games.append(game)
            else:
                games.append(dict(raw_game))
    favorite_opponents = profile.get("favorite_opponents") or []
    home_away = profile.get("home_away", _profile_home_away_preference(profile))
    if home_away in {"Prefer home", "Prefer away"}:
        home_away = _profile_home_away_preference(profile)
    vibe = profile.get("vibe") or _profile_experience_preference(profile)
    budget = float(profile.get("budget") or profile.get("typical_budget") or 200)
    ticket_count = int(profile.get("ticket_count") or 1)
    travel_preference = profile.get("travel") or _profile_travel_preference(profile)
    today = datetime.now().strftime("%Y-%m-%d")
    games = [g for g in games if not g.get("game_date") or str(g.get("game_date")) >= today]
    results = []
    for game in games:
        score, reasons = _generic_game_match_score(
            game, favorite_team, favorite_opponents, home_away, vibe,
            travel_preference,
        )
        profile_bonus, profile_reasons = calculate_profile_game_bonus(game, profile)
        score = min(100, score + profile_bonus)
        reasons.extend(profile_reasons)
        demos = _get_ticket_inventory_for_game(game.get("team"), game, budget=budget, ticket_count=ticket_count)
        if demos and vibe == "Affordable / value-focused":
            score = min(100, score + 8)
            reasons.append("Connected ticket inventory fits your current budget.")
        results.append({"game": game, "score": min(100, round(score)), "reasons": reasons[:3] or ["This game matches your selected preference mix."]})
    results.sort(key=lambda x: (x["score"], str(x["game"].get("game_date") or "9999")), reverse=True)
    return results

def _open_nfl_ticket_search(game):
    st.session_state["kz_ticket_team"] = game.get("team")
    st.session_state["kz_ticket_game_id"] = game.get("game_id")
    _kz_navigate("find_tickets")


def render_platform_home():
    favorite_team = _get_saved_favorite_team()
    focus_team = favorite_team if favorite_team in NFL_TEAM_NAMES else "All NFL"
    st.markdown("""
    <div class="market-hero">
        <h1>Find your next NFL game.</h1>
        <p>Discover NFL matchups, compare connected tickets, save preferences, and keep the games you care about in one place.</p>
    </div>
    """, unsafe_allow_html=True)
    st.markdown('<div class="market-section-label">What are you looking for?</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    c3, c4 = st.columns(2)
    cards = [
        (c1, "🎯", "Find My Game", "Match upcoming NFL games to your team, travel, seats, budget, and experience.", "find_game"),
        (c2, "🎟️", "Find Tickets", "Choose any NFL team and matchup, then see connected ticket inventory.", "find_tickets"),
        (c3, "🧾", "My Tickets", "Rate a ticket, review listings, and manage your saved price watches.", "my_tickets"),
        (c4, "👤", "My Profile", "Save your favorite team, location, travel range, seating preferences, and budget.", "profile"),
    ]
    for i, (column, icon, title, description, target) in enumerate(cards, start=1):
        with column:
            st.markdown(f'<div class="market-card"><h3>{icon} {title}</h3><p>{description}</p></div>', unsafe_allow_html=True)
            st.button(title, key=f"home_card_{i}_{target}", use_container_width=True, on_click=_kz_navigate, args=(target,))

    st.markdown('<div class="market-section-label">Upcoming NFL games</div>', unsafe_allow_html=True)
    view_options = ["All NFL"] + NFL_TEAM_NAMES
    schedule_view = st.selectbox("Show upcoming games for", view_options, index=view_options.index(focus_team), key="kz_home_schedule_view")
    upcoming = get_upcoming_nfl_games(None if schedule_view == "All NFL" else schedule_view, limit=10)
    if not upcoming:
        st.info("The NFL schedule feed is temporarily unavailable. Refresh the page and try again.")
    for start_i in range(0, len(upcoming), 2):
        pair = upcoming[start_i:start_i+2]
        cols = st.columns(len(pair))
        for col, game in zip(cols, pair):
            with col:
                st.markdown(render_nfl_matchup_card(game), unsafe_allow_html=True)
                st.button(
                    "Find tickets for this game",
                    key=f"home_game_ticket_{start_i}_{game.get('game_id')}_{game.get('team')}",
                    use_container_width=True,
                    on_click=_open_nfl_ticket_search,
                    args=(game,),
                )

    st.markdown('<div class="market-section-label">NFL team directory</div>', unsafe_allow_html=True)
    team_choice = st.selectbox("Explore a team", NFL_TEAM_NAMES, index=(NFL_TEAM_NAMES.index(favorite_team) if favorite_team in NFL_TEAM_NAMES else 0), key="kz_home_team_explorer")
    team_info = get_team_venue_info(team_choice) or {}
    explorer_games = get_upcoming_nfl_games(team_choice, limit=3)
    with st.container(border=True):
        left, right = st.columns([1.3, 2])
        with left:
            logo = get_nfl_logo_url(team_choice)
            if logo:
                st.markdown(f'<img src="{logo}" width="76" height="76" loading="lazy" decoding="async" alt="{team_choice} logo">', unsafe_allow_html=True)
            st.markdown(f"### {team_choice}")
            st.caption(f"{NFL_TEAM_DIVISIONS.get(team_choice, 'NFL')} • {team_info.get('venue', 'Venue TBD')}")
            st.caption(team_info.get('location', 'Location TBD'))
        with right:
            st.write("**Next games**")
            for game in explorer_games:
                st.write(f"• {_format_nfl_matchup(game)} — {game.get('game_date') or 'Date TBD'}")
    st.caption("32 NFL teams • league-wide schedule discovery • synthetic demo ticket availability • Ticketmaster Discovery metadata when available")

def render_platform_find_game():
    saved = load_saved_profile() or {}
    saved_team = saved.get("favorite_team") or ""
    saved_opponents = saved.get("favorite_opponents") or []
    st.markdown("## 🎯 Find My Game")
    st.write("Tell KickSeatz what you want and it will surface upcoming NFL games that fit your preferences.")
    saved_profile = load_saved_profile() or {}
    has_saved_profile = bool(saved_profile.get("favorite_team") or saved_profile.get("location") or saved_profile.get("seat_area") != "No preference")
    if has_saved_profile:
        st.info("✨ Using your saved KickSeatz Profile as the starting point. You can still override any preference below.")
    with st.form("kz_find_game_form_nfl"):
        left, right = st.columns(2)
        with left:
            team_options = ["All NFL"] + NFL_TEAM_NAMES
            team_value = saved_team if saved_team in NFL_TEAM_NAMES else "All NFL"
            team = st.selectbox("Which team are you looking for?", team_options, index=team_options.index(team_value), key="kz_find_game_team")
            favorite_opponents = st.multiselect("Any favorite opponents?", NFL_TEAM_NAMES, default=[x for x in saved_opponents if x in NFL_TEAM_NAMES], key="kz_find_game_opponents")
            home_away_options = ["Either", "Prefer home games", "Prefer away games"]
            home_away_default = _profile_home_away_preference(saved_profile)
            home_away = st.selectbox("Where do you want to see the game?", home_away_options, index=home_away_options.index(home_away_default), key="kz_find_game_home_away")
        with right:
            travel_options = ["Home area", "Up to 500 miles", "Up to 1,000 miles", "Anywhere in the U.S.", "Anywhere, including international"]
            travel_default = _profile_travel_preference(saved_profile)
            travel = st.selectbox("How far are you willing to travel?", travel_options, index=travel_options.index(travel_default), key="kz_find_game_travel")
            vibe_options = ["Elite opponent / marquee matchup", "Rivalry atmosphere", "Affordable / value-focused", "Home-field experience", "Unique travel experience"]
            vibe_default = _profile_experience_preference(saved_profile)
            vibe = st.selectbox("What kind of experience do you want?", vibe_options, index=vibe_options.index(vibe_default), key="kz_find_game_vibe")
            budget = st.number_input("Maximum ticket budget per ticket", min_value=25.0, max_value=5000.0, value=float(saved_profile.get("typical_budget", 100.0) or 100.0), step=5.0, key="kz_find_game_budget")
        submitted = st.form_submit_button("Find My Games", use_container_width=True, type="primary")
    if submitted:
        st.session_state["kz_nfl_quiz_results"] = get_generic_nfl_game_recommendations({
            "team": "" if team == "All NFL" else team, "favorite_opponents": favorite_opponents,
            "home_away": home_away, "travel": travel, "vibe": vibe, "budget": budget,
        })
    results = st.session_state.get("kz_nfl_quiz_results", [])
    if results:
        st.markdown("### Your best-fit upcoming games")
        for index, result in enumerate(results[:6], start=1):
            game = result["game"]
            with st.container(border=True):
                left, right = st.columns([4, 1])
                with left:
                    st.markdown(f"### #{index} {_format_nfl_matchup(game)}")
                    st.caption(f"Week {game.get('week')} • {game.get('game_date') or 'Date TBD'} • {game.get('venue', 'Venue TBD')} • {game.get('location', 'Location TBD')}")
                    for reason in result["reasons"]:
                        st.write(f"✓ {reason}")
                with right:
                    st.metric("Preference Match", f"{result['score']}/100")
                    if st.button("Find tickets", key=f"kz_use_nfl_game_{index}_{game.get('game_id')}", use_container_width=True):
                        st.session_state["kz_ticket_team"] = game.get("team")
                        st.session_state["kz_ticket_game_id"] = game.get("game_id")
                        _kz_navigate("find_tickets")
        st.caption("Preference Match is based on the information you entered; it is not a prediction of game outcomes or ticket prices.")

def render_platform_find_tickets():
    saved = load_saved_profile() or {}
    saved_team = st.session_state.get("kz_ticket_team") or saved.get("favorite_team") or ""
    default_team = saved_team if saved_team in NFL_TEAM_NAMES else "All NFL"
    st.markdown("## 🎟️ Find Tickets")
    st.write("Search the NFL by team or matchup, then compare synthetic demo listings while live ticketing access is pending.")
    has_saved_profile = bool(
        saved.get("favorite_team")
        or saved.get("favorite_opponents")
        or saved.get("location")
        or saved.get("travel_radius") not in (None, 50)
        or saved.get("home_away") not in (None, "Either")
        or saved.get("seat_area") not in (None, "No preference")
        or saved.get("typical_budget") not in (None, 100.0)
        or saved.get("ticket_count") not in (None, 2)
        or saved.get("fan_type") not in (None, "Casual / social fan")
        or saved.get("experience") not in (None, "No strong preference")
        or saved.get("priority") not in (None, "Best Overall Value")
    )
    if has_saved_profile:
        st.info("✨ Your saved profile is pre-filling this search. Ticket matches also account for your saved team and seat preferences.")
    st.info("Demo Marketplace: ticket availability, seat locations, and prices are synthetic examples for the NFL-wide MVP. Ticketmaster Discovery may provide live event metadata, but seat-level offers are not connected yet.")
    left, right = st.columns(2)
    with left:
        team_options = ["All NFL"] + NFL_TEAM_NAMES
        team = st.selectbox("Team", team_options, index=team_options.index(default_team), key="kz_ticket_search_team")
    if team == "All NFL":
        team_games = get_upcoming_nfl_games(None, limit=1000)
    else:
        team_games = get_team_schedule(team, include_completed=False) or get_team_schedule(team)
    labels = [f"Week {g.get('week')} • {_format_nfl_matchup(g)} • {g.get('game_date') or 'Date TBD'}" for g in team_games]
    desired_id = st.session_state.get("kz_ticket_game_id")
    default_idx = next((i for i,g in enumerate(team_games) if g.get("game_id") == desired_id), 0)
    with right:
        selected_label = st.selectbox("Game", labels or ["No games available"], index=min(default_idx, max(0, len(labels)-1)), key="kz_ticket_search_game")
    selected_game = team_games[labels.index(selected_label)] if labels else None

    a,b,c,d = st.columns(4)
    with a:
        budget = st.number_input("Max price per ticket", min_value=1.0, max_value=5000.0, value=float(saved.get("typical_budget", 100.0) or 100.0), step=5.0, key="kz_ticket_search_budget")
    with b:
        ticket_count = st.selectbox("Tickets", [1,2,3,4], index=max(0,min(3,int(saved.get("ticket_count",2) or 2)-1)), key="kz_ticket_search_count")
    with c:
        priority_options = ["Best Overall Value", "Lowest Price", "Best Seats"]
        priority_default = saved.get("priority", "Best Overall Value")
        if priority_default not in priority_options:
            priority_default = "Best Overall Value"
        priority = st.selectbox("Prioritize", priority_options, index=priority_options.index(priority_default), key="kz_ticket_search_priority")
    with d:
        seat_options = ["No preference", "Lower Bowl", "Upper Bowl", "Club / Premium"]
        saved_area = saved.get("seat_area", "No preference")
        if saved_area not in seat_options:
            saved_area = "No preference"
        seat_area = st.selectbox("Seat area", seat_options, index=seat_options.index(saved_area), key="kz_ticket_search_area")

    if not selected_game:
        st.info("No schedule entry is available for this team yet.")
        return
    selected_game = enrich_game_with_ticketmaster(selected_game)
    st.markdown(render_nfl_matchup_card(selected_game), unsafe_allow_html=True)
    if selected_game.get("ticketmaster_available"):
        tm_cols = st.columns([3, 1])
        with tm_cols[0]:
            st.caption("Live event metadata: Ticketmaster Discovery • Demo seat inventory below remains synthetic until seat-level access is authorized.")
        with tm_cols[1]:
            url = selected_game.get("ticketmaster_url")
            if url:
                st.link_button("View Ticketmaster", url, use_container_width=True)
    else:
        st.caption("Ticketmaster Discovery metadata is unavailable for this matchup right now; synthetic demo inventory is still available for the MVP.")
    matches = _get_ticket_inventory_for_game(team, selected_game, budget=budget, ticket_count=ticket_count)
    if seat_area != "No preference":
        matches = [
            ticket for ticket in matches
            if get_seat_area(ticket.get("section")) == seat_area
        ]
    if not matches:
        st.markdown("### Ticket availability")
        with st.container(border=True):
            st.markdown("#### Seat inventory not connected for this game yet")
            st.write(f"The schedule is loaded for **{team}**, but KickSeatz does not currently have seat-level inventory for {_format_nfl_matchup(selected_game)}.")
            st.caption("The inventory model supports this matchup. When a connected ticket source is available, the same ticket card and scoring flow can populate here.")
        return

    st.markdown("### Available KickSeatz listings")
    scored = []
    for ticket_item in matches:
        try:
            base_score = calculate_ticket_score(ticket_item, selected_game, budget, ticket_count, priority)
        except Exception:
            base_score = 0
        profile_match = calculate_profile_ticket_match(ticket_item, selected_game, saved)
        score = round((base_score * 0.80) + (profile_match * 0.20)) if saved else base_score
        scored.append({"ticket": ticket_item, "game": selected_game, "score": score, "profile_match": profile_match})
    if priority == "Lowest Price":
        scored.sort(key=lambda x: (float(x["ticket"].get("price",0)), -x["score"]))
    elif priority == "Best Seats":
        scored.sort(key=lambda x: (calculate_seat_quality(x["ticket"]), x["score"]), reverse=True)
    else:
        scored.sort(key=lambda x: x["score"], reverse=True)
    for rank, item in enumerate(scored[:8], start=1):
        t = item["ticket"]
        with st.container(border=True):
            l,m,r = st.columns([3,1,1])
            with l:
                st.markdown(f"### #{rank} • ${float(t.get('price',0)):.0f}/ticket")
                st.write(f"Section **{t.get('section')}** • Row **{t.get('row')}**")
                st.caption(f"{int(t.get('available_quantity',0))} ticket(s) available • {t.get('source') or 'KickSeatz inventory'}")
            with m:
                st.metric("KickSeatz Score", f"{item['score']}/100")
                if saved:
                    st.caption(f"Profile Match: {item.get('profile_match', 50)}/100")
            with r:
                if st.button("Select", key=f"kz_ticket_select_{team}_{selected_game.get('game_id')}_{t.get('id')}", use_container_width=True):
                    st.session_state["kz_selected_ticket_id"] = t.get("id")
                    st.success("Ticket selected.")
    st.caption("Connected listings are separate from the NFL schedule feed. Demonstration/local inventory is labeled by source.")

    selected_ticket_id = st.session_state.get("kz_selected_ticket_id")
    selected_ticket = next((x for x in matches if int(x.get("id", -1)) == int(selected_ticket_id)) if selected_ticket_id is not None else None, None)
    if selected_ticket:
        st.markdown("### Ticket details")
        detail_left, detail_right = st.columns([2.4, 1])
        with detail_left:
            st.markdown(f"**{_format_nfl_matchup(selected_game)}**")
            st.write(f"Section **{selected_ticket.get('section')}** • Row **{selected_ticket.get('row')}** • ${float(selected_ticket.get('price', 0)):.0f}/ticket")
            st.caption("Synthetic demo listing • not live availability")
        with detail_right:
            ticket_score = calculate_ticket_score(selected_ticket, selected_game, budget, ticket_count, priority)
            st.metric("KickSeatz Score", f"{ticket_score}/100")
            target = st.number_input("Price Watch target", min_value=1.0, max_value=float(max(1, selected_ticket.get("price", 1))), value=float(selected_ticket.get("price", 1)), step=5.0, key=f"kz_watch_target_{selected_ticket.get('id')}")
            if st.button("🔔 Add Price Watch", use_container_width=True, key=f"kz_add_watch_{selected_ticket.get('id')}"):
                set_price_watch(int(selected_ticket.get("id")), float(target))
                st.success("Price Watch saved. Find it under My Tickets.")

        with st.expander("🔎 Explain Why this ticket scored this way"):
            st.write(f"**Game:** {calculate_game_score(selected_game)}/100")
            st.write(f"**Price:** {calculate_price_score(float(selected_ticket.get('price', 0)), [float(x.get('price', 0)) for x in matches if x.get('id') != selected_ticket.get('id')])}/100")
            st.write(f"**Seat:** {calculate_seat_quality(selected_ticket)}/100")
            st.write(f"**Availability:** {calculate_availability(selected_ticket, ticket_count)}/100")

    render_interactive_stadium_map(matches[0] if matches else None, selected_game, ticket_count)

def render_platform_rate_ticket():
    st.markdown("## 🧾 Rate My Ticket")
    st.write("Rate a KickSeatz listing or enter a ticket you already bought for any NFL matchup.")
    mode = st.radio("What are you rating?", ["A KickSeatz ticket", "My purchased ticket"], horizontal=True, key="kz_rate_mode")
    if mode == "A KickSeatz ticket":
        rate_options = []
        labels = []
        for ticket_item in inventory:
            game = get_nfl_game_for_ticket(ticket_item)
            if game:
                rate_options.append((ticket_item, game))
                labels.append(f"${ticket_item['price']:.0f} • {_format_nfl_matchup(game)} • Sec {ticket_item.get('section')} Row {ticket_item.get('row')}")
        if not rate_options:
            st.info("No rateable KickSeatz inventory is loaded yet.")
            return
        chosen = st.selectbox("Choose a KickSeatz ticket", labels, key="kz_rate_sample_select")
        ticket_item, game = rate_options[labels.index(chosen)]
        rating = rate_ticket(ticket_item, game)
    else:
        games = get_nfl_schedule_games()
        unique = []
        seen = set()
        for game in games:
            if game.get("game_id") in seen:
                continue
            seen.add(game.get("game_id"))
            unique.append(game)
        unique.sort(key=lambda g: (str(g.get("game_date") or "9999"), str(g.get("game_time") or "99:99")))
        labels = [f"Week {g.get('week')} • {g.get('away_team')} at {g.get('home_team')} • {g.get('game_date') or 'Date TBD'}" for g in unique]
        selected = st.selectbox("Game", labels, key="kz_owned_game_nfl")
        selected_game = unique[labels.index(selected)]
        a,b,c,d = st.columns(4)
        with a:
            purchased_team = st.selectbox("Your team", NFL_TEAM_NAMES, key="kz_owned_team")
        with b:
            paid_price = st.number_input("Price paid per ticket", min_value=1.0, max_value=5000.0, value=100.0, step=5.0, key="kz_owned_price")
        with c:
            owned_section = st.text_input("Section", value="123", key="kz_owned_section")
        with d:
            owned_row = st.text_input("Row", value="8", key="kz_owned_row")
        owned_ticket = {"id": -1, "team": purchased_team, "week": selected_game.get("week"), "opponent": selected_game.get("away_team") if purchased_team == selected_game.get("home_team") else selected_game.get("home_team"), "game_date": selected_game.get("game_date"), "section": owned_section, "row": owned_row, "price": paid_price, "available_quantity": 999, "source": "User-entered ticket"}
        game = dict(selected_game)
        game["team"] = purchased_team
        game["opponent"] = selected_game.get("away_team") if purchased_team == selected_game.get("home_team") else selected_game.get("home_team")
        game["home_game"] = purchased_team == selected_game.get("home_team") and not selected_game.get("neutral_site")
        ticket_item = owned_ticket
        rating = rate_ticket(ticket_item, game, purchased=True)
    st.markdown(f"### {rating['verdict']}")
    left, right = st.columns([3,1])
    with left:
        st.write(f"{_format_nfl_matchup(game)} • Section {ticket_item.get('section')} • Row {ticket_item.get('row')}")
    with right:
        st.metric("KickSeatz Rating", f"{rating['score']}/100")
    with st.expander("🔎 Explain this rating"):
        x1,x2,x3,x4=st.columns(4)
        x1.metric("Game", f"{rating['game']}/100")
        x2.metric("Price", f"{rating['price']}/100")
        x3.metric("Seat", f"{rating['seat']}/100")
        x4.metric("Availability", f"{rating['availability']}/100")


def render_platform_alerts():
    st.markdown("## 🔔 Price Alerts")
    st.write("Track tickets you saved and see when their recorded price reaches your target.")
    watches = get_all_price_watches()
    if not watches:
        st.info("No price watches saved yet. Add one from Find Tickets.")
        return
    for ticket_id, target_price, created_at, active in watches:
        match = next((item for item in inventory if int(item.get("id",-1)) == int(ticket_id)), None)
        if not match:
            continue
        game = get_nfl_game_for_ticket(match)
        if not game:
            continue
        current = float(match.get("price",0))
        triggered = current <= float(target_price)
        with st.container(border=True):
            left,right=st.columns([4,1])
            with left:
                st.markdown(f"### {_format_nfl_matchup(game)} • Section {match.get('section')} Row {match.get('row')}")
                st.write(f"Current: **${current:.0f}** • Target: **${float(target_price):.0f}**")
                st.caption("🚨 Price target reached." if triggered else "Watching this ticket.")
            with right:
                st.metric("Status", "Alert" if triggered else "Watching")
                if st.button("Remove", key=f"kz_remove_watch_{ticket_id}", use_container_width=True):
                    remove_price_watch(ticket_id)
                    st.rerun()


def render_platform_my_tickets():
    """One user-facing space for owned/considered tickets and saved price watches."""
    st.markdown("## 🧾 My Tickets")
    st.caption(
        "Rate a ticket you already bought or are considering, then manage the price watches you saved."
    )

    st.markdown("### 🎟️ Rate My Ticket")
    render_platform_rate_ticket()

    st.divider()

    st.markdown("### 🔔 My Price Watches")
    render_platform_alerts()


if "kz_page" not in st.session_state:
    st.session_state["kz_page"] = "home"

render_platform_nav()
current_platform_page = st.session_state.get("kz_page", "home")

if current_platform_page == "home":
    render_platform_home()
    st.stop()

if current_platform_page == "find_game":
    render_platform_find_game()
    st.stop()

if current_platform_page == "find_tickets":
    render_platform_find_tickets()
    st.stop()

if current_platform_page == "my_tickets":
    render_platform_my_tickets()
    st.stop()

if current_platform_page == "profile":
    render_platform_profile()
    st.stop()
