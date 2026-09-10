import os
import json
import base64
import re
import html
import threading
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from flask import Flask, request, redirect, session, jsonify
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import anthropic
import requests


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "temporary-insecure-key")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_raw_client_config = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
CLIENT_CONFIG = json.loads(_raw_client_config) if _raw_client_config else None
REDIRECT_URI = os.environ.get("REDIRECT_URI")

TOKEN_FILE = "gmail_token.json"

MY_NAME = "Fernandez G."
SENDER_EMAIL = "ismenia.keck@wiener-staatsballett.at"
SCHEDULE_FILENAME_HINT = "ballett-pp"
CAST_LIST_SUBJECT = "Cast list"
LABEL_NAME = "ScheduleBotProcessed"


# ============================================================
# BACKGROUND JOB STATUS
# ============================================================

JOB_LOCK = threading.Lock()

JOB_STATE = {
    "running": False,
    "job_id": None,
    "stage": "idle",
    "message": "Noch kein Lauf gestartet.",
    "dry_run": None,
    "started_at": None,
    "finished_at": None,
    "schedule_file": None,
    "cast_files": [],
    "digest": None,
    "error": None,
}

STAGE_LABELS = {
    "idle": "Bereit",
    "starting": "Starte…",
    "finding_schedule": "Suche Wochenplan…",
    "finding_cast": "Suche aktuelle Cast Lists…",
    "reading_schedule": "Claude liest den Wochenplan…",
    "reading_cast": "Claude liest deine Rollen…",
    "building": "Deine Proben werden ausgewählt…",
    "sending": "WhatsApp wird gesendet…",
    "done": "Fertig ✅",
    "no_schedule": "Kein neuer Wochenplan",
    "error": "Fehler ❌",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def reset_job_state(job_id, dry_run):
    with JOB_LOCK:
        JOB_STATE.clear()
        JOB_STATE.update(
            {
                "running": True,
                "job_id": job_id,
                "stage": "starting",
                "message": "Bot wurde gestartet.",
                "dry_run": dry_run,
                "started_at": utc_now(),
                "finished_at": None,
                "schedule_file": None,
                "cast_files": [],
                "digest": None,
                "error": None,
            }
        )


def update_job(job_id, **kwargs):
    with JOB_LOCK:
        if JOB_STATE.get("job_id") != job_id:
            return
        JOB_STATE.update(kwargs)


def get_job_state():
    with JOB_LOCK:
        state = dict(JOB_STATE)
        state["cast_files"] = list(JOB_STATE.get("cast_files", []))
        return state


# ============================================================
# CLAUDE STRUCTURED OUTPUT SCHEMAS
# ============================================================

SCHEDULE_TOOL = {
    "name": "submit_schedule",
    "description": "Submit the complete extracted weekly ballet schedule.",
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string"},
                        "date": {"type": "string"},
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "studio": {"type": "string"},
                                    "start": {"type": "string"},
                                    "end": {"type": "string"},
                                    "piece": {"type": "string"},
                                    "dancers": {"type": "string"},
                                    "staff": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": [
                                    "studio",
                                    "start",
                                    "end",
                                    "piece",
                                    "dancers",
                                    "staff",
                                    "notes",
                                ],
                            },
                        },
                    },
                    "required": ["day", "date", "rows"],
                },
            }
        },
        "required": ["days"],
    },
}

CAST_TOOL = {
    "name": "submit_cast_roles",
    "description": "Submit Fernandez G.'s current roles.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ballets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ballet": {"type": "string"},
                        "schedule_names": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string"},
                                    "category": {"type": "string"},
                                    "reserve": {"type": "boolean"},
                                },
                                "required": ["role", "category", "reserve"],
                            },
                        },
                    },
                    "required": ["ballet", "schedule_names", "roles"],
                },
            }
        },
        "required": ["ballets"],
    },
}


# ============================================================
# CLAUDE PROMPTS
# ============================================================

