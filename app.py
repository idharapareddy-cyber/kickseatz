import os
import json
import sqlite3
import re
import streamlit as st

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

        rows = conn.execute("""
            SELECT
                id,
                week,
                opponent,
                game_date,
                section,
                row,
                price,
                quantity AS available_quantity
            FROM ticket_inventory
        """).fetchall()

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
                }
            )
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"Invalid ticket_inventory data encountered.\n"
                f"Database row:\n{r}\n\n"
                f"Error:\n{e}"
            ) from e

    return inventory_rows


try:
    master_dataset = load_master_dataset(MASTER_DATA_PATH)
    inventory = load_inventory(DB_PATH)
except Exception as e:
    st.error("KickSeatz could not load its data.")
    st.exception(e)
    st.stop()

# ============================================================
# SCORING DATA
# ============================================================

OPPONENT_RATINGS = {
    "Pittsburgh Steelers": 82,
    "Carolina Panthers": 58,
    "Green Bay Packers": 88,
    "New Orleans Saints": 65,
    "Baltimore Ravens": 91,
    "Chicago Bears": 72,
    "San Francisco 49ers": 94,
    "Tampa Bay Buccaneers": 79,
    "Cincinnati Bengals": 84,
    "Kansas City Chiefs": 96,
    "Minnesota Vikings": 81,
    "Detroit Lions": 87,
    "Cleveland Browns": 62,
    "Washington Commanders": 74,
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

    score += (
        OPPONENT_RATINGS.get(opponent, 70) - 70
    ) * 0.35

    if game.get("home_game"):
        score += 15

    if opponent in DIVISION_RIVALS:
        score += 15

    if game.get("ticketmaster_available"):
        score += 10

    if game.get("seatmap_url"):
        score += 5

    if game.get("safe_tix_enabled"):
        score += 5

    if game.get("all_inclusive_pricing"):
        score += 5

    return round(max(0, min(score, 100)))


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

        game = get_game_by_week(
            ticket.get("week")
        )

        if game is None:
            continue

        eligible.append(
            (ticket, game)
        )

    return eligible


def score_candidates(
    budget,
    ticket_count,
    priority,
):

    candidates = []

    for ticket, game in get_eligible_tickets(
        budget,
        ticket_count,
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

    if priority == "Lowest Price":

        reasons.append(
            f"At ${price:.0f}/ticket, this is the "
            f"lowest-priced eligible option."
        )

    elif priority == "Best Game":

        reasons.append(
            f"This matchup scores {game_score}/100 "
            f"for game quality."
        )

    else:

        reasons.append(
            f"It balances a {game_score}/100 game score "
            f"with a ${price:.0f} ticket price."
        )

    if seat_score >= 80:

        reasons.append(
            f"Section {ticket['section']} is rated strongly "
            f"for seat quality."
        )

    elif seat_score >= 60:

        reasons.append(
            f"Section {ticket['section']} provides a solid "
            f"seat-quality score of {seat_score}/100."
        )

    if availability >= max(
        ticket_count,
        3,
    ):

        reasons.append(
            f"There are {availability} tickets available, "
            f"giving your group flexibility."
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

    return reasons[:3]


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

priority = st.sidebar.radio(
    "What matters most?",
    [
        "Best Overall Value",
        "Lowest Price",
        "Best Game",
    ],
)

st.sidebar.divider()

st.sidebar.caption(
    "KickSeatz MVP • Atlanta Falcons 2026"
)

# ============================================================
# CANDIDATES
# ============================================================

eligible_tickets = get_eligible_tickets(
    budget,
    ticket_count,
)

candidates = score_candidates(
    budget,
    ticket_count,
    priority,
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
        if t["available_quantity"]
        >= ticket_count
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

label = {
    "Best Overall Value":
        "🏆 Best Overall Value",

    "Lowest Price":
        "💰 Lowest Price",

    "Best Game":
        "🔥 Best Game",
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

with right:

    st.markdown(
        f'<div class="score-big">{score}/100</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "KickSeatz Score"
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
}

st.write("")

b1, b2, b3, b4 = st.columns(4)

with b1:
    st.metric(
        "Game Quality",
        f"{breakdown['Game Quality']}/100",
    )

with b2:
    st.metric(
        "Price Score",
        f"{breakdown['Price']}/100",
    )

with b3:
    st.metric(
        "Seat Quality",
        f"{breakdown['Seat Quality']}/100",
    )

with b4:
    st.metric(
        "Availability",
        f"{breakdown['Availability']}/100",
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
    "Put up to three tickets head-to-head and let "
    "KickSeatz identify the strongest value."
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
                    "🏆 Best Value"
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
                    f"Game {r['game']} • "
                    f"Price {r['price']} • "
                    f"Seat {r['seat']} • "
                    f"Availability {r['availability']}"
                )

                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

        st.success(
            f"KickSeatz's strongest value is "
            f"Falcons vs {winner[1]['opponent']} "
            f"at ${winner[0]['price']:.0f}/ticket "
            f"({winner[2]['score']}/100)."
        )

    else:

        st.info(
            "Select at least two tickets to compare them."
        )

st.caption(
    "Core MVP features: Smart Finder • Rate My Ticket "
    "• Deal Analysis • Ticket Comparison"
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
