"""Family Manager — SQLite store.

One DB file under data/ (gitignored — family data never leaves this machine). All
timestamps are local ISO dates.
"""
import json
import re
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import family

ROOT = Path(__file__).resolve().parent
# On Railway set FM_DATA_DIR=/data (persistent volume) — the container FS is
# ephemeral and a redeploy would wipe the DB otherwise.
DATA_DIR = Path(os.environ.get("FM_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "family_manager.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS household_tasks (
    key TEXT PRIMARY KEY, owner TEXT, stage TEXT DEFAULT 'open', snooze_until TEXT,
    reviewed_at TEXT, updated_at TEXT, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS household_history (
    id INTEGER PRIMARY KEY, task_key TEXT, action TEXT, before_json TEXT,
    after_json TEXT, who TEXT, at TEXT, undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payment_plans (
    payment_id INTEGER PRIMARY KEY, starts TEXT, ends TEXT, change_date TEXT,
    expected_amount REAL, change_note TEXT, reviewed_at TEXT, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS payment_observations (
    id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL, charged_on TEXT NOT NULL,
    amount REAL NOT NULL, evidence TEXT NOT NULL, fingerprint TEXT UNIQUE,
    recorded_by TEXT, recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS household_reviews (
    week TEXT PRIMARY KEY, plan TEXT, burden INTEGER, updated_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    msg_id TEXT UNIQUE,
    source TEXT,             -- source key from sources table
    sender TEXT,
    subject TEXT,
    sent_date TEXT,          -- YYYY-MM-DD
    kid TEXT,                -- a kid's name (family.KIDS) | Both
    body_text TEXT,
    pdf_path TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kid TEXT,
    school TEXT,
    type TEXT,               -- closure|early_dismissal|event|deadline|meeting|info|camp
    title TEXT,
    event_date TEXT,         -- YYYY-MM-DD (may be NULL for undated info)
    end_date TEXT,
    start_time TEXT,         -- HH:MM local, NULL for all-day
    end_time TEXT,
    category TEXT,           -- kids_school|money|errand|work|travel_home|other
    details TEXT,
    source TEXT,             -- source key or 'v0_import'
    source_ref TEXT,         -- msg_id / pdf file / json week
    status TEXT DEFAULT 'active',   -- active|dismissed|cancelled
    conflict TEXT,           -- JSON: another source disagrees (e.g. two start times)
    last_seen_at TEXT,       -- last run that saw it upstream; drives cancellation
    created_at TEXT,
    UNIQUE(kid, title, event_date)
);
CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY,
    kid TEXT,
    source TEXT,
    label TEXT,              -- e.g. 'Week #39' or folder date
    nl_date TEXT,
    subject TEXT,
    content TEXT,            -- JSON payload (sections/summary)
    UNIQUE(kid, source, nl_date)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    name TEXT,
    kid TEXT,
    category TEXT,           -- tuition|camp|aftercare|activity|other
    amount REAL,             -- NULL = amount TBD
    cadence TEXT,            -- monthly|weekly|per-session|annual|one-time
    next_due TEXT,           -- YYYY-MM-DD, NULL if autopay/unknown
    autopay INTEGER DEFAULT 0,
    notes TEXT,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY,
    name TEXT,
    kid TEXT,
    sender_match TEXT,       -- substring matched against From address
    subject_match TEXT,      -- optional substring matched against Subject
    cadence TEXT,            -- weekly|monthly|adhoc
    active_months TEXT,      -- JSON list of month numbers the source is expected
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS checklist (
    id INTEGER PRIMARY KEY,
    title TEXT,
    detail TEXT,             -- the specifics: quantities, "nut free", etc.
    kid TEXT,                -- a kid's name (family.KIDS) | Both
    category TEXT,           -- supplies|admin|activity|health|other
    due_date TEXT,           -- YYYY-MM-DD, NULL if no date
    status TEXT DEFAULT 'open',  -- open|done|blocked|na
    blocked_on TEXT,         -- why it can't be done yet (shown to both parents)
    done_at TEXT,
    done_by TEXT,            -- which parent ticked it, so the other one knows
    source TEXT,             -- where it came from, e.g. the forwarded email
    sort INTEGER DEFAULT 100,
    UNIQUE(title, kid)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Every document opened out of an email — an attachment or a link that was followed to a
-- real file/page. One row per document, with the extracted text AND the path to the saved
-- original, so a parent can read the summary OR open the notice itself. status is three-
-- state: ok | failed:<why> | skipped:<why> — a link we couldn't open never renders blank.
CREATE TABLE IF NOT EXISTS mail_documents (
    id INTEGER PRIMARY KEY,
    msg_id TEXT,             -- the emails.msg_id this came from
    origin TEXT,             -- attachment | link
    name TEXT,
    kind TEXT,               -- pdf|docx|xlsx|ics|image|pptx|text|html|file
    url TEXT,                -- the original link (NULL for attachments)
    final_url TEXT,          -- where the link ended up after redirects
    saved_path TEXT,         -- data/mail/<key>/NN_name — the openable original
    size INTEGER,
    text TEXT,               -- extracted content (may be empty for an image)
    status TEXT,             -- ok | failed:<why> | skipped:<why>
    created_at TEXT,
    UNIQUE(msg_id, origin, name, url)
);
-- The action items a summary found — the things a parent must DO. Kept as rows (not buried
-- in the summary JSON) so the home page can ask "what still needs me?" across every email,
-- and so a Done tick records WHO, the same shared-board contract as the checklist.
CREATE TABLE IF NOT EXISTS mail_actions (
    id INTEGER PRIMARY KEY,
    msg_id TEXT,
    idx INTEGER,             -- position within the email's action list (stable key)
    text TEXT,
    due TEXT,                -- YYYY-MM-DD or NULL
    kid TEXT,
    state TEXT DEFAULT 'open',   -- open | done | dismissed
    done_by TEXT,
    done_at TEXT,
    created_at TEXT,
    UNIQUE(msg_id, idx)
);
-- A sender the discovery pass thinks is family/school-relevant but that no rule covers yet.
-- A parent approves it into a real source (or dismisses it) at /mail/sources — so the sweep
-- gets more expansive over time WITHOUT silently turning 9,000 promotional emails into events.
CREATE TABLE IF NOT EXISTS mail_suggestions (
    sender TEXT PRIMARY KEY,     -- the From address
    display TEXT,                -- the From display name last seen
    sample_subject TEXT,
    score REAL,                  -- how school/kid-relevant it looked
    hits INTEGER,                -- how many messages in the window matched
    last_seen TEXT,
    status TEXT DEFAULT 'new'    -- new | approved | dismissed
);
-- When two calendars disagree on an event's time, nothing in the code can know which is
-- right — only a parent does. One row here settles the whole recurring series, and the
-- next ingest applies it instead of re-flagging 52 weeks. Written from the dashboard, so
-- resolving a conflict never needs a code change.
CREATE TABLE IF NOT EXISTS time_preferences (
    title_key TEXT PRIMARY KEY,   -- normalise_title() of the event
    prefer_calendar TEXT,         -- _calendar_name whose clock wins
    decided_by TEXT,
    decided_at TEXT
);
-- Who has each day a kid is out of school. The school-out days themselves are DERIVED
-- (days_off.py merges the feeds); this table holds only what a human decided, so a
-- re-ingest can never overwrite a parent's answer.
CREATE TABLE IF NOT EXISTS coverage_plan (
    day TEXT PRIMARY KEY,         -- YYYY-MM-DD
    coverage TEXT,                -- TBD | a parent's name | Both | family.COVERAGE_EXTRA
    sitter_id INTEGER,            -- sitters.id when coverage = Babysitter
    pto TEXT,                     -- none|requested|approved (a parent's employer system
                                  -- is not readable from here, so they record it)
    note TEXT,
    gcal_event_id TEXT,           -- set once an event exists; makes creation idempotent
    gcal_kind TEXT,               -- dayoff|pickup — a pickup is NOT a day off
    updated_at TEXT,
    updated_by TEXT
);
-- The babysitters in rotation. Nothing here is derivable from any feed; the parents
-- keep it. `active` = still in rotation (a sitter who moved away stays as history).
CREATE TABLE IF NOT EXISTS sitters (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    rate TEXT,                    -- free text: "$25/hr", "$20/hr, $30 after midnight"
    how_we_know TEXT,             -- neighbor, a teacher's daughter, care.com ...
    kids_ok TEXT,                 -- who they can take: Both | a kid's name
    availability TEXT,            -- free text: weeknights, not Fridays, school days ok
    notes TEXT,
    active INTEGER DEFAULT 1,
    sort INTEGER DEFAULT 0,       -- rotation order: who to call first
    updated_at TEXT,
    updated_by TEXT
);
-- The sheet a sitter needs: pediatrician, allergies, bedtimes, the door code. One row per
-- fact, grouped by section, values EMPTY until a parent fills them in -- a plausible
-- default in a pediatrician field is worse than a blank one.
CREATE TABLE IF NOT EXISTS sitter_info (
    id INTEGER PRIMARY KEY,
    section TEXT NOT NULL,
    label TEXT NOT NULL,
    value TEXT,
    sort INTEGER DEFAULT 0,
    updated_at TEXT,
    updated_by TEXT,
    UNIQUE(section, label)
);
-- Days a parent is already off work. Not derivable from anything here: it is an employer's
-- holiday schedule and it changes yearly. A quarter of a school year's closure days can be
-- these, and getting it wrong either burns PTO nobody needs or leaves a day uncovered.
-- A trip is the one thing where the details live in six different confirmation emails
-- and nowhere together. `trips` is the header; `trip_items` is every leg, booking, plan
-- and open question, one row each, so a new trip needs a seed script and no code change.
CREATE TABLE IF NOT EXISTS trips (
    slug TEXT PRIMARY KEY,        -- beach-2026-07
    name TEXT,
    destination TEXT,
    start_date TEXT,              -- YYYY-MM-DD
    end_date TEXT,
    party TEXT,                   -- who is going
    notes TEXT
);
-- status matters more than it looks: an item that is BOOKED and one that is merely
-- PLANNED read identically on a page that only lists them, and that is exactly how a
-- family arrives at an airport with no way to reach the hotel.
CREATE TABLE IF NOT EXISTS trip_items (
    id INTEGER PRIMARY KEY,
    trip TEXT,                    -- trips.slug
    day TEXT,                     -- YYYY-MM-DD, NULL = applies to the whole trip
    end_day TEXT,                 -- set for a stay that spans nights
    start_time TEXT,              -- HH:MM local
    end_time TEXT,
    kind TEXT,                    -- flight|stay|transport|activity|food|admin|money
    title TEXT,
    glance TEXT,                  -- the one line you want WITHOUT opening the row: seat
                                  -- numbers, the lockbox code, the price. Detail behind a
                                  -- click is fine for reading; it is useless at a gate.
    detail TEXT,
    location TEXT,
    confirmation TEXT,
    cost TEXT,                    -- free text: currencies and points both appear
    paid TEXT,                    -- what has actually been paid, when it differs
    -- LEGACY. Kept in sync from (plan, booking) by legacy_status() so anything reading
    -- this table directly still sees a sane value, but nothing in the app reads it.
    status TEXT DEFAULT 'booked', -- booked|open|decision|done
    open_reason TEXT,             -- why it is not settled — an open item with no reason
                                  -- is a nag; with one it is a next action
    deadline TEXT,                -- YYYY-MM-DD a decision expires
    url TEXT,
    source TEXT,                  -- the email/calendar entry it was read from
    -- The two questions one `status` column was being asked at once. Planning and
    -- booking are independent: the fireworks are free, nightly and unreservable (doing /
    -- none), the attraction pass is bought on the day (idea / none), the boat must be
    -- reserved before you fly (doing / todo). One column could only ever lie about one
    -- of them.
    plan TEXT DEFAULT 'doing',    -- idea|penciled|doing|dropped
    booking TEXT DEFAULT 'none',  -- none|todo|booked
    -- A part of day is a real answer, not a blank waiting to be filled in. "Doing it,
    -- some time in the afternoon" is the commonest shape of a family plan.
    part TEXT,                    -- morning|afternoon|evening
    duration_min INTEGER,
    -- Free-text `cost` keeps the nuance ("CA$18 adult, CA$9 child 4-12"); this pair is
    -- what lets a day add up. NULL means nobody priced it and it stays OUT of every
    -- total by name -- never defaulted to a plausible number.
    cost_est REAL,
    currency TEXT DEFAULT 'CAD',
    area TEXT,                    -- an area key from household.json — turns "10:00 and 10:30"
                                  -- into "20 min apart, 25 min of driving between them"
    -- Coordinates, so "how far is it" stops being a guess. Filled by geo.py from
    -- `location`; NEVER typed by hand and never defaulted. geo_status carries the third
    -- state that matters: 'ok' found it, 'nohit' the geocoder answered and had nothing,
    -- 'error' nobody could look. An error must never render as a distance of zero.
    lat REAL,
    lng REAL,
    geo_status TEXT,              -- ok|nohit|error|noaddress
    -- What to ASK the geocoder, when the human-readable `location` is a bad question.
    -- An airport terminal's name can resolve perfectly -- to a high school named after
    -- the same person, 30 km from the airport -- and another airport's name to an
    -- expressway in the next county. Both are valid results for the
    -- wrong place, which no error would ever reveal. The hint overrides the question,
    -- never the answer: the geocoder still does the finding.
    geo_hint TEXT,
    geo_query TEXT,               -- the string that was actually looked up
    geo_label TEXT,               -- what the geocoder said it found — the check that the
                                  -- id it returned is the place we asked for
    geo_at TEXT,
    -- Does this thing have a roof? The forecast is decoration without it: knowing
    -- Monday is 54% rain only changes a decision once you can also see that the only
    -- thing on Monday is outdoors. outdoor|indoor|covered|na -- 'na' for the rows
    -- weather cannot touch (paying for the car, bringing the car seats).
    shelter TEXT,
    -- The one place every other distance is measured FROM. Exactly one row per trip
    -- carries it; app.py refuses to compute a "from base" number when none does, rather
    -- than silently measuring from the first row it happens to see.
    is_base INTEGER DEFAULT 0,
    gcal_event_id TEXT,           -- set once it is on the calendar; makes a second press
                                  -- an update instead of a duplicate
    gcal_sig TEXT,                -- what was SENT. A green "on the calendar" that keeps
                                  -- saying so after you move the item is worse than no
                                  -- button at all, so the day compares this to now.
    updated_at TEXT,
    updated_by TEXT,
    sort INTEGER DEFAULT 100,
    UNIQUE(trip, title, day)
);
-- UNIQUE(trip, title, day) above cannot dedupe an undated row: in SQL, NULL <> NULL, so
-- every re-run of seed_trip.py inserted a fresh copy of all nine whole-trip rows. A
-- partial index is the only thing that makes it impossible rather than merely unlikely.
CREATE UNIQUE INDEX IF NOT EXISTS trip_items_undated
    ON trip_items(trip, title) WHERE day IS NULL;
-- A forecast is a MODEL and it is supposed to change, which makes it the opposite of
-- the geocode cache: it is stored WITH the time it was issued and re-fetched on a TTL.
-- `fetched_at` is not bookkeeping -- it is rendered, because a five-day-old forecast
-- shown as today's is the same failure as a stale calendar that looks healthy.
-- What the trip actually COST, read off the card statements after the fact. It sits
-- BESIDE trip_items (what things were expected to cost) and is never merged into it: an
-- estimate and a charge are different facts from different sources, and the whole point
-- of keeping both is being able to put one against the other.
--   amount is what the card BILLED, in card_currency (USD). That is the fact. The CAD
--   figure is derived from `fx` (Bank of Canada daily average for the posting date) and
--   is labeled approximate everywhere it renders -- the card's own rate is what was
--   actually charged and the statement does not show it.
--   A foreign-transaction fee is its own row pointing at its charge via fee_for, so the
--   fee total can be named instead of buried inside the merchant lines.
--   A pending row that is the same charge as a posted one is dup_of that row and stays
--   out of every total -- the bank shows both for a day and the sum is 2x until it drops.
--   scope says whose money question this row answers: 'trip' counts, 'home' is a charge
--   that posted during the trip but was not the trip (the Target run before you left),
--   'excluded' is a subscription or a bill. Home and excluded are counted BY NAME on the
--   page, never silently dropped.
CREATE TABLE IF NOT EXISTS trip_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip TEXT NOT NULL,
    key TEXT UNIQUE,              -- card|posted-or-pending|merchant|amount|ordinal
    posted TEXT,                  -- YYYY-MM-DD the card posted it; NULL while pending
    day TEXT,                     -- the day it actually happened, when somebody knows
    merchant TEXT NOT NULL,       -- the statement line, verbatim
    label TEXT,                   -- the human name: "Pizza place -- dinner"
    category TEXT,                -- see TRIP_EXPENSE_CATEGORIES
    amount REAL NOT NULL,
    card_currency TEXT DEFAULT 'USD',
    card TEXT,                    -- "...1234 Cash Rewards"
    status TEXT DEFAULT 'posted', -- posted|pending
    phase TEXT DEFAULT 'during',  -- before (paid ahead) | during
    scope TEXT DEFAULT 'trip',    -- trip|home|excluded
    fee_for INTEGER,              -- a foreign-transaction fee row -> its charge
    dup_of INTEGER,               -- a pending row that IS an already-posted row
    item_id INTEGER,              -- the trip_items row it settles, if any
    -- A difference from the plan that somebody has CHECKED and accepted. The variance
    -- stays visible in the plan-vs-actual table -- it is still what happened -- but it
    -- stops being flagged as something to chase. A page that keeps asking a question
    -- already answered is a page people stop reading.
    settled INTEGER DEFAULT 0,
    fx REAL,                      -- CAD per 1 USD on `posted` (or the last business day)
    fx_source TEXT,               -- "Bank of Canada daily avg 2026-08-28"
    note TEXT,
    source TEXT,                  -- where it was read from
    ordinal INTEGER DEFAULT 1,    -- two identical charges on one day are two rows
    updated_at TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS weather_daily (
    trip TEXT,
    day TEXT,                     -- YYYY-MM-DD, local to the destination
    code INTEGER,                 -- WMO code; NULL means not forecast, NOT clear
    temp_max REAL,                -- Fahrenheit: the family is American, the signs are not
    temp_min REAL,
    precip_in REAL,
    pop_max INTEGER,              -- % chance of precipitation. NULL is unknown, not dry.
    wind_kmh REAL,
    sunrise TEXT,
    sunset TEXT,
    fetched_at TEXT,
    PRIMARY KEY (trip, day)
);
-- The day figure cannot answer the question that actually matters. Monday is 54% for
-- the DAY, and the only outdoor thing on it is at 8pm -- whether 8pm is the wet part is
-- a different number, and this is where it lives.
CREATE TABLE IF NOT EXISTS weather_hourly (
    trip TEXT,
    day TEXT,
    hour TEXT,                    -- HH:00 local
    pop INTEGER,
    temp_f REAL,
    code INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (trip, day, hour)
);
CREATE TABLE IF NOT EXISTS company_holidays (
    day TEXT PRIMARY KEY,         -- YYYY-MM-DD
    label TEXT
);
-- A kid's WEEK at school: which special falls on which weekday (PE = wear sneakers,
-- library day = send the books back) plus the standing class rules that arrive ONCE in a
-- teacher's post and then live nowhere -- snack is daily except half days, a dismissal
-- change needs a message to the teacher AND a written note for the office. Class-app posts
-- often never arrive as email content (the mail is a pointer), so this is where they land.
-- weekday 0=Mon..6=Sun; -1 = a standing note not tied to a day (not NULL: SQLite's UNIQUE
-- treats NULLs as distinct, and the seed must be idempotent).
CREATE TABLE IF NOT EXISTS kid_routines (
    id INTEGER PRIMARY KEY,
    kid TEXT,
    weekday INTEGER,
    label TEXT,              -- "PE", "Library", "Snack"
    note TEXT,               -- "wear sneakers", "return library books"
    source TEXT,             -- where it came from: the post, or who typed it on the page
    sort INTEGER DEFAULT 100,
    created_at TEXT,
    UNIQUE(kid, weekday, label)
);
-- Geocoding and routing are network calls to somebody else's free service, so they are
-- cached -- but ONLY when they succeeded. A cached timeout is a permanent wrong answer
-- that looks exactly like a real one, which is the whole reason this cache stores no
-- failures: an error re-asks next time.
CREATE TABLE IF NOT EXISTS geo_cache (
    query TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    label TEXT,
    fetched_at TEXT
);
-- One row per ordered pair. Driving is not symmetric (one-ways, divided highways), so
-- (a,b) and (b,a) are separate rows and neither is inferred from the other.
CREATE TABLE IF NOT EXISTS drive_cache (
    origin TEXT,                  -- "lat,lng" rounded to 5dp
    dest TEXT,
    meters REAL NOT NULL,
    seconds REAL NOT NULL,
    source TEXT,                  -- osrm|haversine — a straight line is NOT a drive and
                                  -- every reader of this table must be able to tell
    fetched_at TEXT,
    PRIMARY KEY (origin, dest)
);
-- A gathering is anything the house plans and hosts: a kid's birthday party first, a
-- family visit, a small New Year's thing. The header is deliberately thin; the PLAN
-- (gathering_items, one row per thing to decide or do, grouped by workstream) and the
-- GUEST LIST (gathering_guests) are where the load lives. Two dates on purpose: the
-- occasion (Leo's birthday is Oct 2 whatever else is true) and the party date, which
-- is itself one of the first decisions and may be empty for weeks.
CREATE TABLE IF NOT EXISTS gatherings (
    slug TEXT PRIMARY KEY,        -- leo-birthday-2026
    name TEXT,
    kind TEXT,                    -- kid_birthday|family_visit|party
    honoree TEXT,                 -- a kid's name for a birthday, else NULL
    occasion_date TEXT,           -- YYYY-MM-DD: the birthday / the anchor date
    event_date TEXT,              -- the party itself; NULL until chosen
    end_date TEXT,                -- a visit spans days
    start_time TEXT,              -- HH:MM
    end_time TEXT,
    venue TEXT,
    headcount_goal TEXT,          -- "12 kids + parents" -- free text, it is a plan not a count
    budget REAL,
    notes TEXT,
    status TEXT DEFAULT 'planning',   -- planning|done|cancelled
    went_well TEXT,               -- the post-mortem, filled after the day
    do_differently TEXT,
    actual_cost REAL,
    attended TEXT,                -- who actually came, free text
    cloned_from TEXT,             -- slug of the gathering this was started from
    gcal_event_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    updated_by TEXT
);
-- status is one question, "is this settled": open -> decided (we know the answer but
-- something still has to happen: the deposit, the order) -> done. skipped is a real
-- answer too ("no entertainer this year"), never a deleted row.
CREATE TABLE IF NOT EXISTS gathering_items (
    id INTEGER PRIMARY KEY,
    gathering TEXT,               -- gatherings.slug
    workstream TEXT,              -- Venue|Food|Cake|Guests & invitations|...
    title TEXT,
    status TEXT DEFAULT 'open',   -- open|decided|done|skipped
    owner TEXT,                   -- a parent's name | NULL = either
    due TEXT,                     -- YYYY-MM-DD
    decision TEXT,                -- the answer: "Bounce U, 2-4pm", "Costco sheet cake"
    cost REAL,
    paid INTEGER DEFAULT 0,
    sort INTEGER DEFAULT 0,
    updated_at TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS gathering_guests (
    id INTEGER PRIMARY KEY,
    gathering TEXT,
    name TEXT,                    -- "Liam (Ava's class)" or "The Cohens"
    household TEXT,               -- groups a kid with the parent who RSVPs
    adults INTEGER DEFAULT 0,
    kids INTEGER DEFAULT 1,
    rsvp TEXT DEFAULT 'not_asked',  -- not_asked|invited|yes|no|maybe
    contact TEXT,
    notes TEXT,
    sort INTEGER DEFAULT 0,
    updated_at TEXT,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_gathering_items ON gathering_items (gathering, workstream, sort);
CREATE INDEX IF NOT EXISTS idx_gathering_guests ON gathering_guests (gathering, sort);
"""

CHECKLIST_CATEGORIES = ["supplies", "admin", "activity", "health", "other"]
CHECKLIST_STATUSES = ["open", "done", "blocked", "na"]
TRIP_KINDS = ["flight", "stay", "transport", "activity", "food", "admin", "money"]
# option/skipped are a different KIND of thing from open: an open item is an
# obligation you have not discharged, an option is a candidate you may never want.
# Counting them together would push "still open" from 4 to 14 and destroy the one
# number on the page that is supposed to mean something.
TRIP_STATUSES = ["booked", "open", "decision", "done", "option", "skipped"]

# The two axes that replaced it.
TRIP_PLANS = ["idea", "penciled", "doing", "dropped"]
TRIP_BOOKINGS = ["none", "todo", "booked"]
TRIP_PARTS = ["morning", "afternoon", "evening"]
# Coarse on purpose: it groups a day by direction. It used to be phrased as TIMES
# ("10 min north"), which stopped being defensible on 2026-08-22 when geo.py started
# rendering a measured drive time in the chip right next to it -- the bucket said 10
# minutes beside a measured 3. Directions here, minutes from the measurement.
# A household that plans by area names its buckets in household.json as
# "trip_areas": {"key": "Label"}. With none, items simply carry no area.
TRIP_AREAS = dict(family.CONFIG.get("trip_areas") or {})
# Whether the weather can spoil it. 'covered' is its own answer and not a hedge: the
# hotel pool is indoors, the boat is open-air but you are getting soaked anyway, and a
# tunnel behind a waterfall is the driest thing on the trip. Lumping those into one
# "indoor-ish" bucket would lose exactly the distinction a rainy morning needs.
TRIP_SHELTERS = {
    "outdoor": "Outdoors",
    "covered": "Mostly under cover",
    "indoor": "Indoors",
    "na": "Weather does not apply",
}
PART_LABELS = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}

# The ledger's categories. Fees are a category of their own so the cost of putting a
# trip on the wrong card is a number on the page, not a rounding error inside "food".
TRIP_EXPENSE_CATEGORIES = {
    "food": "Food & drink",
    "attractions": "Attractions",
    "lodging": "Lodging",
    "flights": "Flights",
    "car": "Rental car & parking",
    "fuel": "Gas",
    "shopping": "Shopping & souvenirs",
    "fee": "Foreign transaction fees",
    "other": "Other",
}
TRIP_EXPENSE_SCOPES = {
    "trip": "The trip",
    "home": "Home, not the trip",
    "excluded": "Excluded (bill / subscription)",
}

# Where an untimed item sorts into the day. It sits half a step after anything genuinely
# clocked at that hour, and an item with no part at all sorts to the end.
PART_ANCHOR = {"morning": 8 * 60, "afternoon": 13 * 60, "evening": 18 * 60}


def legacy_status(plan: str, booking: str) -> str:
    """Keep the old single `status` column truthful for anything reading the DB directly.

    Nothing in the app reads it any more -- but a column that silently stops updating is
    a trap for the next person who opens the database, so every write refreshes it.
    """
    if plan == "dropped":
        return "skipped"
    if plan == "idea":
        return "option"
    if booking == "booked":
        return "booked"
    if booking == "todo":
        return "decision" if plan == "penciled" else "open"
    return "done"


# How the single column is read apart into the two. `open` maps to doing, not to
# penciled: the rental car IS happening, it is the BOOKING that is outstanding -- which
# is the whole distinction the split exists to make.
_PLAN_FROM_STATUS = {"booked": "doing", "done": "doing", "open": "doing",
                     "decision": "penciled", "option": "idea", "skipped": "dropped"}
_BOOKING_FROM_STATUS = {"booked": "booked", "done": "none", "open": "todo",
                        "decision": "todo", "option": "none", "skipped": "none"}

# Expected sources and their seasonality. A school newsletter pauses in summer; a preschool
# may run year-round (camp variant); teacher/bus/district traffic follows the school year.
# Each row: key, name, kid, sender_match, subject_match, cadence, active_months,
#           enabled (0/1), keywords (list — a message from this sender counts only if the
#           subject OR body carries one; [] means the whole sender counts).
# The starting set comes from the household file (family.SOURCES); a parent edits it later
# at /mail/sources. Two lessons from running this that belong in anyone's household file:
#   - A class app often sends the school's notices AND its own upsell from one domain.
#     Match the notice address (parent@...), not the bare domain, or keyword-gate it.
#   - Use all twelve months for anything back-to-school: that mail lands in AUGUST, and a
#     Sep-Jun window reports it as "seasonal break -- silence expected" exactly when it
#     matters most.
_GENERIC_SCHOOL_KEYWORDS = [
    "school", "class", "teacher", "principal", "dismissal", "closed", "closure",
    "camp", "swim", "lesson", "registration", "register", "enroll", "form", "permission",
    "supply", "supplies", "conference", "back to school", "orientation", "pta", "pto",
    "field trip", "picture day", "report card", "lunch", "breakfast", "bus", "drop-off",
    "pickup", "pick-up", "kindergarten", "pre-k", "prek", "due", "deadline", "rsvp",
]
# Generic words plus the household's own: every kid's name and every school's name.
SCHOOL_KEYWORDS = list(dict.fromkeys(_GENERIC_SCHOOL_KEYWORDS + family.school_keywords()))
DEFAULT_SOURCES = list(family.SOURCES)


# Columns added after the first DBs were created. CREATE TABLE IF NOT EXISTS silently
# leaves an existing table alone, so a schema edit alone reaches new installs only —
# the live DB needs the ALTER.
MIGRATIONS = {
    # A payment is an ACTIVITY with an ORGANIZATION, and "due" is a rule as often as a
    # date ("Mondays", "the 1st") -- found adding a weekly swim lesson at the rec center.
    "payments": [
        ("activity", "TEXT"),
        ("organization", "TEXT"),
        ("due_rule", "TEXT"),
    ],
    "trip_expenses": [
        ("settled", "INTEGER DEFAULT 0"),
    ],
    "trip_items": [
        ("lat", "REAL"),
        ("lng", "REAL"),
        ("geo_status", "TEXT"),
        ("geo_hint", "TEXT"),
        ("geo_query", "TEXT"),
        ("geo_label", "TEXT"),
        ("geo_at", "TEXT"),
        ("is_base", "INTEGER"),
        ("shelter", "TEXT"),
        ("glance", "TEXT"),
        ("plan", "TEXT"),
        ("booking", "TEXT"),
        ("part", "TEXT"),
        ("duration_min", "INTEGER"),
        ("cost_est", "REAL"),
        ("currency", "TEXT"),
        ("area", "TEXT"),
        ("gcal_event_id", "TEXT"),
        ("gcal_sig", "TEXT"),
        ("updated_at", "TEXT"),
        ("updated_by", "TEXT"),
    ],
    "events": [
        ("start_time", "TEXT"),
        ("end_time", "TEXT"),
        ("category", "TEXT"),
        ("conflict", "TEXT"),
        ("last_seen_at", "TEXT"),
    ],
    "emails": [
        ("summary", "TEXT"),        # JSON: the Fable-5 briefing over body + all documents
        ("summarized_at", "TEXT"),
        ("swept_at", "TEXT"),       # when depth (documents) last ran for this message
    ],
    "coverage_plan": [
        ("confirmation", "TEXT DEFAULT 'proposed'"),
        ("sitter_id", "INTEGER"),   # sitters.id when coverage = Babysitter
    ],
    "sources": [
        ("enabled", "INTEGER"),     # 1 = scanned; a parent can switch a source off
        ("keywords", "TEXT"),       # JSON list — for a broad sender, the subject/body words
                                    # that make a message relevant (else the whole sender counts)
        ("origin", "TEXT"),         # seed | discovered — where the rule came from
    ],
}


# The email feeds are school feeds — every event they can produce is a kid's thing.
# Leaving their category NULL once dumped 140 school events into "Other" on the dashboard,
# which is the pile the categories exist to prevent.
#   - every source the household file configures (family.SOURCES)
#   - "pasted": a note a parent pasted or uploaded at /mail
#   - "v0_import": rows carried over from the first version's JSON
#   - "disc_<domain>": a discovered sender a parent approved at /mail/sources. The key is
#     minted there from the sender's domain, so it can't be listed ahead of time; the
#     prefix is the contract.
# Calendar ("gcal"), gatherings, Ask and anything else are NOT email feeds and get None.
DISCOVERED_PREFIX = "disc_"
_FIXED_EMAIL_SOURCES = ("v0_import", "pasted")
SCHOOL_SOURCES = tuple(dict.fromkeys([s[0] for s in family.SOURCES] + list(_FIXED_EMAIL_SOURCES)))


def _school_source_keys() -> tuple:
    """Re-read on every call so a family.reload() (a test, a household edit) is honored."""
    return tuple(dict.fromkeys([s[0] for s in family.SOURCES] + list(_FIXED_EMAIL_SOURCES)))


def category_for_source(source: str) -> str | None:
    """Category a row gets from WHERE it came from. gcal is the exception — it spans the
    whole household, so gcal.classify_category reads the title instead."""
    if not source:
        return None
    if source in _school_source_keys() or source.startswith(DISCOVERED_PREFIX):
        return "kids_school"
    return None


def split_status(con: sqlite3.Connection) -> int:
    """Fill plan/booking from the legacy status for any row that has neither yet.

    Called by the migration AND by seed_trip, because a row inserted after the migration
    ran would otherwise carry NULLs -- which render as a blank chip and make every booked
    item offer a "needs booking" button.
    """
    todo = con.execute("SELECT id, status FROM trip_items WHERE plan IS NULL "
                       "OR booking IS NULL").fetchall()
    for r in todo:
        st = r["status"] or "booked"
        con.execute("UPDATE trip_items SET plan=COALESCE(plan,?), booking=COALESCE(booking,?), "
                    "currency=COALESCE(currency,'CAD') WHERE id=?",
                    (_PLAN_FROM_STATUS.get(st, "doing"),
                     _BOOKING_FROM_STATUS.get(st, "none"), r["id"]))
    return len(todo)


def _migrate(con: sqlite3.Connection) -> list[str]:
    applied = []
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    # Seed sources created before the enabled/origin columns existed have NULL enabled,
    # which the sweep would read as "off". Scoped to NULL so a parent's later toggle is safe.
    have_src = {r["name"] for r in con.execute("PRAGMA table_info(sources)")}
    if "enabled" in have_src:
        n = con.execute("UPDATE sources SET enabled=1 WHERE enabled IS NULL").rowcount
        con.execute("UPDATE sources SET origin='seed' WHERE origin IS NULL")
        if n:
            applied.append(f"enabled {n} existing sources")
    # Rows written before the write path knew about categories. Scoped to NULL so it can
    # never overwrite a category anything else has since decided.
    keys = _school_source_keys()
    n = con.execute(
        f"UPDATE events SET category='kids_school' WHERE category IS NULL AND (source IN "
        f"({','.join('?' * len(keys))}) OR source LIKE ?)",
        (*keys, DISCOVERED_PREFIX + "%")).rowcount
    if n:
        applied.append(f"backfilled {n} school-source categories")
    # trip_items rows written before planning and booking were separate questions. The
    # backfill is scoped to plan IS NULL so it runs once and can never overwrite a state
    # somebody has since set on the page.
    n_split = split_status(con)
    if n_split:
        applied.append(f"split {n_split} trip items into plan + booking")
    if applied:
        con.commit()
    return applied


# Schema + migrations run ONCE per process, on the first connect. They used to run on
# EVERY connect -- and _migrate/_ensure_sources issue UPDATE/INSERT statements that take
# the write lock even when they change nothing, so any page render that coincided with
# ingest.py's write transaction (the nightly at 4 PM, the mail check, a scan) died with
# "database is locked" after the default 5s. Found 2026-09-04 when the first Scan
# everything took the site down with it.
_READY_FOR: set[str] = set()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    # 30s, not 5: a writer mid-transaction is normal here, not a fault.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    key = str(DB_PATH)
    if key not in _READY_FOR:
        # WAL: readers never wait on a writer. Persistent once set; harmless to re-set.
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # a locked file right now -- the next first-connect will set it
        con.executescript(SCHEMA)
        _migrate(con)
        _ensure_sources(con)
        _fix_seed_sources(con)
        _READY_FOR.add(key)
    return con


def _ensure_sources(con: sqlite3.Connection) -> None:
    for key, name, kid, smatch, submatch, cadence, months, enabled, keywords in DEFAULT_SOURCES:
        con.execute(
            "INSERT OR IGNORE INTO sources (key, name, kid, sender_match, subject_match, "
            "cadence, active_months, enabled, keywords, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,'seed')",
            (key, name, kid, smatch, submatch, cadence, json.dumps(months),
             enabled, json.dumps(keywords)),
        )
    con.commit()


# A seed rule that turned out to be WRONG can't be fixed by editing DEFAULT_SOURCES alone:
# _ensure_sources is INSERT OR IGNORE, so every existing DB keeps the bad rule forever and
# only a fresh install gets the fix. Each entry is (key, column, superseded_value,
# new_value). The update fires ONLY when the row still holds the exact superseded value and
# is still origin='seed' -- so a rule a parent has edited at /mail/sources is never
# clobbered, and re-running is a no-op.
#
# Example shape: ("elementary", "active_months", "[9, 10, 11, 12, 1, 2, 3, 4, 5, 6]",
#                 "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]")
SEED_FIXES: list[tuple[str, str, str, str]] = []

_FIXABLE_COLUMNS = {"sender_match", "name", "active_months", "keywords"}


def _fix_seed_sources(con: sqlite3.Connection) -> list[str]:
    """Apply SEED_FIXES to seed rows that still carry the exact superseded value."""
    changed = []
    for key, col, was, now_val in SEED_FIXES:
        if col not in _FIXABLE_COLUMNS:      # the column name is interpolated below
            raise ValueError(f"SEED_FIXES: {col!r} is not a fixable column")
        cur = con.execute(
            f"UPDATE sources SET {col}=? WHERE key=? AND {col}=? "
            "AND COALESCE(origin,'seed')='seed'", (now_val, key, was))
        if cur.rowcount:
            changed.append(f"{key}.{col}: {was} -> {now_val}")
    if changed:
        con.commit()
    return changed


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- mail sweep: documents, action items, discovery suggestions --------------------

def save_documents(con: sqlite3.Connection, msg_id: str, docs: list[dict]) -> None:
    """Replace the stored documents for a message with a fresh sweep. Idempotent: a
    reprocess never leaves stale rows or duplicates behind."""
    con.execute("DELETE FROM mail_documents WHERE msg_id=?", (msg_id,))
    for d in docs:
        con.execute(
            "INSERT OR IGNORE INTO mail_documents (msg_id, origin, name, kind, url, "
            "final_url, saved_path, size, text, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (msg_id, d.get("origin"), d.get("name"), d.get("kind"), d.get("url"),
             d.get("final_url"), d.get("saved_path"), d.get("size", 0),
             (d.get("text") or "")[:200000], d.get("status"), now()))
    con.execute("UPDATE emails SET swept_at=? WHERE msg_id=?", (now(), msg_id))


def save_summary(con: sqlite3.Connection, msg_id: str, summary: dict, kid: str | None) -> int:
    """Store the briefing on the email row and (re)write its action items. Returns the
    number of NEW action items. Match existing wording before assigning a new slot:
    reordering a summary must not transfer ownership or Done to a different ask.
    Ambiguous rewordings become new asks; touched old asks remain for human review."""
    con.execute("UPDATE emails SET summary=?, summarized_at=? WHERE msg_id=?",
                (json.dumps(summary), now(), msg_id))
    new = 0
    items = summary.get("action_items") or []
    old = [dict(r) for r in con.execute('SELECT * FROM mail_actions WHERE msg_id=? ORDER BY idx', (msg_id,))]
    used = set()
    next_idx = max((r['idx'] for r in old), default=-1) + 1
    def identity(text):
        return ' '.join(re.findall(r'\w+', (text or '').lower()))
    for i, a in enumerate(items):
        text = (a.get("text") or "").strip()
        if not text:
            continue
        due = (a.get("due") or "").strip() or None
        akid = (a.get("kid") or "").strip() or kid or "Both"
        match = next((r for r in old if r['id'] not in used and identity(r['text']) == identity(text)
                      and (r['kid'] or akid) == akid), None)
        existed = match is not None
        if match:
            i = match['idx']
            used.add(match['id'])
        else:
            i = next_idx
            next_idx += 1
        con.execute(
            "INSERT INTO mail_actions (msg_id, idx, text, due, kid, state, created_at) "
            "VALUES (?,?,?,?,?, 'open', ?) "
            "ON CONFLICT(msg_id, idx) DO UPDATE SET text=excluded.text, due=excluded.due, "
            "kid=excluded.kid",
            (msg_id, i, text, due, akid, now()))
        if not existed:
            new += 1
    # An item removed from a re-summary (the model dropped it) that a parent never touched
    # is stale — drop it. One a parent acted on is kept as a record.
    for r in old:
        if r['id'] not in used:
            con.execute("DELETE FROM mail_actions WHERE id=? AND state='open' AND NOT EXISTS "
                        "(SELECT 1 FROM household_tasks WHERE key='mail:' || mail_actions.id)", (r['id'],))
    return new


# Only connective words and the family's own names/places. "school", "day", "first" STAY --
# they are exactly the signal shared between "First Day of School / PTA Coffee" and "First
# day of school -- Maple Street Elementary".
_GENERIC_TITLE_STOP = {"the", "and", "for", "with", "from", "at", "of", "in", "on", "to",
                       "a", "an", "st", "elementary", "pm", "am"}
_TITLE_STOP = _GENERIC_TITLE_STOP | set(family.NAME_TOKENS)


def _title_words(title: str) -> set:
    out = set()
    for w in (title or "").split():
        w = w.strip(".,:;()\"'/-#").lower()
        if w and any(ch.isalnum() for ch in w) and w not in _TITLE_STOP:
            out.add(w)
    return out


def already_on_board(con: sqlite3.Connection, kid: str, day: str, kind: str, title: str) -> bool:
    """Is this dated item already on the board under another source's wording?

    A teacher's "Early Dismissal at 1:15 PM" on 9/4 and the district feed's "Early dismissal
    (1:15)" are one day, not two. gcal.dedupe only collapses within a calendar pull, so a
    note's dates need their own check before landing (the principal's September post put 7
    duplicates on the board, 2026-09-02). Rules: a closure / early dismissal / meeting on the
    same day for the same kid is the same day; anything else is a duplicate when the titles
    share two significant words or half of the new title's words."""
    rows = con.execute(
        "SELECT type, title FROM events WHERE status='active' AND event_date=? "
        "AND (kid=? OR kid='Both' OR ?='Both') AND COALESCE(category,'kids_school')='kids_school'",
        (day, kid, kid)).fetchall()
    if not rows:
        return False
    if kind in ("closure", "early_dismissal", "meeting") and any(r["type"] == kind for r in rows):
        return True
    mine = _title_words(title)
    if not mine:
        return False
    for r in rows:
        shared = mine & _title_words(r["title"])
        # Two shared words, or every word of a short title -- so "Picture Day" is not the
        # same thing as "First day of school" just because both say "day".
        if len(shared) >= 2 or (mine and shared == mine):
            return True
    return False


def open_actions(con: sqlite3.Connection, limit: int = 50,
                 today: str | None = None) -> list[sqlite3.Row]:
    """Action items still needing a parent, ordered by what actually needs doing NOW:
    upcoming due-dates first (soonest first), then undated items, then PAST-DUE last (newest
    first). Leading with a three-week-old camp note — moot once school started — is how a
    "Needs you" list stops being read. Past-due items are kept (a missed deadline is still
    information), just not at the top."""
    today = today or datetime.now().date().isoformat()
    # An undated ask that ARRIVED in the last few days ("orange slip, return asap") is the
    # most urgent thing on the list, not the least -- with 160 open items, an undated one
    # otherwise sits under every dated item and never reaches the home page (found with
    # the first pasted teacher note). Fresh + undated leads; after that window it
    # drops back to the undated pile.
    fresh_since = (datetime.fromisoformat(today) - timedelta(days=3)).date().isoformat()
    return con.execute(
        "SELECT a.*, e.subject, e.sender, e.source, "
        "  CASE WHEN a.due IS NULL AND substr(a.created_at,1,10) >= ? THEN 0 "
        "       WHEN a.due IS NULL THEN 1 "
        "       WHEN a.due < ? THEN 2 ELSE 0 END AS bucket "
        "FROM mail_actions a JOIN emails e ON e.msg_id = a.msg_id "
        "WHERE a.state='open' "
        "ORDER BY bucket ASC, "
        "  a.due IS NOT NULL, "                         # in bucket 0: fresh undated first
        "  CASE WHEN a.due < ? THEN a.due END DESC, "   # past-due: most recent first
        "  a.due ASC, a.created_at DESC "               # upcoming: soonest first
        "LIMIT ?", (fresh_since, today, today, limit)).fetchall()


def record_suggestion(con: sqlite3.Connection, sender: str, display: str,
                      subject: str, score: float) -> None:
    """A sender the discovery pass flagged. Never overwrites a parent's approve/dismiss."""
    row = con.execute("SELECT status, hits FROM mail_suggestions WHERE sender=?",
                      (sender,)).fetchone()
    if row and row["status"] in ("approved", "dismissed"):
        con.execute("UPDATE mail_suggestions SET hits=hits+1, last_seen=? WHERE sender=?",
                    (now(), sender))
        return
    con.execute(
        "INSERT INTO mail_suggestions (sender, display, sample_subject, score, hits, "
        "last_seen, status) VALUES (?,?,?,?,1,?, 'new') "
        "ON CONFLICT(sender) DO UPDATE SET hits=hits+1, "
        "score=MAX(score, excluded.score), display=excluded.display, "
        "sample_subject=CASE WHEN excluded.score>score THEN excluded.sample_subject "
        "ELSE sample_subject END, last_seen=excluded.last_seen",
        (sender, display, subject, score, now()))


def touch_source(con: sqlite3.Connection, key: str, seen_date: str) -> None:
    """Advance a source's last_seen high-water mark (never move it backwards)."""
    con.execute(
        "UPDATE sources SET last_seen = MAX(COALESCE(last_seen, ''), ?) WHERE key = ?",
        (seen_date, key),
    )


def source_status(row: sqlite3.Row, today: datetime | None = None) -> dict:
    """Seasonality-aware health for one source row.

    OK       — seen within the expected window
    quiet    — outside active months (e.g. Wednesday Folder in July): silence is normal
    missing  — inside active months and overdue
    unknown  — never seen
    """
    today = today or datetime.now()
    months = json.loads(row["active_months"])
    in_season = today.month in months
    last_seen = row["last_seen"]
    window_days = {"weekly": 10, "monthly": 45}.get(row["cadence"], 9999)

    if not in_season:
        return {"status": "quiet", "label": "Seasonal break — silence expected"}
    if not last_seen:
        return {"status": "unknown", "label": "Never seen yet"}
    days = (today - datetime.strptime(last_seen, "%Y-%m-%d")).days
    if days > window_days:
        return {"status": "missing", "label": f"Overdue — last seen {last_seen} ({days}d ago)"}
    return {"status": "ok", "label": f"Last seen {last_seen}"}