SCHEDULE_PROMPT = '''
Read the attached Wiener Staatsballett weekly rehearsal schedule PDF.

The PDF is a VISUAL TABLE with normally one page per day.

Your ONLY task is extraction.
Do NOT decide whether Fernandez G. participates.

CRITICAL RULES:

1. Process ONE PAGE AT A TIME.
2. Process ONE COLUMN AT A TIME from TOP to BOTTOM.
3. NEVER combine information from different columns.
4. One output row must represent ONE visually continuous timed block.
5. studio, start, end, piece, dancers, staff and notes must all belong
   to that SAME block.
6. Never move information horizontally from a neighboring column.
7. Carefully distinguish dancer names, staff names, teacher/pianist names,
   notes, Gäste, Sonstiges, and side-room information.
8. Extract all normal Training entries and rehearsals.
9. Side rooms are important. If a named room appears directly above a
   rehearsal, use the named room.

Examples:
Hilverdingsaal
Wiesenthalsaal
Elsslersaal
Hankasaal

10. Normalize:
Ballettsaal 1 -> BS1
Ballettsaal 2 -> BS2
Ballettsaal 3 -> BS3

11. Preserve dancer calls faithfully, including:
Entire Cast
Alle Solo Damen & Herren
Alle Solodamen & Herren verfügbar
Solo Dame
Solo Damen & Herren
all available Solo Da. & Herr.
specific dancer names
Variation numbers
headcount groups

12. Preserve notes faithfully, including:
ohne ...
Bes. dates
ab HH:MM
bis HH:MM
n. Mög.
performance-cast restrictions
personal dancer time restrictions

13. TRAINING STAFF IS MANDATORY.
For every Training block, copy the teacher/pianist line printed directly
under the Training title into "staff".
Never leave staff blank when those names are visibly printed.

14. Do NOT put information from Gäste or Sonstiges into a neighboring
rehearsal.

15. A Sonstiges item such as "SU Alosa bis 15:00" is NOT a personal time
restriction for Fernandez G.

16. If the rehearsal itself says "Fernandez G., Mitsumori bis 13:25",
preserve that phrase inside that rehearsal.

17. If a field is genuinely empty, use an empty string.
18. Never invent a time, person, room, note or activity.

FINAL CHECK FOR EACH ROW:
correct day
correct room
correct start
correct end
correct activity
correct dancer call
correct staff
correct notes
nothing copied from neighboring columns

Then call submit_schedule exactly once with the COMPLETE schedule.
'''

CAST_PROMPT = f'''
Read all attached Wiener Staatsballett Cast List PDFs.

All PDFs belong to ONE current Cast List email.
Every attached PDF is currently relevant.

Find every ballet in which:
{MY_NAME}
appears.

For every relevant ballet:

1. Identify the exact ballet title.
2. Return useful schedule_names.

Examples:
Divertimento No. 15 -> ["Divertimento", "Divertimento No. 15"]
Rhapsody -> ["Rhapsody"]
Nijinsky -> ["Nijinsky"]

3. Find EVERY printed role belonging to Fernandez G.
4. Determine category from the visual hierarchy.

Example:
Solo Damen
Variation 6
Avraam, Liz, Fernandez G.

means:
role = "Variation 6"
category = "Solo Dame"

If the table says:
Solo Dame
Trenary | Fernandez G. | Fernandes

then:
role = "Solo Dame"
category = "Solo Dame"

5. Under Solo Herren: category = "Solo Herr"
6. Named character: category = "Named Role"
7. Group or ensemble assignment: category = "Group/Ensemble"
8. reserve=true only when Fernandez G. is explicitly Reserve/Cover.
9. Return all distinct roles belonging to Fernandez G.
10. Ignore ballets where Fernandez G. does not appear.
11. Never invent roles.

Then call submit_cast_roles exactly once.
'''


# ============================================================
# BASIC HELPERS
# ============================================================

def walk_parts(part):
    yield part
    for child in part.get("parts", []):
        yield from walk_parts(child)


def decode_gmail_data(data):
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def norm(text):
    text = (text or "").lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9äöüß.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_studio(studio):
    value = (studio or "").strip()
    n = norm(value)
    if "ballettsaal 1" in n or n == "bs1":
        return "BS1"
    if "ballettsaal 2" in n or n == "bs2":
        return "BS2"
    if "ballettsaal 3" in n or n == "bs3":
        return "BS3"
    return value


def first_staff_name(staff):
    staff = (staff or "").strip()
    if not staff:
        return ""
    return staff.split("/")[0].strip()


def minutes(time_text):
    try:
        hour, minute = map(int, time_text.split(":"))
        return hour * 60 + minute
    except Exception:
        return 99999


def short_day(day):
    mapping = {
        "montag": "Mo",
        "mo": "Mo",
        "dienstag": "Di",
        "di": "Di",
        "mittwoch": "Mi",
        "mi": "Mi",
        "donnerstag": "Do",
        "do": "Do",
        "freitag": "Fr",
        "fr": "Fr",
        "samstag": "Sa",
        "sa": "Sa",
        "sonntag": "So",
        "so": "So",
    }
    return mapping.get(norm(day), day)


