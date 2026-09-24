import os
import json
import sqlite3
import re
import csv
import io
import statistics
import math
from urllib.parse import urlencode
from datetime import datetime
import requests
import streamlit as st

# Streamlit page configuration must happen before other Streamlit UI calls.
st.set_page_config(
    page_title="KickSeatz",
    page_icon="🏟️",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
    """Return image URL without exposing the Ticketmaster API key to the browser."""
    return url


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
                    "snapshot_url": pick.get("snapshotImageUrl"),
                    "vfs_url": pick.get("largeVFSImageUrl"),
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



@st.cache_data(ttl=300)
def load_ticketmaster_events():
    """Load Falcons event metadata without taking down the whole app if TM is unavailable."""
    if not TICKETMASTER_API_KEY:
        return []

    try:
        response = requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={
                "apikey": TICKETMASTER_API_KEY,
                "keyword": "Atlanta Falcons",
                "city": "Atlanta",
                "stateCode": "GA",
                "countryCode": "US",
                "source": "ticketmaster",
                "size": 100,
                "sort": "date,asc",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("_embedded", {}).get("events", [])
    except (requests.RequestException, ValueError, TypeError):
        return []


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

            if "falcons" not in event_name:
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
# CUSTOM CSS
# ============================================================

st.markdown("""
<style>

.hero {
    padding: 30px 34px;
    border-radius: 20px;
    background: linear-gradient(135deg, #a71930 0%, #111111 100%);
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
                    "source": r[8],
                    "last_updated": r[9],
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

    ticketmaster_events = load_ticketmaster_events()
    
    if isinstance(master_dataset, dict):

        master_dataset["games"] = (
            enrich_games_with_ticketmaster(
                master_dataset.get(
                    "games",
                    []
                ),
                ticketmaster_events,
            )
        )

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


def get_game_by_week(week):

    games = (
        master_dataset.get("games", [])
        if isinstance(master_dataset, dict)
        else master_dataset
    )

    if not isinstance(games, list):
        raise RuntimeError(
            "The master dataset has an unexpected format. "
            "Expected a `games` list."
        )

    normalized_target = normalize_week(week)

    for game in games:

        if not isinstance(game, dict):
            continue

        if normalize_week(game.get("week")) == normalized_target:
            return game

    return None


# ============================================================
# SCORING FUNCTIONS
# ============================================================

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

    if opponent in DIVISION_RIVALS:
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


def calculate_confidence(
    ticket,
    game,
):

    confidence = 40

    comparable_count = sum(
        1
        for t in inventory
        if normalize_week(t.get("week"))
        == normalize_week(ticket.get("week"))
        and int(
            t.get(
                "available_quantity",
                0,
            )
        ) > 0
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
        if normalize_week(t.get("week")) == normalize_week(ticket.get("week"))
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
    for t, _g in eligible_pairs:
        price = float(t.get("price", 0))
        if price > 0:
            value_ratios.append((calculate_seat_quality(t) * 10) / price)

    candidate_ratio = (seat_score / current_price) if current_price > 0 else 0
    if value_ratios and candidate_ratio > 0:
        better_or_equal = sum(1 for ratio in value_ratios if ratio <= candidate_ratio)
        seat_value_score = (better_or_equal / len(value_ratios)) * 100
    else:
        seat_value_score = 50

    if budget > 0 and current_price > 0:
        # A ticket should not receive extra opportunity points merely for
        # spending a fixed percentage of the user's budget. Reward lower
        # spend while preserving value instead.
        budget_efficiency_score = _clamp((budget / current_price) * 55)
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
            if normalize_week(t.get("week"))
            == normalize_week(ticket.get("week"))
            and int(t.get("available_quantity", 0))
            >= ticket_count
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
            ticket.get("week")
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

    opponent_rank = OPPONENT_POWER_RANKINGS.get(
        opponent,
        32
    )

    if opponent_rank <= 10:
        reasons.append(
            f"{opponent} is Power Ranking #{opponent_rank}, a high-ranked matchup."
        )

    elif opponent_rank <= 20:
        reasons.append(
            f"{opponent} is Power Ranking #{opponent_rank}, giving the matchup solid game-quality value."
        )

    else:
        reasons.append(
            f"{opponent} is Power Ranking #{opponent_rank}."
        )

    if game.get("home_game"):
        reasons.append(
            "Atlanta is playing at home, which adds value "
            "to the matchup."
        )

    else:
        reasons.append(
            "This is an away game, so KickSeatz does not "
            "apply the home-game advantage."
        )

    if opponent in DIVISION_RIVALS:
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

def get_game_selector_options():
    games = (
        master_dataset.get("games", [])
        if isinstance(master_dataset, dict)
        else []
    )
    valid_games = [g for g in games if isinstance(g, dict)]
    valid_games.sort(key=lambda g: normalize_week(g.get("week")) or 999)
    return valid_games


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
    # exact Mercedes-Benz Stadium map.
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
                ATLANTA
            </text>

            <circle cx="150" cy="160" r="118"
                    fill="none" stroke="#a71930"
                    stroke-width="25" opacity="{lower_opacity}"/>
            <circle cx="150" cy="160" r="85"
                    fill="none" stroke="#111111"
                    stroke-width="18" opacity="{upper_opacity}"/>

            {"".join(
                f'<circle cx="{x}" cy="{y}" r="8" fill="#a71930" opacity=".75"/>'
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
# HERO
# ============================================================

st.markdown("""
<div class="hero">
    <h1>🏟️ KickSeatz</h1>
    <p><b>Smart Sports Ticket Finder</b></p>
    <p class="tagline">
        Stop scrolling through hundreds of tickets. Find the one
        that makes the most sense for you.
    </p>
</div>
""", unsafe_allow_html=True)

# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.header(
    "🎟️ Find Your Ticket"
)

budget = st.sidebar.slider(
    "Maximum budget per ticket",
    min_value=40,
    max_value=200,
    value=100,
    step=5,
)

ticket_count = st.sidebar.selectbox(
    "Number of tickets",
    [1, 2, 3, 4],
    index=1,
)

total_budget = budget * ticket_count
st.sidebar.caption(
    f"Maximum group spend: ${total_budget:.0f}"
)


priority = st.sidebar.radio(
    "What matters most?",
    [
        "Best Overall Value",
        "Lowest Price",
        "Best Game",
        "Best Seats",
        "Custom Mix",
    ],
)

if priority == "Custom Mix":
    st.sidebar.markdown("**Customize your scoring mix**")
    custom_game_weight = st.sidebar.slider(
        "Game Quality",
        min_value=0,
        max_value=100,
        value=40,
        step=5,
        key="custom_game_weight",
    )
    custom_price_weight = st.sidebar.slider(
        "Price",
        min_value=0,
        max_value=100,
        value=25,
        step=5,
        key="custom_price_weight",
    )
    custom_seat_weight = st.sidebar.slider(
        "Seat Quality",
        min_value=0,
        max_value=100,
        value=20,
        step=5,
        key="custom_seat_weight",
    )
    custom_availability_weight = st.sidebar.slider(
        "Availability",
        min_value=0,
        max_value=100,
        value=15,
        step=5,
        key="custom_availability_weight",
    )

    WEIGHTS["Custom Mix"] = normalize_weights(
        [
            custom_game_weight,
            custom_price_weight,
            custom_seat_weight,
            custom_availability_weight,
        ]
    )

    st.sidebar.caption(
        "Weights are automatically normalized to 100%."
    )

selector_games = get_game_selector_options()
game_selector_labels = ["All 2026 Games"] + [
    f"Week {g.get('week')} • Falcons vs {g.get('opponent')} • {g.get('game_date', 'Date N/A')}"
    for g in selector_games
]

selected_game_label = st.sidebar.selectbox(
    "Game",
    game_selector_labels,
    index=0,
)

selected_week = None
if selected_game_label != "All 2026 Games":
    selected_index = game_selector_labels.index(selected_game_label) - 1
    selected_week = selector_games[selected_index].get("week")

st.sidebar.markdown("**Advanced Filters**")

seat_area_filter = st.sidebar.selectbox(
    "Seat area",
    [
        "Any",
        "Lower Bowl",
        "Upper Bowl",
        "Other",
    ],
    index=0,
    help="Uses KickSeatz's section-numbering heuristic."
)

min_seat_quality = st.sidebar.slider(
    "Minimum seat quality",
    min_value=0,
    max_value=100,
    value=0,
    step=5,
    help="Filters tickets using KickSeatz's section/row seat-quality model."
)

rivals_only = st.sidebar.checkbox(
    "Division rivals only",
    value=False,
    help="Only show Carolina, New Orleans, or Tampa Bay matchups."
)

st.sidebar.divider()
st.sidebar.caption(
    "KickSeatz MVP • Atlanta Falcons 2026"
)
st.sidebar.caption(
    "Ticketmaster event data refreshes through a 5-minute cache."
)

if st.sidebar.button(
    "🔄 Refresh live data",
    use_container_width=True,
    help="Clear cached API data and reload the app."
):
    clear_app_cache_and_rerun()

# ============================================================
# CANDIDATES
# ============================================================

eligible_tickets = get_eligible_tickets(
    budget,
    ticket_count,
    selected_week,
    seat_area_filter,
    min_seat_quality,
    rivals_only,
)

candidates = score_candidates(
    budget,
    ticket_count,
    priority,
    selected_week,
    seat_area_filter,
    min_seat_quality,
    rivals_only,
)

# ============================================================
# TOP METRICS
# ============================================================

m1, m2, m3, m4 = st.columns(4)

with m1:
    st.metric(
        "Budget",
        f"${budget}"
    )

with m2:
    st.metric(
        "Tickets",
        ticket_count
    )

with m3:
    st.metric(
        "Eligible Options",
        len(eligible_tickets)
    )

with m4:
    st.metric(
        "Priority",
        priority
    )

if selected_week is not None:
    st.info(
        f"Game filter active: Week {selected_week} • Falcons vs "
        f"{get_game_by_week(selected_week).get('opponent', 'Unknown opponent')}"
    )

# ============================================================
# NO RESULTS
# ============================================================

if not candidates:

    st.error(
        "No tickets currently fit your requirements."
    )

    valid_prices = [
        t["price"]
        for t in inventory
        if t["available_quantity"] >= ticket_count
        and (
            selected_week is None
            or normalize_week(t.get("week")) == normalize_week(selected_week)
        )
        and (
            seat_area_filter == "Any"
            or get_seat_area(t.get("section")) == seat_area_filter
        )
        and calculate_seat_quality(t) * 10 >= int(min_seat_quality)
        and (
            not rivals_only
            or t.get("opponent") in DIVISION_RIVALS
        )
    ]

    if valid_prices:

        cheapest = min(valid_prices)

        st.info(
            f"The cheapest available option for "
            f"{ticket_count} ticket(s) is "
            f"${cheapest:.0f} per ticket. "
            f"Try a budget of at least "
            f"${cheapest:.0f}."
        )

    else:

        st.warning(
            "KickSeatz loaded the database, but no inventory "
            "has enough tickets for your requested quantity."
        )

    with st.expander(
        "Developer Debug Information"
    ):

        st.write(
            f"Database path: {DB_PATH}"
        )

        st.write(
            f"Master JSON path: {MASTER_DATA_PATH}"
        )

        st.write(
            f"Inventory rows loaded: {len(inventory)}"
        )

        st.write(
            f"Eligible tickets: {len(eligible_tickets)}"
        )

        st.write(
            f"Budget: ${budget}"
        )

        st.write(
            f"Requested tickets: {ticket_count}"
        )

        st.write(
            f"Priority: {priority}"
        )

        st.write(
            "Loaded inventory:"
        )

        for t in inventory:

            game = get_game_by_week(
                t.get("week")
            )

            st.write(
                f"ID {t['id']} | "
                f"Week {t['week']} | "
                f"Opponent: {t['opponent']} | "
                f"Price: ${t['price']:.0f} | "
                f"Quantity: {t['available_quantity']} | "
                f"Game lookup: "
                f"{'FOUND' if game else 'NOT FOUND'}"
            )

    st.stop()

# ============================================================
# TOP RECOMMENDATION
# ============================================================

recommendation = candidates[0]

ticket = recommendation["ticket"]
game = recommendation["game"]
score = recommendation["score"]

confidence = calculate_confidence(
    ticket,
    game,
)

label = {
    "Best Overall Value":
        "🏆 Best Overall Value",

    "Lowest Price":
        "💰 Lowest Price",

    "Best Game":
        "🔥 Best Game",

    "Best Seats":
        "💺 Best Seats",

    "Custom Mix":
        "🎯 Custom Mix",
}[priority]

st.markdown(
    '<div class="section-title">'
    'Your KickSeatz Recommendation'
    '</div>',
    unsafe_allow_html=True,
)

left, right = st.columns([3, 1])

with left:

    st.markdown(
        f'<div class="badge">{label}</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f"### Atlanta Falcons vs {game['opponent']}"
    )

    game_type = (
        "Home"
        if game.get("home_game")
        else "Away"
    )

    details = [
        f"**Week {game.get('week')}**",
        f"**{game_type} game**",
        f"📅 {game.get('game_date', 'Date unavailable')}",
    ]

    if game.get("venue"):

        details.append(
            f"🏟️ {game.get('venue')}"
        )

    st.write(
        " • ".join(details)
    )

    st.write(
        f"💺 **Section {ticket['section']} "
        f"• Row {ticket['row']}**"
    )

    st.write(
        f"🎟️ {ticket['available_quantity']} "
        f"tickets available"
    )

    st.caption(get_ticketmaster_status_text(game))

    if game.get("ticketmaster_url"):
        st.link_button(
            "🎟️ View Event on Ticketmaster",
            game["ticketmaster_url"],
        )
    else:
        st.info(
            "Ticketmaster event link is unavailable for this matchup."
        )

    if game.get("seatmap_url"):
        st.link_button(
            "🗺️ View Seat Map",
            game["seatmap_url"],
        )

    with st.expander("Ticket Details"):
        st.write(f"Inventory Ticket ID: `{ticket.get('id')}`")
        st.write(f"Section: **{ticket.get('section')}**")
        st.write(f"Row: **{ticket.get('row')}**")
        st.write(f"Available: **{ticket.get('available_quantity')}**")
        if ticket.get("source"):
            st.write(f"Inventory source: **{ticket.get('source')}**")
        if ticket.get("last_updated"):
            st.write(f"Inventory timestamp: **{get_last_updated_display(ticket.get('last_updated'))}**")

    st.markdown(
        '<div class="section-title">🗺️ Seat Map & Live Picks</div>',
        unsafe_allow_html=True,
    )

    # Render the SVG as a dedicated HTML component so Streamlit does not
    # expose the SVG/HTML markup as visible text.
    st.components.v1.html(
        get_schematic_seat_map_svg(ticket),
        height=360,
        scrolling=False,
    )

    if game.get("seatmap_url"):
        st.link_button(
            "🗺️ Open Official Seat Map",
            game["seatmap_url"],
            use_container_width=True,
        )

    if TOP_PICKS_ENABLED:
        live_picks, live_picks_error = load_top_picks(
            game.get("ticketmaster_event_id"),
            quantity=ticket_count,
            max_price=budget,
        )

        if live_picks:
            st.success(
                f"Live Ticketmaster Top Picks connected • "
                f"{len(live_picks)} pick(s) returned for your budget."
            )

            for pick_index, live_pick in enumerate(live_picks[:3], start=1):
                pick_cols = st.columns([1.5, 1, 1, 1.1])

                with pick_cols[0]:
                    location = (
                        f"Section {live_pick.get('section') or 'N/A'}"
                    )
                    if live_pick.get("row"):
                        location += f" • Row {live_pick.get('row')}"

                    st.markdown(f"**Live Pick #{pick_index}**")
                    st.write(location)

                    if live_pick.get("seats"):
                        st.caption(
                            "Seats: " + ", ".join(
                                str(seat)
                                for seat in live_pick["seats"]
                            )
                        )

                with pick_cols[1]:
                    st.metric(
                        "Quality",
                        f"{live_pick.get('quality', 0)}/100",
                    )

                with pick_cols[2]:
                    live_price = live_pick.get("total_price")
                    if live_price is not None:
                        currency = live_pick.get("currency") or "USD"
                        symbol = "$" if currency == "USD" else f"{currency} "
                        st.metric(
                            "Price",
                            f"{symbol}{float(live_price):.2f}",
                        )
                    else:
                        st.metric("Price", "See offer")

                with pick_cols[3]:
                    if live_pick.get("vfs_url"):
                        st.link_button(
                            "View From Seat",
                            live_pick["vfs_url"],
                            key=f"live_vfs_{pick_index}_{ticket['id']}",
                        )
                    elif live_pick.get("snapshot_url"):
                        st.link_button(
                            "Seat Preview",
                            live_pick["snapshot_url"],
                            key=f"live_snap_{pick_index}_{ticket['id']}",
                        )

                description_parts = [
                    live_pick.get("area"),
                    live_pick.get("description"),
                    live_pick.get("offer_name"),
                ]

                description = " • ".join(
                    str(part)
                    for part in description_parts
                    if part
                )

                if description:
                    st.caption(description)

                if live_pick.get("listing_details"):
                    st.caption(
                        str(live_pick["listing_details"])
                    )
        elif live_picks_error:
            st.info(
                f"Live seat picks are not available right now: "
                f"{live_picks_error}"
            )
    else:
        st.info(
            "Live Ticketmaster seat picks are ready to activate when "
            "authorized Top Picks access is enabled."
        )

with right:

    st.markdown(
        f'<div class="score-big">{score}/100</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "KickSeatz Score"
    )
    st.metric(
        "Confidence",
        f"{confidence}/100",

    )
    st.caption(
        "Based on comparable ticket inventory, price-history data, and game data."
    )
    st.markdown(
        f'<div class="price-big">'
        f'${ticket["price"]:.0f}'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "per ticket"
    )

    st.write(
        f"**${ticket['price'] * ticket_count:.0f} total**"
    )

# ============================================================
# BUDGET FIT
# ============================================================

budget_used = min(
    ticket["price"] / budget,
    1.0,
)

st.markdown(
    '<div class="budget-box">',
    unsafe_allow_html=True,
)

st.write(
    f"**Budget usage:** "
    f"${ticket['price']:.0f} of "
    f"${budget:.0f} per ticket"
)

st.progress(
    budget_used
)

st.caption(
    f"${budget - ticket['price']:.0f} of your "
    f"per-ticket budget remains."
)

st.markdown(
    "</div>",
    unsafe_allow_html=True,
)


# ============================================================
# PRICE WATCH
# ============================================================

watch_status = get_watch_status(ticket)

st.markdown(
    '<div class="section-title">🔔 Price Watch</div>',
    unsafe_allow_html=True,
)

watch_cols = st.columns([2, 1, 1])

with watch_cols[0]:
    default_target = max(
        1,
        int(round(float(ticket["price"]) - 5)),
    )

    target_price = st.number_input(
        "Alert me when this ticket reaches",
        min_value=1,
        max_value=max(1, int(math.ceil(float(ticket["price"])))),
        value=min(
            max(1, int(math.ceil(float(ticket["price"])))),
            default_target,
        ),
        step=5,
        key=f"watch_target_{ticket['id']}",
    )

with watch_cols[1]:
    st.write("")
    st.write("")
    watch_button = st.button(
        "🔔 Watch Ticket",
        use_container_width=True,
        key=f"watch_button_{ticket['id']}",
    )

with watch_cols[2]:
    st.write("")
    st.write("")
    remove_watch_button = st.button(
        "Remove Watch",
        use_container_width=True,
        key=f"remove_watch_{ticket['id']}",
    )

if watch_button:
    set_price_watch(
        ticket["id"],
        target_price,
    )
    st.success(
        f"Price watch saved. KickSeatz will flag this ticket "
        f"when the recorded price is at or below ${target_price:.0f}."
    )
    watch_status = get_watch_status(ticket)

if remove_watch_button:
    remove_price_watch(ticket["id"])
    st.info("Price watch removed.")
    watch_status = None

watch_status = get_watch_status(ticket)

if watch_status:
    if watch_status["triggered"]:
        st.success(
            f"🚨 Price Watch Triggered — current price is "
            f"${watch_status['current']:.0f}, at or below your "
            f"${watch_status['target']:.0f} target."
        )
    else:
        difference = watch_status["current"] - watch_status["target"]
        st.info(
            f"Watching this ticket. It is "
            f"${difference:.0f} above your ${watch_status['target']:.0f} target."
        )
else:
    st.caption(
        "Price watches are stored in the local KickSeatz database. "
        "This MVP displays alerts inside the app; it does not send email or SMS."
    )

# ============================================================
# HISTORICAL PRICE TIMING
# ============================================================

timing = get_price_timing_snapshot(ticket)

pt1, pt2 = st.columns(2)

with pt1:
    st.markdown(
        f"**{timing['label']}**"
    )

with pt2:
    if timing["low"] is not None:
        st.caption(
            f"Observed historical low: ${timing['low']:.0f}"
        )

st.caption(
    timing["detail"]
)

st.caption(
    "Historical signal only — it describes recorded prices and does not predict future prices."
)

# ============================================================
# HISTORICAL TIMING SIGNAL
# ============================================================

timing_advice = get_price_timing_advice(ticket)

st.markdown(
    '<div class="section-title">🧭 Historical Timing Signal</div>',
    unsafe_allow_html=True,
)

if timing_advice["tone"] == "success":
    st.success(f"{timing_advice['headline']} — {timing_advice['detail']}")
elif timing_advice["tone"] == "warning":
    st.warning(f"{timing_advice['headline']} — {timing_advice['detail']}")
else:
    st.info(f"{timing_advice['headline']} — {timing_advice['detail']}")

st.caption(
    "This is a descriptive historical signal based only on recorded KickSeatz snapshots; it is not a buy/sell prediction."
)

# ============================================================
# OPPORTUNITY ENGINE
# ============================================================

recommendation_opportunity = recommendation.get("opportunity", {})
opportunity_candidates = sorted(
    candidates,
    key=lambda item: item.get("opportunity", {}).get("score", 0),
    reverse=True,
)
best_opportunity = opportunity_candidates[0] if opportunity_candidates else recommendation
best_opp = best_opportunity.get("opportunity", {})

st.markdown(
    '<div class="section-title">🔥 Opportunity Engine</div>',
    unsafe_allow_html=True,
)

st.caption(
    "KickSeatz looks for value that a simple cheapest-ticket search can miss. "
    "This signal compares the actual eligible market, seat value, budget efficiency, "
    "observed price history, and current inventory. It is descriptive, not a prediction."
)

opp_col1, opp_col2, opp_col3, opp_col4 = st.columns(4)

with opp_col1:
    st.metric(
        "Opportunity Score",
        f"{best_opp.get('score', 0)}/100",
    )
    st.caption(best_opp.get("label", "Market Value"))

with opp_col2:
    st.metric(
        "Same-Game Median",
        f"${best_opp.get('same_game_median', float(best_opportunity['ticket']['price'])):.0f}",
    )
    st.caption(
        f"Selected: ${float(best_opportunity['ticket']['price']):.0f}/ticket"
    )

with opp_col3:
    st.metric(
        "Seat Value",
        f"{best_opp.get('seat_value_score', 0)}/100",
    )
    st.caption("Relative seat quality per dollar")

with opp_col4:
    st.metric(
        "History Signal",
        f"{best_opp.get('history_score', 0)}/100",
    )
    st.caption(
        f"{best_opp.get('history', {}).get('sample_size', 0)} recorded observations"
    )

opp_ticket = best_opportunity["ticket"]
opp_game = best_opportunity["game"]

if best_opportunity["ticket"]["id"] != ticket["id"]:
    st.info(
        f"**Hidden opportunity:** Falcons vs {opp_game.get('opponent', 'Unknown')} "
        f"— Section {opp_ticket.get('section')} • Row {opp_ticket.get('row')} "
        f"— ${float(opp_ticket.get('price', 0)):.0f}/ticket. "
        f"{best_opp.get('reason', '')}"
    )
else:
    st.success(
        f"**Your recommendation is also the strongest opportunity.** "
        f"{best_opp.get('reason', '')}"
    )

upgrade = get_opportunity_upgrade(recommendation, candidates)
if upgrade:
    upgrade_ticket = upgrade["candidate"]["ticket"]
    upgrade_game = upgrade["candidate"]["game"]
    st.markdown("**What would your next dollars actually buy?**")
    u1, u2, u3 = st.columns(3)
    with u1:
        st.metric(
            "Extra Cost",
            f"+${upgrade['extra_cost']:.0f}/ticket",
        )
    with u2:
        st.metric(
            "Seat Quality Gain",
            f"+{upgrade['seat_gain']} points",
        )
    with u3:
        st.caption(
            f"Upgrade: Falcons vs {upgrade_game.get('opponent', 'Unknown')} "
            f"• Sec {upgrade_ticket.get('section')} • Row {upgrade_ticket.get('row')} "
            f"• ${float(upgrade_ticket.get('price', 0)):.0f}"
        )

with st.expander("How KickSeatz found this opportunity"):
    st.write(
        f"**Relative price:** {best_opp.get('relative_price_score', 0)}/100 — "
        "compared with the actual eligible tickets for the matchup."
    )
    st.write(
        f"**Seat value:** {best_opp.get('seat_value_score', 0)}/100 — "
        "seat quality relative to ticket price across eligible inventory."
    )
    st.write(
        f"**Budget efficiency:** {best_opp.get('budget_efficiency_score', 0)}/100 — "
        "how efficiently the ticket uses the selected per-ticket budget."
    )
    st.write(
        f"**Historical signal:** {best_opp.get('history_score', 0)}/100 — "
        "based only on recorded prices for this ticket."
    )
    st.write(
        f"**Availability signal:** {best_opp.get('scarcity_score', 0)}/100 — "
        "based on current inventory available for the requested quantity."
    )

# ============================================================
# PRICE BENCHMARK + BUDGET OPPORTUNITY
# ============================================================

benchmark = get_price_benchmark(ticket)
if benchmark:
    st.markdown(
        '<div class="section-title">📈 Price Benchmark</div>',
        unsafe_allow_html=True,
    )

    pb1, pb2, pb3, pb4 = st.columns(4)

    with pb1:
        st.metric("Same-game Median", f"${benchmark['median']:.0f}")

    with pb2:
        st.metric("Cheapest Available", f"${benchmark['cheapest']:.0f}")

    with pb3:
        delta_text = (
            f"${abs(benchmark['difference']):.0f} below median"
            if benchmark["difference"] < 0
            else f"${benchmark['difference']:.0f} above median"
            if benchmark["difference"] > 0
            else "At median"
        )
        st.metric("Ticket Position", f"{benchmark['percentile']}%", delta_text)

    with pb4:
        st.metric("Comparable Tickets", benchmark["sample_size"])

    st.caption(
        "Benchmark uses KickSeatz's currently loaded inventory for the same matchup; "
        "it is not a live market-wide average."
    )

cheaper_option, upgrade_option = get_budget_insights(
    candidates,
    ticket,
    budget,
)

if cheaper_option or upgrade_option:
    st.markdown(
        '<div class="section-title">💡 What Your Budget Can Change</div>',
        unsafe_allow_html=True,
    )

    budget_cols = st.columns(2)

    with budget_cols[0]:
        if cheaper_option:
            ct = cheaper_option["ticket"]
            cg = cheaper_option["game"]
            savings = float(ticket["price"]) - float(ct["price"])
            st.success(
                f"Save ${savings:.0f}/ticket with Falcons vs {cg['opponent']} "
                f"at ${float(ct['price']):.0f}."
            )
            st.caption(
                f"Section {ct['section']} • Row {ct['row']} • "
                f"Score {cheaper_option['score']}/100"
            )
        else:
            st.info("No cheaper eligible ticket is available in the current inventory.")

    with budget_cols[1]:
        if upgrade_option:
            ut = upgrade_option["ticket"]
            ug = upgrade_option["game"]
            extra = float(ut["price"]) - float(ticket["price"])
            st.info(
                f"Spend ${extra:.0f} more/ticket for Falcons vs {ug['opponent']} "
                f"at ${float(ut['price']):.0f}."
            )
            st.caption(
                f"Section {ut['section']} • Row {ut['row']} • "
                f"Score {upgrade_option['score']}/100"
            )
        else:
            st.info("No higher-priced eligible upgrade is available within your budget.")

# ============================================================
# WHY KICKSEATZ CHOSE IT
# ============================================================

st.markdown(
    '<div class="section-title">'
    '🧠 Why KickSeatz Chose This Ticket'
    '</div>',
    unsafe_allow_html=True,
)

reasons = get_reasons(
    ticket,
    game,
    budget,
    ticket_count,
    priority,
)

st.markdown(
    '<div class="reason-card">',
    unsafe_allow_html=True,
)

for reason in reasons:
    st.markdown(
        f"• {reason}"
    )

st.markdown(
    "</div>",
    unsafe_allow_html=True,
)

# ============================================================
# BEST ALTERNATIVE
# ============================================================

if len(candidates) >= 2:
    alternative = candidates[1]
    at = alternative["ticket"]
    ag = alternative["game"]

    st.markdown(
        '<div class="section-title">🔄 Best Alternative</div>',
        unsafe_allow_html=True,
    )

    alt1, alt2, alt3 = st.columns(3)
    with alt1:
        st.metric("Alternative Score", f"{alternative['score']}/100")
    with alt2:
        st.metric("Price", f"${float(at['price']):.0f}/ticket")
    with alt3:
        score_gap = alternative["score"] - score
        st.metric("Score Difference", f"{score_gap:+.0f}")

    st.write(
        f"Falcons vs **{ag['opponent']}** • Section **{at['section']}** • "
        f"Row **{at['row']}** • {at['available_quantity']} available"
    )
    st.caption(
        "This is the next option in the same recommendation model, not a separate opinion."
    )

# ============================================================
# SCORE BREAKDOWN
# ============================================================

breakdown = {
    "Game Quality":
        calculate_game_score(game),

    "Price":
        calculate_price_score(
            ticket["price"],
            [
                float(t.get("price", 0))
                for t in inventory
                if normalize_week(t.get("week"))
                == normalize_week(ticket.get("week"))
                and int(t.get("available_quantity", 0)) > 0
            ]
        ),

    "Seat Quality":
        calculate_seat_quality(ticket) * 10,

    "Availability":
        calculate_availability(
            ticket,
            ticket_count,
        ),

    "Confidence":
        confidence,
}

st.write("")

b1, b2, b3, b4, b5 = st.columns(5)

with b1:
    st.metric(
        "Game Quality",
        f"{breakdown['Game Quality']}/100",
    )

    opponent = game.get(
        "opponent",
        ""
    )

    opponent_rank = OPPONENT_POWER_RANKINGS.get(
        opponent,
        32,
    )

    game_notes = []

    game_notes.append(
        f"{opponent} is ranked #{opponent_rank}."
    )

    if game.get("home_game"):
        game_notes.append(
            "Home-game advantage included."
        )

    if opponent in DIVISION_RIVALS:
        game_notes.append(
            "Division-rival bonus included."
        )

    st.caption(
        " ".join(game_notes)
    )

with b2:
    st.metric(
        "Price Score",
        f"{breakdown['Price']}/100",
    )

    comparable_prices = [
        float(t.get("price", 0))
        for t in inventory
        if normalize_week(t.get("week"))
        == normalize_week(ticket.get("week"))
        and int(t.get("available_quantity", 0)) > 0
    ]

    if comparable_prices:
        cheaper_count = sum(
            1
            for p in comparable_prices
            if p >= float(ticket["price"])
        )

        price_percentile = round(
            (cheaper_count / len(comparable_prices)) * 100
        )

        st.caption(
            f"${ticket['price']:.0f} is cheaper than "
            f"{price_percentile}% of comparable available tickets."
        )

with b3:
    st.metric(
        "Seat Quality",
        f"{breakdown['Seat Quality']}/100",
    )

    section = str(
        ticket.get("section", "")
    )

    row = str(
        ticket.get("row", "")
    )

    seat_notes = []

    try:
        section_number = int(
            "".join(
                c for c in section
                if c.isdigit()
            )
        )

        if 101 <= section_number <= 134:
            seat_notes.append(
                "Lower-bowl section."
            )

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
            seat_notes.append(
                "Excellent row position."
            )

        elif row_number <= 10:
            seat_notes.append(
                "Good row position."
            )

    except ValueError:
        pass

    if not seat_notes:
        seat_notes.append(
            "KickSeatz seat-quality heuristic based on section and row."
        )

    st.caption(
        " ".join(seat_notes)
    )

with b4:
    st.metric(
        "Availability",
        f"{breakdown['Availability']}/100",
    )

    available = int(
        ticket.get(
            "available_quantity",
            0,
        )
    )

    if available >= ticket_count:
        st.caption(
            f"{available} tickets available — "
            f"enough for your group."
        )
    else:
        st.caption(
            "Not enough tickets available "
            "for your requested quantity."
        )

with b5:
    st.metric(
        "Confidence",
        f"{breakdown['Confidence']}/100",
    )

    if confidence >= 85:
        confidence_note = (
            "High confidence — strong data coverage."
        )

    elif confidence >= 70:
        confidence_note = (
            "Good confidence — enough data for a solid comparison."
        )

    elif confidence >= 50:
        confidence_note = (
            "Moderate confidence — more data would improve reliability."
        )

    else:
        confidence_note = (
            "Low confidence — limited comparison or history data."
        )

    st.caption(
        confidence_note
    )

# ============================================================
# PRICE HISTORY
# ============================================================

st.markdown(
    '<div class="section-title">'
    '📉 Price History'
    '</div>',
    unsafe_allow_html=True,
)

history_conn = sqlite3.connect(DB_PATH)

try:
    history_rows = history_conn.execute(
        """
        SELECT price, recorded_at
        FROM price_history
        WHERE ticket_id = ?
        ORDER BY recorded_at
        """,
        (ticket["id"],),
    ).fetchall()

finally:
    history_conn.close()

if history_rows:

    history_prices = [
        float(row[0])
        for row in history_rows
    ]

    starting_price = history_prices[0]
    current_price = history_prices[-1]
    price_change = current_price - starting_price

    if starting_price > 0:
        percent_change = (
            price_change / starting_price
        ) * 100
    else:
        percent_change = 0

    h1, h2, h3, h4 = st.columns(4)

    with h1:
        st.metric(
            "Starting Price",
            f"${starting_price:.0f}",
        )

    with h2:
        st.metric(
            "Current Price",
            f"${current_price:.0f}",
        )

    with h3:
        st.metric(
            "Price Change",
            f"${price_change:+.0f}",
        )

    with h4:
        st.metric(
            "% Change",
            f"{percent_change:+.1f}%",
        )

    st.line_chart(
        {
            "Ticket Price": history_prices
        }
    )

    if len(history_prices) >= 2:

        previous_price = history_prices[-2]
        current_price = history_prices[-1]

        price_change = (
            current_price - previous_price
        )

        if previous_price > 0:
            percent_change = (
                price_change / previous_price
            ) * 100
        else:
            percent_change = 0

        if percent_change <= -10:

            st.success(
                f"🚨 Price Drop Alert — "
                f"${abs(price_change):.0f} cheaper "
                f"({abs(percent_change):.1f}% drop) "
                f"than the previous recorded price."
            )

        elif price_change < 0:

            st.info(
                f"Price dropped ${abs(price_change):.0f} "
                f"({abs(percent_change):.1f}%) "
                f"from the previous snapshot."
            )

        elif price_change > 0:

            st.warning(
                f"Price increased ${price_change:.0f} "
                f"({percent_change:.1f}%) "
                f"from the previous snapshot."
            )

        else:

            st.info(
                "The ticket price has not changed "
                "since the previous snapshot."
            )

    else:

        st.info(
            "Only one price snapshot has been recorded so far."
        )

    st.caption(
        f"{len(history_rows)} price snapshot(s) recorded."
    )

else:

    st.info(
        "No price history has been recorded for this ticket yet."
    )


# ============================================================
# TOP 3 COMPARISON
# ============================================================

st.markdown(
    '<div class="section-title">'
    '📊 Compare Your Top Options'
    '</div>',
    unsafe_allow_html=True,
)

top_options = candidates[:3]

compare_cols = st.columns(
    len(top_options)
)

for i, (col, option) in enumerate(
    zip(compare_cols, top_options),
    start=1,
):

    option_ticket = option["ticket"]
    option_game = option["game"]

    with col:

        st.markdown(
            '<div class="compare-card">',
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<div class="compare-rank">'
            f'#{i} Option'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            f"**Falcons vs {option_game['opponent']}**"
        )

        st.markdown(
            f"💺 Section {option_ticket['section']} "
            f"• Row {option_ticket['row']}"
        )

        st.markdown(
            f"### ${option_ticket['price']:.0f}/ticket"
        )
        st.caption(
            f"${float(option_ticket['price']) * ticket_count:.0f} total for {ticket_count} ticket(s)"
        )

        if option_game.get("ticketmaster_url"):
            st.link_button(
                "View Ticketmaster Event",
                option_game["ticketmaster_url"],
                key=f"top3_tm_{i}_{option_ticket['id']}",
            )

        st.markdown(
            f'<div class="compare-score">'
            f'{option["score"]}/100'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.caption(
            f"Game "
            f"{calculate_game_score(option_game)}/100 • "
            f"Seat "
            f"{calculate_seat_quality(option_ticket) * 10}/100"
        )

        st.markdown(
            "</div>",
            unsafe_allow_html=True,
        )

# ============================================================
# OTHER OPTIONS
# ============================================================

if len(candidates) > 3:

    with st.expander(
        f"See all {len(candidates)} eligible options"
    ):

        for i, option in enumerate(
            candidates[3:],
            start=4,
        ):

            t = option["ticket"]
            g = option["game"]

            st.write(
                f"**#{i} Falcons vs {g['opponent']}** — "
                f"Section {t['section']}, "
                f"Row {t['row']} — "
                f"${t['price']:.0f}/ticket — "
                f"Score {option['score']}/100"
            )

# ============================================================
# EXPORT RESULTS
# ============================================================

if candidates:
    st.markdown(
        '<div class="section-title">⬇️ Save Your Results</div>',
        unsafe_allow_html=True,
    )
    st.download_button(
        "Download Top Matches as CSV",
        data=build_candidate_csv(candidates),
        file_name="kickseatz_recommendations.csv",
        mime="text/csv",
    )
    st.caption(
        "Exports up to 15 currently eligible matches using the active budget, ticket count, game filter, and priority."
    )

# ============================================================
# RATE MY TICKET + DEAL ANALYSIS
# ============================================================

st.markdown(
    '<div class="section-title">'
    '🎟️ Rate My Ticket'
    '</div>',
    unsafe_allow_html=True,
)

st.caption(
    "Already found a ticket? KickSeatz evaluates its "
    "value independently of your search budget."
)

rate_options = []

for t in inventory:

    g = get_game_by_week(
        t.get("week")
    )

    if g:
        rate_options.append(
            (t, g)
        )

if rate_options:

    labels = [
        f"${t['price']:.0f} • "
        f"Falcons vs {g['opponent']} • "
        f"Section {t['section']} "
        f"Row {t['row']}"
        for t, g in rate_options
    ]

    selected_label = st.selectbox(
        "Select a ticket to rate",
        labels,
        key="rate_ticket_select",
    )

    idx = labels.index(
        selected_label
    )

    rt, rg = rate_options[idx]

    rating = rate_ticket(
        rt,
        rg,
    )

    deal_title, deal_text = get_deal_assessment(
        rating
    )

    st.markdown(
        '<div class="rate-card">',
        unsafe_allow_html=True,
    )

    left, right = st.columns([3, 1])

    with left:

        st.markdown(
            f'<div class="deal-badge">'
            f'{rating["verdict"]}'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            f"### Falcons vs {rg['opponent']}"
        )

        st.write(
            f"📅 {rg.get('game_date', 'Date unavailable')} "
            f"• 💺 Section {rt['section']} "
            f"• Row {rt['row']}"
        )

        st.write(
            f"💵 ${rt['price']:.0f}/ticket "
            f"• 🎟️ {rt['available_quantity']} available"
        )

    with right:

        st.markdown(
            f'<div class="rate-score">'
            f'{rating["score"]}/100'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.caption(
            "KickSeatz Ticket Rating"
        )

    st.markdown(
        f'<div class="deal-answer">'
        f'<b>Is this a good deal?</b><br>'
        f'<b>{deal_title}.</b> {deal_text}'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        "**Rating Breakdown**"
    )

    a, b, c, d = st.columns(4)

    for col, name, key in [
        (a, "Game", "game"),
        (b, "Price", "price"),
        (c, "Seat", "seat"),
        (d, "Availability", "availability"),
    ]:

        with col:

            st.metric(
                name,
                f"{rating[key]}/100",
            )

            if key == "game":

                opponent = rg.get(
                    "opponent",
                    ""
                )

                opponent_rank = OPPONENT_POWER_RANKINGS.get(
                    opponent,
                    32,
                )

                st.caption(
                    f"{opponent} is ranked #{opponent_rank}. "
                    "Source: NFL.com Week 1 Power Rankings (2026)."
                )

            elif key == "price":

                st.caption(
                    "Compared with available tickets for the same matchup."
                )

            elif key == "seat":

                st.caption(
                    f"Section {rt['section']} • "
                    f"Row {rt['row']}."
                )

            elif key == "availability":

                st.caption(
                    f"{rt['available_quantity']} tickets available."
                )

    st.markdown(
        "**Why this rating?**"
    )

    for reason in get_rate_reasons(
        rt,
        rg,
        rating,
    ):

        st.markdown(
            f"• {reason}"
        )

    st.caption(
        "This rating does not use your search budget."
    )

    st.markdown(
        "</div>",
        unsafe_allow_html=True,
    )

# ============================================================
# COMPARE TICKETS
# ============================================================

st.markdown(
    '<div class="section-title">'
    '⚖️ Compare Tickets'
    '</div>',
    unsafe_allow_html=True,
)

st.caption(
    "Put up to three tickets head-to-head and compare their KickSeatz ratings."
)

if len(rate_options) >= 2:

    compare_labels = [
        f"${t['price']:.0f} • "
        f"{g['opponent']} • "
        f"Sec {t['section']} Row {t['row']}"
        for t, g in rate_options
    ]

    selected = st.multiselect(
        "Choose 2–3 tickets",
        compare_labels,
        default=compare_labels[:3],
        max_selections=3,
        key="compare_tickets_select",
    )

    data = []

    for label in selected:

        i = compare_labels.index(
            label
        )

        t, g = rate_options[i]

        data.append(
            (
                t,
                g,
                rate_ticket(t, g),
            )
        )

    if len(data) >= 2:

        winner = max(
            data,
            key=lambda x: x[2]["score"],
        )

        cols = st.columns(
            len(data)
        )

        for i, (col, (t, g, r)) in enumerate(
            zip(cols, data),
            1,
        ):

            with col:

                st.markdown(
                    '<div class="compare-card">',
                    unsafe_allow_html=True,
                )

                rank_text = (
                    "🏆 Highest Score"
                    if t["id"] == winner[0]["id"]
                    else f"Option {i}"
                )

                st.markdown(
                    f'<div class="compare-rank">'
                    f'{rank_text}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                st.markdown(
                    f"**Falcons vs {g['opponent']}**"
                )

                st.write(
                    f"💺 Section {t['section']} "
                    f"• Row {t['row']}"
                )

                st.markdown(
                    f"### ${t['price']:.0f}/ticket"
                )

                st.markdown(
                    f'<div class="compare-score">'
                    f'{r["score"]}/100'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                st.caption(
                    f"Game: {r['game']}/100"
                )

                st.caption(
                    f"Price: {r['price']}/100 — "
                    "compared with available tickets."
                )

                st.caption(
                    f"Seat: {r['seat']}/100 — "
                    "KickSeatz seat-quality heuristic based on section and row."
                )

                st.caption(
                    f"Availability: {r['availability']}/100 — "
                    f"{t['available_quantity']} tickets available."
                )

                compare_confidence = calculate_confidence(
                    t,
                    g,
                )

                st.caption(
                    f"Confidence: {compare_confidence}/100 — "
                    "based on comparison and history data."
                )

                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

        st.success(
            f"Highest-rated option in this comparison: "
            f"Falcons vs {winner[1]['opponent']} "
            f"at ${winner[0]['price']:.0f}/ticket "
            f"({winner[2]['score']}/100)."
        )

    else:

        st.info(
            "Select at least two tickets to compare them."
        )

st.caption(
    "Core MVP features: Smart Finder • Game Selector • Best Seats • Custom Mix • "
    "Advanced Filters • Price Watch • Seat Map • Rate My Ticket • Deal Analysis • "
    "Price Benchmark • Ticket Comparison • CSV Export"
)


# ============================================================
# MY PRICE WATCHES
# ============================================================

all_watches = get_all_price_watches()

st.markdown(
    '<div class="section-title">🔔 My Price Watches</div>',
    unsafe_allow_html=True,
)

if not all_watches:
    st.info(
        "No price watches saved yet. Use Price Watch on a ticket to track it."
    )
else:
    for watch_index, watch_row in enumerate(all_watches, start=1):
        watch_ticket_id = int(watch_row[0])
        watched_ticket = next(
            (
                item
                for item in inventory
                if int(item.get("id")) == watch_ticket_id
            ),
            None,
        )

        if not watched_ticket:
            st.caption(
                f"Saved watch #{watch_index} references ticket ID {watch_ticket_id}, "
                "which is not currently in the loaded inventory."
            )
            continue

        watched_game = get_game_by_week(
            watched_ticket.get("week")
        )

        current_price = float(watched_ticket["price"])
        target_price = float(watch_row[1])

        with st.container(border=True):
            w1, w2, w3, w4 = st.columns(4)

            with w1:
                st.markdown(
                    f"**Falcons vs {watched_game.get('opponent', 'Unknown')}**"
                )
                st.caption(
                    f"Sec {watched_ticket['section']} • "
                    f"Row {watched_ticket['row']}"
                )

            with w2:
                st.metric(
                    "Current",
                    f"${current_price:.0f}"
                )

            with w3:
                st.metric(
                    "Target",
                    f"${target_price:.0f}"
                )

            with w4:
                if current_price <= target_price:
                    st.success("Target reached")
                else:
                    st.caption(
                        f"${current_price - target_price:.0f} above target"
                    )

                if st.button(
                    "Remove",
                    key=f"remove_saved_watch_{watch_ticket_id}",
                ):
                    remove_price_watch(watch_ticket_id)
                    st.rerun()

st.caption(
    "Watch status is based on the latest price currently loaded by KickSeatz."
)

# ============================================================
# TICKETMASTER PARTNER ACCESS STATUS
# ============================================================

st.markdown(
    '<div class="section-title">🔗 Live Ticketing Access</div>',
    unsafe_allow_html=True,
)

if TOP_PICKS_ENABLED:
    st.success(
        "Ticketmaster Top Picks integration is enabled. "
        "KickSeatz will request live seat recommendations for supported events."
    )
elif TICKETMASTER_API_KEY and ticketmaster_events:
    st.success(
        "Ticketmaster event discovery is connected. Event metadata and event links are being used by KickSeatz."
    )
    st.info(
        "Live seat-level Top Picks integration is built into the app but remains disabled "
        "until authorized Top Picks access is enabled."
    )
else:
    st.info(
        "KickSeatz is running in local/demo inventory mode for seat-level ticket selection. "
        "Live Top Picks access can be enabled without changing the core recommendation system."
    )

st.caption(
    "The app uses live event metadata now; live seat-level inventory depends on authorized Ticketmaster partner access."
)

# ============================================================
# DATA FRESHNESS
# ============================================================

st.markdown(
    '<div class="section-title">🕒 Data Freshness</div>',
    unsafe_allow_html=True,
)

freshness = get_data_freshness()
fresh_col1, fresh_col2, fresh_col3 = st.columns(3)

with fresh_col1:
    if freshness:
        st.metric(
            "Inventory Data",
            freshness.strftime("%b %d, %Y"),
        )
        st.caption(
            freshness.strftime("Last detected update: %I:%M %p")
        )
    else:
        st.metric("Inventory Data", "Timestamp unavailable")
        st.caption("The database does not expose a usable update timestamp.")

with fresh_col2:
    st.metric("Ticketmaster Metadata", "≤ 5 min cache")
    st.caption(
        "Event discovery is cached for performance and may be slightly older than a live request."
    )

with fresh_col3:
    st.metric(
        "Live Seat Inventory",
        "Enabled" if TOP_PICKS_ENABLED else "Pending access",
    )
    st.caption(
        "Live Top Picks is enabled." if TOP_PICKS_ENABLED
        else "Seat-level inventory will use authorized partner access when available."
    )

# ============================================================
# DATA NOTICE
# ============================================================

st.divider()

st.caption(
    "KickSeatz MVP • Atlanta Falcons 2026 season"
)

st.caption(
    "Ticket inventory shown in this MVP is demonstration "
    "inventory. Game and event data is sourced from the "
    "Falcons schedule and Ticketmaster event data; live "
    "seat-level inventory requires authorized "
    "ticketing-partner access."
)

# ============================================================
# DEBUG
# ============================================================

show_debug = st.sidebar.checkbox(
    "Developer mode",
    value=False,
    help="Show database paths and technical diagnostics."
)

if show_debug:
    with st.expander(
        "Developer Debug Information"
    ):

        st.write(
            f"Database path: {DB_PATH}"
        )

        st.write(
            f"Master JSON path: {MASTER_DATA_PATH}"
        )

        st.write(
            f"Inventory rows loaded: {len(inventory)}"
        )

        st.write(
            f"Budget: ${budget}"
        )

        st.write(
            f"Requested tickets: {ticket_count}"
        )

        st.write(
            f"Priority: {priority}"
        )

        st.write(
            f"Eligible tickets: {len(eligible_tickets)}"
        )

        st.write(
            f"Seat area filter: {seat_area_filter}"
        )

        st.write(
            f"Minimum seat quality: {min_seat_quality}"
        )

        st.write(
            f"Division rivals only: {rivals_only}"
        )

        if priority == "Custom Mix":
            st.write(
                f"Custom weights: {WEIGHTS['Custom Mix']}"
            )

        st.write(
            f"Recommended ticket ID: {ticket['id']}"
        )

        st.write(
            f"Recommended score: {score}"
        )

        st.write(
            "Loaded inventory:"
        )

        for t in inventory:

            game = get_game_by_week(
                t.get("week")
            )

            st.write(
                f"ID {t['id']} | "
                f"Week {t['week']} | "
                f"Opponent: {t['opponent']} | "
                f"Section {t['section']} "
                f"Row {t['row']} | "
                f"${t['price']:.0f} | "
                f"{t['available_quantity']} available | "
                f"Game lookup: "
                f"{'FOUND' if game else 'NOT FOUND'}"
            )






