def short_date(date):
    if not date:
        return ""
    match = re.search(r"(\d{1,2})\.(\d{1,2})", date)
    if not match:
        return date
    return f"{int(match.group(1)):02d}.{int(match.group(2)):02d}"


# ============================================================
# GMAIL AUTH
# ============================================================

def get_gmail_credentials():
    env_token = os.environ.get("GMAIL_TOKEN_JSON")
    creds = None

    if env_token:
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(env_token),
                SCOPES,
            )
        except Exception as exc:
            print("Could not read GMAIL_TOKEN_JSON:", repr(exc), flush=True)

    if creds is None and os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        try:
            with open(TOKEN_FILE, "w") as file:
                file.write(creds.to_json())
        except Exception:
            pass

    return creds


# ============================================================
# HOME / PING
# ============================================================

@app.route("/")
def home():
    creds = get_gmail_credentials()
    gmail_status = "✅ Gmail connected" if creds else "❌ Gmail not connected"
    return (
        "<h2>Schedule Bot 💃</h2>"
        f"<p>{gmail_status}</p>"
        "<p>Bot is running.</p>"
        "<p><a href='/auth'>Connect / Reconnect Gmail</a></p>"
    )


@app.route("/ping")
def ping():
    if request.args.get("secret") != os.environ.get("CRON_SECRET"):
        return "unauthorized", 401
    return "✅ Schedule Bot is alive", 200


# ============================================================
# GOOGLE AUTH
# ============================================================

@app.route("/auth")
def auth():
    if not CLIENT_CONFIG or not REDIRECT_URI:
        return "Missing GOOGLE_CLIENT_SECRET_JSON or REDIRECT_URI", 500

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    session["state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    if "state" not in session:
        return "OAuth state missing. Start again from /auth.", 400

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        state=session["state"],
    )

    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    token_json = creds.to_json()

    with open(TOKEN_FILE, "w") as file:
        file.write(token_json)

    return (
        "Gmail connected 🎉<br><br>"
        "Save this in Render as GMAIL_TOKEN_JSON:<br><br>"
        f"<textarea readonly style='width:95%;height:180px;'>"
        f"{html.escape(token_json)}"
        f"</textarea>"
    )


# ============================================================
# PROCESSED LABEL
# ============================================================

def get_or_create_label(service):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])

    for label in labels:
        if label["name"] == LABEL_NAME:
            return label["id"]

    new_label = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": LABEL_NAME,
                "labelListVisibility": "labelHide",
                "messageListVisibility": "hide",
            },
        )
        .execute()
    )

    return new_label["id"]


def mark_as_processed(message_id):
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)
    label_id = get_or_create_label(service)

    (
        service.users()
        .messages()
        .modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        )
        .execute()
    )


# ============================================================
# FIND WEEKLY SCHEDULE
# ============================================================

def find_latest_schedule_pdf():
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    if not creds:
        return None, None, None

    service = build("gmail", "v1", credentials=creds)
    get_or_create_label(service)

    query = (
        f'from:{SENDER_EMAIL} '
        f'has:attachment '
        f'newer_than:10d '
        f'-label:{LABEL_NAME}'
    )

    result = (
        service.users()
        .messages()
        .list(
            userId="me",
            q=query,
            maxResults=20,
        )
        .execute()
    )

    refs = result.get("messages", [])
    messages = []

    for ref in refs:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="full",
            )
            .execute()
        )
        messages.append(msg)

    messages.sort(
        key=lambda message: int(message.get("internalDate", "0")),
        reverse=True,
    )

    for msg in messages:
        for part in walk_parts(msg["payload"]):
            filename = part.get("filename", "")

            if not filename.lower().endswith(".pdf"):
                continue
            if SCHEDULE_FILENAME_HINT not in filename.lower():
                continue

            body = part.get("body", {})

            if body.get("attachmentId"):
                attachment = (
                    service.users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=msg["id"],
                        id=body["attachmentId"],
                    )
                    .execute()
                )
                pdf_bytes = decode_gmail_data(attachment["data"])
            elif body.get("data"):
                pdf_bytes = decode_gmail_data(body["data"])
            else:
                continue

            print("✅ Weekly schedule found:", filename, flush=True)
            return pdf_bytes, msg["id"], filename

    print("❌ No new Ballett-PP schedule found", flush=True)
    return None, None, None


# ============================================================
# FIND NEWEST CAST LIST EMAIL
# ============================================================

def find_current_cast_list_pdfs():
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    if not creds:
        return []

    service = build("gmail", "v1", credentials=creds)

    query = (
        f'from:me '
        f'subject:"{CAST_LIST_SUBJECT}" '
        f'has:attachment'
    )

    result = (
        service.users()
        .messages()
        .list(
            userId="me",
            q=query,
            maxResults=20,
        )
        .execute()
    )

    refs = result.get("messages", [])
    if not refs:
        return []

    messages = []

    for ref in refs:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="full",
            )
            .execute()
        )
        messages.append(msg)

    messages.sort(
        key=lambda message: int(message.get("internalDate", "0")),
        reverse=True,
    )

    newest = messages[0]
    pdfs = []

    for part in walk_parts(newest["payload"]):
        filename = part.get("filename", "")

        if not filename.lower().endswith(".pdf"):
            continue

        body = part.get("body", {})

        if body.get("attachmentId"):
            attachment = (
                service.users()
                .messages()
                .attachments()
                .get(
                    userId="me",
                    messageId=newest["id"],
                    id=body["attachmentId"],
                )
                .execute()
            )
            pdf_bytes = decode_gmail_data(attachment["data"])
        elif body.get("data"):
            pdf_bytes = decode_gmail_data(body["data"])
        else:
            continue

        pdfs.append(
            {
                "filename": filename,
                "bytes": pdf_bytes,
            }
        )

    print(
        f"✅ Current Cast List email contains {len(pdfs)} PDF(s)",
        flush=True,
    )

    for item in pdfs:
        print("   •", item["filename"], flush=True)

    return pdfs


# ============================================================
# CLAUDE READS SCHEDULE
# ============================================================

def read_schedule_with_claude(pdf_bytes):
    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"]
    )

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        temperature=0,
        tools=[SCHEDULE_TOOL],
        tool_choice={
            "type": "tool",
            "name": "submit_schedule",
        },
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": SCHEDULE_PROMPT,
                    },
                ],
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_schedule":
            data = block.input
            if not data.get("days"):
                raise ValueError("Schedule contains no days")
            return data

    raise ValueError("Claude did not return structured schedule data")


# ============================================================
# CLAUDE READS CAST LIST
# ============================================================

def read_cast_list_with_claude(cast_pdfs):
    if not cast_pdfs:
        return {"ballets": []}

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"]
    )

    content = [{"type": "text", "text": CAST_PROMPT}]

    for item in cast_pdfs:
        content.append(
            {
                "type": "text",
                "text": "CAST LIST FILE: " + item["filename"],
            }
        )

        pdf_b64 = base64.standard_b64encode(item["bytes"]).decode("utf-8")

        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            }
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5000,
        temperature=0,
        tools=[CAST_TOOL],
        tool_choice={
            "type": "tool",
            "name": "submit_cast_roles",
        },
        messages=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_cast_roles":
            data = block.input
            data.setdefault("ballets", [])
            return data

    raise ValueError("Claude did not return structured Cast List data")


# ============================================================
# TRAINING
# ============================================================

def is_training(row):
    piece = norm(row.get("piece", ""))
    studio = normalize_studio(row.get("studio", ""))

    if studio not in {"BS1", "BS2"}:
        return False
    if "training" not in piece:
        return False
    if "reha" in piece:
        return False
    if re.search(r"\bjk\b", piece):
        return False

    return True


def is_voluntary(row):
    text = norm(
        f"{row.get('piece', '')} "
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    return "freiw" in text or "freiwillig" in text


# ============================================================
# FIND BALLET
# ============================================================

def find_ballet(piece, cast_info):
    piece_n = norm(piece)

    if not piece_n:
        return None

    for ballet in cast_info.get("ballets", []):
        names = list(ballet.get("schedule_names", []))
        names.append(ballet.get("ballet", ""))

        for name in names:
            name_n = norm(name)

            if not name_n:
                continue

            if name_n in piece_n or piece_n in name_n:
                return ballet

    return None


# ============================================================
# ROLE CHECKS
# ============================================================

def has_category(ballet, category):
    wanted = norm(category)

    for role in ballet.get("roles", []):
        if norm(role.get("category", "")) == wanted:
            return True

    return False


def role_is_called(ballet, text):
    text_n = norm(text)

    for role in ballet.get("roles", []):
        role_name = norm(role.get("role", ""))

        if not role_name:
            continue

        if role_name in {"solo dame", "solo herr"}:
            continue

        if role_name in text_n:
            return True

    return False


def performance_cast_restriction(text):
    return bool(
        re.search(
            r"\bbes\.\s*\d{1,2}\.\d{1,2}",
            text or "",
            flags=re.IGNORECASE,
        )
    )


# ============================================================
# DOES REHEARSAL BELONG TO LAURA?
# ============================================================

def belongs_to_me(row, cast_info):
    if is_training(row):
        return True

    piece = row.get("piece", "")
    dancers = row.get("dancers", "")
    notes = row.get("notes", "")

    ballet = find_ballet(piece, cast_info)

    if ballet is None:
        return False

    piece_n = norm(piece)
    dancers_n = norm(dancers)
    notes_n = norm(notes)

    if (
        "ohne fernandez g." in dancers_n
        or "ohne fernandez g." in notes_n
    ):
        return False

    # Her name only proves participation if it is in DANCERS.
    if "fernandez g." in dancers_n:
        return True

    if "entire cast" in dancers_n or "entire cast" in piece_n:
        return True

    if performance_cast_restriction(f"{dancers} {notes}"):
        return False

    solo_dame_call = (
        "solo dame" in dancers_n
        or "solodame" in dancers_n
        or "solo da." in dancers_n
        or "solo da " in dancers_n
    )

    if solo_dame_call and has_category(ballet, "Solo Dame"):
        return True

    if role_is_called(ballet, f"{piece} {dancers}"):
        return True

    return False


# ============================================================
# SAFE PERSONAL "BIS"
# ============================================================

def personal_bis_from_field(field):
    field = field or ""

    if not re.search(
        r"Fernandez\s+G\.",
        field,
        flags=re.IGNORECASE,
    ):
        return None

    match = re.search(
        r"\bbis\s+(\d{1,2}:\d{2})",
        field,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1)


def personal_end_time(row):
    result = personal_bis_from_field(row.get("dancers", ""))
    if result:
        return result

    result = personal_bis_from_field(row.get("notes", ""))
    if result:
        return result

    return row.get("end", "")


def personal_end_note(row):
    result = personal_bis_from_field(row.get("dancers", ""))

    if not result:
        result = personal_bis_from_field(row.get("notes", ""))

    if not result:
        return None

    return f"bis {result} für Fernandez G."


# ============================================================
# SAFE PERSONAL "AB"
# ============================================================

def personal_ab_from_field(field):
    field = field or ""

    if not re.search(
        r"Fernandez\s+G\.",
        field,
        flags=re.IGNORECASE,
    ):
        return None

    match = re.search(
        r"\bab\s+(\d{1,2}:\d{2})",
        field,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1)


def personal_start_note(row, selected_rows_for_day):
    result = personal_ab_from_field(row.get("dancers", ""))
    if result:
        return f"ab {result}"

    result = personal_ab_from_field(row.get("notes", ""))
    if result:
        return f"ab {result}"

    notes = row.get("notes", "") or ""

    match = re.search(
        r"\bab\s+(\d{1,2}:\d{2})",
        notes,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    notes_n = norm(notes)

    for other in selected_rows_for_day:
        if other is row:
            continue

        other_piece = norm(other.get("piece", ""))

        if not other_piece:
            continue

        keyword = other_piece.split()[0]

        if len(keyword) >= 5 and keyword in notes_n:
            return f"ab {match.group(1)}"

    return None


# ============================================================
# DISPLAY PIECE
# ============================================================

def display_piece(row, cast_info):
    ballet = find_ballet(row.get("piece", ""), cast_info)

    if not ballet:
        return row.get("piece", "")

    ballet_name = norm(ballet.get("ballet", ""))

    if "divertimento" in ballet_name:
        return "Divertimento"

    if "rhapsody" in ballet_name:
        return "Rhapsody"

    return ballet.get("ballet", row.get("piece", ""))


# ============================================================
# FORMAT TRAINING
# ============================================================

def format_training(rows):
    rows = sorted(
        rows,
        key=lambda row: (
            minutes(row.get("start", "")),
            0 if normalize_studio(row.get("studio", "")) == "BS1" else 1,
        ),
    )

    if not rows:
        return ""

    if (
        len(rows) >= 2
        and rows[0].get("start") == rows[1].get("start")
        and rows[0].get("end") == rows[1].get("end")
    ):
        first = rows[0]
        second = rows[1]

        first_text = (
            f"{first.get('piece', '').strip()} "
            f"– {normalize_studio(first.get('studio', ''))}"
        )

        first_staff = first_staff_name(first.get("staff", ""))

        if first_staff:
            first_text += f" ({first_staff})"

        second_text = (
            f"{second.get('piece', '').strip()} "
            f"– {normalize_studio(second.get('studio', ''))}"
        )

        second_staff = first_staff_name(second.get("staff", ""))

        if second_staff:
            second_text += f" ({second_staff})"

        return (
            f"• **{first.get('start')}-{first.get('end')}** "
            f"{first_text} ODER {second_text}"
        )

    row = rows[0]

    line = (
        f"• **{row.get('start')}-{row.get('end')}** "
        f"{row.get('piece', '').strip()} "
        f"– {normalize_studio(row.get('studio', ''))}"
    )

    staff = first_staff_name(row.get("staff", ""))

    if staff:
        line += f" ({staff})"

    return line


# ============================================================
# FORMAT REHEARSAL
# ============================================================

def format_rehearsal(row, cast_info, selected_rows_for_day):
    piece = display_piece(row, cast_info)
    studio = normalize_studio(row.get("studio", ""))

    line = (
        f"• **{row.get('start')}-{row.get('end')}** "
        f"{piece} – {studio}"
    )

    dancers = (row.get("dancers", "") or "").strip()

    if dancers:
        line += f" ({dancers})"

    if "rhapsody" in norm(piece):
        staff = row.get("staff", "") or ""
        dancers_n = norm(dancers)

        if re.search(
            r"\bFerri\b",
            staff,
            flags=re.IGNORECASE,
        ):
            line += " Ferri"

        elif "entire cast" not in dancers_n:
            coach = first_staff_name(staff)
            if coach:
                line += f" {coach}"

    end_note = personal_end_note(row)

    if end_note:
        line += f" – {end_note}"

    start_note = personal_start_note(row, selected_rows_for_day)

    if start_note:
        line += f" – {start_note}"

    return line


# ============================================================
# DEBUG
# ============================================================

def log_selected_rows(schedule, cast_info):
    print(
        "\n================ SELECTED REHEARSALS ================",
        flush=True,
    )

    for day in schedule.get("days", []):
        day_name = short_day(day.get("day", ""))

        print(f"\n--- {day_name} ---", flush=True)

        for row in day.get("rows", []):
            if belongs_to_me(row, cast_info):
                print(
                    "✅",
                    json.dumps(row, ensure_ascii=False),
                    flush=True,
                )

    print(
        "\n======================================================\n",
        flush=True,
    )


# ============================================================
# BUILD FINAL DIGEST
# ============================================================

def build_digest(schedule, cast_info):
    sections = []
    closing_times = []

    for day in schedule.get("days", []):
        rows = day.get("rows", [])

        selected = [
            row
            for row in rows
            if belongs_to_me(row, cast_info)
        ]

        if not selected:
            continue

        selected.sort(
            key=lambda row: (
                minutes(row.get("start", "")),
                minutes(row.get("end", "")),
            )
        )

        trainings = [
            row
            for row in selected
            if is_training(row)
        ]

        rehearsals = [
            row
            for row in selected
            if not is_training(row)
        ]

        lines = []

        if trainings:
            lines.append(format_training(trainings))

        for row in rehearsals:
            lines.append(
                format_rehearsal(
                    row,
                    cast_info,
                    selected,
                )
            )

        day_name = short_day(day.get("day", ""))
        date = short_date(day.get("date", ""))

        sections.append(
            f"**{day_name} {date}**"
            + "\n\n"
            + "\n\n".join(lines)
        )

        valid_rows = [
            row
            for row in selected
            if minutes(personal_end_time(row)) < 99999
        ]

        if valid_rows:
            latest = max(
                valid_rows,
                key=lambda row: minutes(
                    personal_end_time(row)
                ),
            )

            end = personal_end_time(latest)

            only_voluntary = (
                len(selected) == 1
                and is_training(selected[0])
                and is_voluntary(selected[0])
            )

            suffix = " (freiwillig)" if only_voluntary else ""

            closing_times.append(
                f"{day_name} {end}{suffix}"
            )

    if not sections:
        return "Keine passenden Proben gefunden."

    digest = "\n\n".join(sections)

    if closing_times:
        digest += (
            "\n\n"
            "**Feierabend:**\n"
            + "\n".join(closing_times)
        )

    return digest


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(message):
    id_instance = os.environ.get(
        "GREEN_API_ID_INSTANCE",
        "710522730585",
    )

    api_token = os.environ["GREEN_API_TOKEN"]
    my_number = os.environ["MY_WHATSAPP_NUMBER"]
    chat_id = f"{my_number}@c.us"

    base_url = (
        os.environ.get(
            "GREEN_API_BASE_URL",
            "https://7105.api.greenapi.com",
        )
        .rstrip("/")
    )

    url = (
        f"{base_url}"
        f"/waInstance{id_instance}"
        f"/sendMessage/{api_token}"
    )

    response = requests.post(
        url,
        json={
            "chatId": chat_id,
            "message": message,
        },
        timeout=30,
    )

    response.raise_for_status()


# ============================================================
# BACKGROUND WEEKLY JOB
# ============================================================

def process_weekly_job(job_id, dry_run):
    try:
        update_job(
            job_id,
            stage="finding_schedule",
            message="Suche den neuen Wochenplan von Ismenia…",
        )

        (
            schedule_pdf,
            message_id,
            schedule_filename,
        ) = find_latest_schedule_pdf()

        if not schedule_pdf:
            update_job(
                job_id,
                running=False,
                stage="no_schedule",
                message="Kein neuer unprocessed Wochenplan gefunden.",
                finished_at=utc_now(),
            )
            return

        update_job(
            job_id,
            schedule_file=schedule_filename,
        )

        update_job(
            job_id,
            stage="finding_cast",
            message="Suche deine neueste Cast List E-Mail…",
        )

        cast_pdfs = find_current_cast_list_pdfs()

        if not cast_pdfs:
            raise RuntimeError(
                "Keine aktuelle Cast List E-Mail mit PDFs gefunden."
            )

        update_job(
            job_id,
            cast_files=[
                item["filename"]
                for item in cast_pdfs
            ],
        )

        update_job(
            job_id,
            stage="reading_schedule",
            message="Claude liest gerade den Wochenplan. Das dauert am längsten…",
        )

        schedule = read_schedule_with_claude(
            schedule_pdf
        )

        print("✅ Schedule extracted", flush=True)

        update_job(
            job_id,
            stage="reading_cast",
            message="Claude liest jetzt deine Cast Lists…",
        )

        cast_info = read_cast_list_with_claude(
            cast_pdfs
        )

        if not cast_info.get("ballets"):
            raise RuntimeError(
                "Cast Lists wurden gelesen, aber keine Rollen für Fernandez G. gefunden."
            )

        print("✅ Cast List extracted", flush=True)

        update_job(
            job_id,
            stage="building",
            message="Python wählt jetzt nur deine Proben aus…",
        )

        log_selected_rows(
            schedule,
            cast_info,
        )

        digest = build_digest(
            schedule,
            cast_info,
        )

        if digest == "Keine passenden Proben gefunden.":
            raise RuntimeError(
                "Keine passenden Proben gefunden. Sicherheitsstopp."
            )

        print(
            "\n================ CURRENT ROLES ================\n",
            flush=True,
        )

        print(
            json.dumps(
                cast_info,
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )

        print(
            "\n================ WHATSAPP =====================\n",
            flush=True,
        )

        print(
            digest,
            flush=True,
        )

        print(
            "\n================================================\n",
            flush=True,
        )

        update_job(
            job_id,
            digest=digest,
        )

        if dry_run:
            update_job(
                job_id,
                running=False,
                stage="done",
                message="Dry Run fertig. Nichts wurde gesendet.",
                finished_at=utc_now(),
            )
            return

        update_job(
            job_id,
            stage="sending",
            message="WhatsApp wird gesendet…",
        )

        send_whatsapp(digest)

        mark_as_processed(message_id)

        update_job(
            job_id,
            running=False,
            stage="done",
            message="WhatsApp wurde gesendet und der Wochenplan als verarbeitet markiert.",
            finished_at=utc_now(),
        )

        print(
            "✅ WhatsApp sent and schedule marked processed",
            flush=True,
        )

    except Exception as exc:
        print(
            "❌ Background job failed:",
            repr(exc),
            flush=True,
        )

        print(
            traceback.format_exc(),
            flush=True,
        )

        update_job(
            job_id,
            running=False,
            stage="error",
            message="Der Lauf ist mit einem Fehler abgebrochen.",
            error=repr(exc),
            finished_at=utc_now(),
        )


# ============================================================
# START WEEKLY JOB
# ============================================================

@app.route(
    "/run-weekly",
    methods=["GET", "POST"],
)
def run_weekly():
    supplied_secret = request.args.get("secret")
    real_secret = os.environ.get("CRON_SECRET")

    if not real_secret or supplied_secret != real_secret:
        return "unauthorized", 401

    dry_run = request.args.get("dry") == "1"

    with JOB_LOCK:
        if JOB_STATE.get("running"):
            return (
                "<h2>Bot läuft bereits ⏳</h2>"
                "<p>Öffne die Status-Seite.</p>"
                f"<p><a href='/status?secret={quote(supplied_secret)}'>"
                "Status öffnen</a></p>",
                202,
            )

    job_id = uuid.uuid4().hex[:10]

    reset_job_state(
        job_id,
        dry_run,
    )

    worker = threading.Thread(
        target=process_weekly_job,
        args=(job_id, dry_run),
        daemon=True,
    )

    worker.start()

    status_url = (
        f"/status?"
        f"secret={quote(supplied_secret)}"
    )

    return (
        f'''
        <!doctype html>
        <html>
        <head>
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <meta http-equiv="refresh"
                  content="1;url={status_url}">
            <title>Schedule Bot</title>
        </head>
        <body style="
            font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;
            padding:30px;
            line-height:1.5;
        ">
            <h2>Gestartet ✅</h2>
            <p>Der Bot arbeitet jetzt im Hintergrund.</p>
            <p>Weiterleitung zum Status…</p>
        </body>
        </html>
        ''',
        202,
    )


# ============================================================
# STATUS PAGE
# ============================================================

@app.route(
    "/status",
    methods=["GET"],
)
def status():
    supplied_secret = request.args.get("secret")

    if supplied_secret != os.environ.get("CRON_SECRET"):
        return "unauthorized", 401

    state = get_job_state()

    if request.args.get("json") == "1":
        return jsonify(state)

    running = state.get("running", False)
    stage = state.get("stage", "idle")
    stage_label = STAGE_LABELS.get(stage, stage)
    message = state.get("message", "") or ""
    schedule_file = state.get("schedule_file") or ""
    cast_files = state.get("cast_files", []) or []
    digest = state.get("digest") or ""
    error = state.get("error") or ""
    dry_run = state.get("dry_run")

    refresh_tag = (
        "<meta http-equiv='refresh' content='5'>"
        if running
        else ""
    )

    cast_html = ""

    if cast_files:
        cast_html = (
            "<ul>"
            + "".join(
                f"<li>{html.escape(name)}</li>"
                for name in cast_files
            )
            + "</ul>"
        )

    preview_html = ""

    if digest:
        preview_html = (
            "<h3>WhatsApp Preview</h3>"
            "<pre style='"
            "white-space:pre-wrap;"
            "font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;"
            "font-size:16px;"
            "background:#f5f5f5;"
            "padding:16px;"
            "border-radius:12px;"
            "'>"
            + html.escape(digest)
            + "</pre>"
        )

    error_html = ""

    if error:
        error_html = (
            "<div style='"
            "background:#ffecec;"
            "padding:15px;"
            "border-radius:10px;"
            "margin-top:20px;"
            "'>"
            "<strong>Fehler:</strong><br>"
            + html.escape(error)
            + "</div>"
        )

    if running:
        status_icon = "⏳"
    elif stage == "done":
        status_icon = "✅"
    elif stage == "error":
        status_icon = "❌"
    else:
        status_icon = "ℹ️"

    mode_text = (
        "Dry Run"
        if dry_run
        else "Echter WhatsApp-Lauf"
    )

    schedule_html = ""

    if schedule_file:
        schedule_html = (
            "<p><strong>Schedule:</strong><br>"
            + html.escape(schedule_file)
            + "</p>"
        )

    cast_section_html = ""

    if cast_files:
        cast_section_html = (
            "<p><strong>Current Cast Lists:</strong></p>"
            + cast_html
        )

    refresh_text = ""

    if running:
        refresh_text = (
            "<p>Diese Seite aktualisiert sich automatisch alle 5 Sekunden.</p>"
        )

    return (
        f'''
        <!doctype html>
        <html>
        <head>
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            {refresh_tag}
            <title>Schedule Bot Status</title>
        </head>
        <body style="
            font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;
            padding:24px;
            line-height:1.5;
            max-width:800px;
            margin:auto;
        ">
            <h2>{status_icon} {html.escape(stage_label)}</h2>
            <p>{html.escape(message)}</p>

            <p>
                <strong>Modus:</strong>
                {html.escape(mode_text)}
            </p>

            <p>
                <strong>Job:</strong>
                {html.escape(str(state.get("job_id") or "-"))}
            </p>

            {schedule_html}
            {cast_section_html}
            {error_html}
            {preview_html}
            {refresh_text}
        </body>
        </html>
        ''',
        200,
    )


# ============================================================
# TEST WHATSAPP
# ============================================================

@app.route(
    "/test-whatsapp",
    methods=["GET"],
)
def test_whatsapp():
    if request.args.get("secret") != os.environ.get("CRON_SECRET"):
        return "unauthorized", 401

    send_whatsapp(
        "Test-Nachricht vom Schedule Bot 🎉"
    )

    return "test sent", 200


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000,
            )
        ),
    )
