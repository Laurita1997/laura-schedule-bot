import os
import json
import base64
import re

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

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "temporary-insecure-key"
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify"
]

_raw_client_config = os.environ.get(
    "GOOGLE_CLIENT_SECRET_JSON"
)

CLIENT_CONFIG = (
    json.loads(_raw_client_config)
    if _raw_client_config
    else None
)

REDIRECT_URI = os.environ.get(
    "REDIRECT_URI"
)

TOKEN_FILE = "gmail_token.json"

MY_NAME = "Fernandez G."

SENDER_EMAIL = "ismenia.keck@wiener-staatsballett.at"

SCHEDULE_FILENAME_HINT = "ballett-pp"

CAST_LIST_SUBJECT = "Cast list"

LABEL_NAME = "ScheduleBotProcessed"


# ============================================================
# STRUCTURED OUTPUT SCHEMAS
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
                        "day": {
                            "type": "string"
                        },
                        "date": {
                            "type": "string"
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "studio": {
                                        "type": "string"
                                    },
                                    "start": {
                                        "type": "string"
                                    },
                                    "end": {
                                        "type": "string"
                                    },
                                    "piece": {
                                        "type": "string"
                                    },
                                    "dancers": {
                                        "type": "string"
                                    },
                                    "staff": {
                                        "type": "string"
                                    },
                                    "notes": {
                                        "type": "string"
                                    }
                                },
                                "required": [
                                    "studio",
                                    "start",
                                    "end",
                                    "piece",
                                    "dancers",
                                    "staff",
                                    "notes"
                                ]
                            }
                        }
                    },
                    "required": [
                        "day",
                        "date",
                        "rows"
                    ]
                }
            }
        },
        "required": [
            "days"
        ]
    }
}


CAST_TOOL = {
    "name": "submit_cast_roles",
    "description": "Submit Fernandez G.'s current roles from the current Cast List email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ballets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ballet": {
                            "type": "string"
                        },
                        "schedule_names": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            }
                        },
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {
                                        "type": "string"
                                    },
                                    "category": {
                                        "type": "string"
                                    },
                                    "reserve": {
                                        "type": "boolean"
                                    }
                                },
                                "required": [
                                    "role",
                                    "category",
                                    "reserve"
                                ]
                            }
                        }
                    },
                    "required": [
                        "ballet",
                        "schedule_names",
                        "roles"
                    ]
                }
            }
        },
        "required": [
            "ballets"
        ]
    }
}


# ============================================================
# CLAUDE PROMPTS
# ============================================================

SCHEDULE_PROMPT = """
Read the attached Wiener Staatsballett weekly rehearsal schedule PDF.

The PDF is a VISUAL TABLE with normally one page per day.

Each page contains several vertical columns such as:

- Ballettsaal 1
- Ballettsaal 2
- Ballettsaal 3 / BS3
- BAK / Anproben
- div. Orte
- Gäste
- Sonstiges

Your ONLY task is to extract what is printed.

Do NOT decide whether Fernandez G. participates.

CRITICAL VISUAL RULES:

1. Process ONE PAGE AT A TIME.

2. Inside each page, process ONE COLUMN AT A TIME from TOP TO BOTTOM.

3. NEVER combine information from different columns.

4. One output row must represent ONE visually continuous timed block.

5. start, end, piece, dancers, staff and notes must belong to that same
   visual block.

6. Extract all timed rehearsals and training entries.

7. Also inspect side columns carefully.

8. If a named room is printed, preserve that room:
   Hilverdingsaal
   Wiesenthalsaal
   Elsslersaal
   Hankasaal
   etc.

9. Normalize:
   Ballettsaal 1 -> BS1
   Ballettsaal 2 -> BS2
   Ballettsaal 3 -> BS3

10. Preserve dancer calls faithfully, including:
    Entire Cast
    Alle Solo Damen & Herren
    Alle Solodamen & Herren verfügbar
    Solo Dame
    Solo Damen & Herren
    all available Solo Da. & Herr.
    fixed lists of names
    Variation numbers
    headcount groups

11. Preserve important notes exactly, including:
    ohne ...
    Bes. dates
    ab HH:MM
    bis HH:MM
    n. Mög.
    Fernandez G. bis HH:MM
    performance cast information

12. If something is empty, return an empty string.

13. Never invent a time or room.

Before submitting, verify every row against the visual PDF.

Then call submit_schedule exactly once with the COMPLETE schedule.
"""


CAST_PROMPT = f"""
Read all attached Wiener Staatsballett Cast List PDFs.

These PDFs all belong to ONE current Cast List email.

Therefore ALL attached PDFs are currently relevant.

Find every ballet in which:

{MY_NAME}

appears.

For each ballet:

1. Identify the ballet title.

2. Return schedule_names containing the likely abbreviated names used in
   rehearsal schedules.

Example:

Ballet:
Divertimento No. 15

schedule_names:
["Divertimento", "Divertimento No. 15"]

Example:

Ballet:
Rhapsody

schedule_names:
["Rhapsody"]

Example:

Ballet:
Nijinsky

schedule_names:
["Nijinsky"]

3. Find every role belonging to Fernandez G.

4. Determine the category from the visual hierarchy.

Example:

Solo Damen
Variation 6
Avraam, Liz, Fernandez G.

means:

role = "Variation 6"
category = "Solo Dame"

If the table itself says:

Solo Dame
Trenary / Fernandez G. / Fernandes

then:

role = "Solo Dame"
category = "Solo Dame"

If underneath Solo Herren:

category = "Solo Herr"

If it is a named character:

category = "Named Role"

If it is an ensemble role:

category = "Group/Ensemble"

5. reserve=true only if Fernandez G. is explicitly a reserve/cover for
   that role.

6. Ignore ballets where Fernandez G. does not appear.

7. Do not invent roles.

Then call submit_cast_roles exactly once.
"""


# ============================================================
# GENERAL HELPERS
# ============================================================

def walk_parts(part):
    yield part

    for child in part.get("parts", []):
        yield from walk_parts(child)


def decode_gmail_data(data):
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def norm(text):
    text = (text or "").lower()

    text = (
        text
        .replace("–", "-")
        .replace("—", "-")
    )

    text = re.sub(
        r"[^a-z0-9äöüß.]+",
        " ",
        text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


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
        hour, minute = map(
            int,
            time_text.split(":")
        )

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
        "so": "So"
    }

    return mapping.get(
        norm(day),
        day
    )


# ============================================================
# GMAIL AUTH
# ============================================================

def get_gmail_credentials():

    env_token = os.environ.get(
        "GMAIL_TOKEN_JSON"
    )

    creds = None

    if env_token:
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(env_token),
                SCOPES
            )
        except Exception as exc:
            print(
                "Could not read GMAIL_TOKEN_JSON:",
                repr(exc)
            )

    if (
        creds is None
        and os.path.exists(TOKEN_FILE)
    ):
        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES
        )

    if (
        creds
        and creds.expired
        and creds.refresh_token
    ):
        creds.refresh(
            GoogleAuthRequest()
        )

        try:
            with open(
                TOKEN_FILE,
                "w"
            ) as file:
                file.write(
                    creds.to_json()
                )
        except Exception:
            pass

    return creds


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    creds = get_gmail_credentials()

    gmail_status = (
        "✅ Gmail connected"
        if creds
        else "❌ Gmail not connected"
    )

    return (
        "Schedule Bot is running.<br>"
        f"{gmail_status}<br>"
        f"Redirect URI: {REDIRECT_URI}<br><br>"
        "<a href='/auth'>Connect / Reconnect Gmail</a>"
    )


# ============================================================
# GOOGLE AUTH
# ============================================================

@app.route("/auth")
def auth():

    if not CLIENT_CONFIG or not REDIRECT_URI:
        return (
            "Missing GOOGLE_CLIENT_SECRET_JSON or REDIRECT_URI",
            500
        )

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent"
    )

    session["state"] = state

    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():

    if "state" not in session:
        return (
            "OAuth state missing. Start again from /auth.",
            400
        )

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        state=session["state"]
    )

    flow.fetch_token(
        authorization_response=request.url
    )

    creds = flow.credentials

    token_json = creds.to_json()

    with open(
        TOKEN_FILE,
        "w"
    ) as file:
        file.write(token_json)

    return (
        "Gmail connected 🎉<br><br>"
        "Save this in Render as GMAIL_TOKEN_JSON:<br><br>"
        f"<textarea readonly style='width:95%;height:180px;'>"
        f"{token_json}"
        f"</textarea>"
    )


# ============================================================
# PROCESSED LABEL
# ============================================================

def get_or_create_label(service):

    labels = (
        service
        .users()
        .labels()
        .list(userId="me")
        .execute()
        .get("labels", [])
    )

    for label in labels:
        if label["name"] == LABEL_NAME:
            return label["id"]

    new_label = (
        service
        .users()
        .labels()
        .create(
            userId="me",
            body={
                "name": LABEL_NAME,
                "labelListVisibility": "labelHide",
                "messageListVisibility": "hide"
            }
        )
        .execute()
    )

    return new_label["id"]


def mark_as_processed(message_id):

    from googleapiclient.discovery import build

    creds = get_gmail_credentials()

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    label_id = get_or_create_label(
        service
    )

    (
        service
        .users()
        .messages()
        .modify(
            userId="me",
            id=message_id,
            body={
                "addLabelIds": [
                    label_id
                ]
            }
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

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    get_or_create_label(service)

    query = (
        f'from:{SENDER_EMAIL} '
        f'has:attachment '
        f'newer_than:10d '
        f'-label:{LABEL_NAME}'
    )

    result = (
        service
        .users()
        .messages()
        .list(
            userId="me",
            q=query,
            maxResults=20
        )
        .execute()
    )

    refs = result.get(
        "messages",
        []
    )

    full_messages = []

    for ref in refs:

        msg = (
            service
            .users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="full"
            )
            .execute()
        )

        full_messages.append(msg)

    full_messages.sort(
        key=lambda message: int(
            message.get(
                "internalDate",
                "0"
            )
        ),
        reverse=True
    )

    for msg in full_messages:

        for part in walk_parts(
            msg["payload"]
        ):

            filename = (
                part.get(
                    "filename",
                    ""
                )
            )

            if not filename.lower().endswith(
                ".pdf"
            ):
                continue

            if (
                SCHEDULE_FILENAME_HINT
                not in filename.lower()
            ):
                continue

            body = part.get(
                "body",
                {}
            )

            if body.get(
                "attachmentId"
            ):

                attachment = (
                    service
                    .users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=msg["id"],
                        id=body["attachmentId"]
                    )
                    .execute()
                )

                pdf_bytes = decode_gmail_data(
                    attachment["data"]
                )

            elif body.get("data"):

                pdf_bytes = decode_gmail_data(
                    body["data"]
                )

            else:
                continue

            print(
                "✅ Weekly schedule:",
                filename
            )

            return (
                pdf_bytes,
                msg["id"],
                filename
            )

    return None, None, None


# ============================================================
# FIND CURRENT CAST LIST EMAIL
# ============================================================

def find_current_cast_list_pdfs():
    """
    ONLY the newest email from YOU with subject "Cast list" is used.

    All PDFs attached to that email are considered your complete
    current Cast List set.

    Older Cast List emails are ignored.
    """

    from googleapiclient.discovery import build

    creds = get_gmail_credentials()

    if not creds:
        return []

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    query = (
        f'from:me '
        f'subject:"{CAST_LIST_SUBJECT}" '
        f'has:attachment'
    )

    result = (
        service
        .users()
        .messages()
        .list(
            userId="me",
            q=query,
            maxResults=10
        )
        .execute()
    )

    refs = result.get(
        "messages",
        []
    )

    if not refs:
        return []

    messages = []

    for ref in refs:

        msg = (
            service
            .users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="full"
            )
            .execute()
        )

        messages.append(msg)

    messages.sort(
        key=lambda message: int(
            message.get(
                "internalDate",
                "0"
            )
        ),
        reverse=True
    )

    newest = messages[0]

    pdfs = []

    for part in walk_parts(
        newest["payload"]
    ):

        filename = (
            part.get(
                "filename",
                ""
            )
        )

        if not filename.lower().endswith(
            ".pdf"
        ):
            continue

        body = part.get(
            "body",
            {}
        )

        if body.get(
            "attachmentId"
        ):

            attachment = (
                service
                .users()
                .messages()
                .attachments()
                .get(
                    userId="me",
                    messageId=newest["id"],
                    id=body["attachmentId"]
                )
                .execute()
            )

            pdf_bytes = decode_gmail_data(
                attachment["data"]
            )

        elif body.get("data"):

            pdf_bytes = decode_gmail_data(
                body["data"]
            )

        else:
            continue

        pdfs.append(
            {
                "filename": filename,
                "bytes": pdf_bytes
            }
        )

    print(
        f"✅ Current Cast List email contains {len(pdfs)} PDF(s)"
    )

    for item in pdfs:
        print(
            "   •",
            item["filename"]
        )

    return pdfs


# ============================================================
# CLAUDE READS SCHEDULE
# ============================================================

def read_schedule_with_claude(
    pdf_bytes
):

    client = anthropic.Anthropic(
        api_key=os.environ[
            "ANTHROPIC_API_KEY"
        ]
    )

    pdf_b64 = (
        base64
        .standard_b64encode(
            pdf_bytes
        )
        .decode("utf-8")
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,

        tools=[
            SCHEDULE_TOOL
        ],

        tool_choice={
            "type": "tool",
            "name": "submit_schedule"
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
                            "data": pdf_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": SCHEDULE_PROMPT
                    }
                ]
            }
        ]
    )

    for block in response.content:

        if (
            block.type == "tool_use"
            and block.name == "submit_schedule"
        ):

            data = block.input

            if not data.get("days"):
                raise ValueError(
                    "Schedule contains no days"
                )

            return data

    raise ValueError(
        "Claude did not return structured schedule data"
    )


# ============================================================
# CLAUDE READS CURRENT CAST LIST
# ============================================================

def read_cast_list_with_claude(
    cast_pdfs
):

    if not cast_pdfs:
        return {
            "ballets": []
        }

    client = anthropic.Anthropic(
        api_key=os.environ[
            "ANTHROPIC_API_KEY"
        ]
    )

    content = [
        {
            "type": "text",
            "text": CAST_PROMPT
        }
    ]

    for item in cast_pdfs:

        content.append(
            {
                "type": "text",
                "text": (
                    "CAST LIST FILE: "
                    + item["filename"]
                )
            }
        )

        pdf_b64 = (
            base64
            .standard_b64encode(
                item["bytes"]
            )
            .decode("utf-8")
        )

        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64
                }
            }
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5000,

        tools=[
            CAST_TOOL
        ],

        tool_choice={
            "type": "tool",
            "name": "submit_cast_roles"
        },

        messages=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    for block in response.content:

        if (
            block.type == "tool_use"
            and block.name == "submit_cast_roles"
        ):

            return block.input

    raise ValueError(
        "Claude did not return structured Cast List data"
    )


# ============================================================
# TRAINING
# ============================================================

def is_training(row):

    piece = norm(
        row.get(
            "piece",
            ""
        )
    )

    studio = normalize_studio(
        row.get(
            "studio",
            ""
        )
    )

    if studio not in {
        "BS1",
        "BS2"
    }:
        return False

    if "training" not in piece:
        return False

    if "reha" in piece:
        return False

    if re.search(
        r"\bjk\b",
        piece
    ):
        return False

    return True


def is_voluntary(row):

    combined = norm(
        f"{row.get('piece', '')} "
        f"{row.get('notes', '')}"
    )

    return (
        "freiw" in combined
        or "freiwillig" in combined
    )


# ============================================================
# MATCH SCHEDULE BALLET TO CAST LIST
# ============================================================

def find_ballet(
    piece,
    cast_info
):

    piece_n = norm(piece)

    if not piece_n:
        return None

    for ballet in cast_info.get(
        "ballets",
        []
    ):

        names = ballet.get(
            "schedule_names",
            []
        )

        names = names + [
            ballet.get(
                "ballet",
                ""
            )
        ]

        for name in names:

            name_n = norm(name)

            if not name_n:
                continue

            if (
                name_n in piece_n
                or piece_n in name_n
            ):
                return ballet

    return None


# ============================================================
# ROLE CHECKS
# ============================================================

def has_category(
    ballet,
    category
):

    wanted = norm(
        category
    )

    for role in ballet.get(
        "roles",
        []
    ):

        if norm(
            role.get(
                "category",
                ""
            )
        ) == wanted:

            return True

    return False


def role_is_called(
    ballet,
    text
):

    text_n = norm(
        text
    )

    for role in ballet.get(
        "roles",
        []
    ):

        role_name = norm(
            role.get(
                "role",
                ""
            )
        )

        if not role_name:
            continue

        if role_name in {
            "solo dame",
            "solo herr"
        }:
            continue

        if role_name in text_n:
            return True

    return False


def performance_cast_restriction(
    text
):

    return bool(
        re.search(
            r"\bbes\.\s*\d{1,2}\.\d{1,2}",
            text or "",
            flags=re.IGNORECASE
        )
    )


# ============================================================
# DOES THIS REHEARSAL BELONG TO LAURA?
# ============================================================

def belongs_to_me(
    row,
    cast_info
):

    # Normal daily training is always shown.

    if is_training(row):
        return True

    piece = row.get(
        "piece",
        ""
    )

    dancers = row.get(
        "dancers",
        ""
    )

    notes = row.get(
        "notes",
        ""
    )

    ballet = find_ballet(
        piece,
        cast_info
    )

    # Ignore ballets not in the current Cast List email.

    if ballet is None:
        return False

    piece_n = norm(piece)

    dancers_n = norm(dancers)

    combined = norm(
        f"{dancers} {notes}"
    )


    # Explicit exclusion

    if (
        "ohne fernandez g."
        in combined
    ):
        return False


    # Laura explicitly named

    if (
        "fernandez g."
        in combined
    ):
        return True


    # Entire Cast

    if (
        "entire cast"
        in dancers_n
        or "entire cast"
        in piece_n
    ):
        return True


    # Performance-specific cast restriction.
    #
    # Example:
    # Solo Damen & Herren Bes. 18.09. & 25.09.
    #
    # Not automatically Laura's rehearsal.

    if performance_cast_restriction(
        f"{dancers} {notes}"
    ):
        return False


    # Generic Solo Dame calls

    solo_dame_call = (
        "solo dame" in dancers_n
        or "solodame" in dancers_n
        or "solo da." in dancers_n
        or "solo da " in dancers_n
    )

    if (
        solo_dame_call
        and has_category(
            ballet,
            "Solo Dame"
        )
    ):
        return True


    # Exact current role appears

    if role_is_called(
        ballet,
        f"{piece} {dancers}"
    ):
        return True


    # Fixed dancer lists without Fernandez G. fall through here.

    return False


# ============================================================
# PERSONAL TIME NOTES
# ============================================================

def personal_end_time(row):

    text = (
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    match = re.search(
        r"Fernandez\s+G\..{0,100}?\bbis\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if match:
        return match.group(1)

    return row.get(
        "end",
        ""
    )


def personal_end_note(row):

    text = (
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    match = re.search(
        r"Fernandez\s+G\..{0,100}?\bbis\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if match:
        return (
            f"bis {match.group(1)} "
            f"für Fernandez G."
        )

    return None


def generic_start_note(row):

    text = row.get(
        "notes",
        ""
    )

    match = re.search(
        r"\bab\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if match:
        return (
            f"ab {match.group(1)}"
        )

    return None


# ============================================================
# DISPLAY NAMES
# ============================================================

def display_piece(
    row,
    cast_info
):

    ballet = find_ballet(
        row.get(
            "piece",
            ""
        ),
        cast_info
    )

    if not ballet:
        return row.get(
            "piece",
            ""
        )

    ballet_name = norm(
        ballet.get(
            "ballet",
            ""
        )
    )

    if "divertimento" in ballet_name:
        return "Divertimento"

    if "rhapsody" in ballet_name:
        return "Rhapsody"

    return ballet.get(
        "ballet",
        row.get(
            "piece",
            ""
        )
    )


# ============================================================
# FORMAT TRAINING
# ============================================================

def format_training(
    rows
):

    rows = sorted(
        rows,
        key=lambda row: (
            minutes(
                row.get(
                    "start",
                    ""
                )
            ),
            normalize_studio(
                row.get(
                    "studio",
                    ""
                )
            )
        )
    )

    if not rows:
        return ""


    # Standard two-option class

    if (
        len(rows) >= 2
        and rows[0].get("start") == rows[1].get("start")
        and rows[0].get("end") == rows[1].get("end")
    ):

        first = rows[0]
        second = rows[1]

        first_text = (
            f"{first.get('piece', '')} "
            f"– {normalize_studio(first.get('studio', ''))}"
        )

        first_staff = first_staff_name(
            first.get(
                "staff",
                ""
            )
        )

        if first_staff:
            first_text += (
                f" ({first_staff})"
            )


        second_text = (
            f"{second.get('piece', '')} "
            f"– {normalize_studio(second.get('studio', ''))}"
        )

        second_staff = first_staff_name(
            second.get(
                "staff",
                ""
            )
        )

        if second_staff:
            second_text += (
                f" ({second_staff})"
            )


        return (
            f"• **{first.get('start')}-{first.get('end')}** "
            f"{first_text} "
            f"ODER "
            f"{second_text}"
        )


    # One class, e.g. Saturday voluntary

    row = rows[0]

    line = (
        f"• **{row.get('start')}-{row.get('end')}** "
        f"{row.get('piece', '')} "
        f"– {normalize_studio(row.get('studio', ''))}"
    )

    staff = first_staff_name(
        row.get(
            "staff",
            ""
        )
    )

    if staff:
        line += (
            f" ({staff})"
        )

    return line


# ============================================================
# FORMAT REHEARSAL
# ============================================================

def format_rehearsal(
    row,
    cast_info
):

    piece = display_piece(
        row,
        cast_info
    )

    studio = normalize_studio(
        row.get(
            "studio",
            ""
        )
    )

    line = (
        f"• **{row.get('start')}-{row.get('end')}** "
        f"{piece} – {studio}"
    )

    dancers = (
        row.get(
            "dancers",
            ""
        )
        or ""
    ).strip()

    if dancers:
        line += (
            f" ({dancers})"
        )


    # Rhapsody:
    # only show main coach
    #
    # Ferri/Ishida -> Ferri
    # Gomes/Ishida -> Gomes

    if "rhapsody" in norm(piece):

        staff = (
            row.get(
                "staff",
                ""
            )
            or ""
        )

        if re.search(
            r"\bFerri\b",
            staff,
            flags=re.IGNORECASE
        ):
            line += " Ferri"

        else:

            coach = first_staff_name(
                staff
            )

            if coach:
                line += (
                    f" {coach}"
                )


    end_note = personal_end_note(
        row
    )

    if end_note:
        line += (
            f" – {end_note}"
        )


    start_note = generic_start_note(
        row
    )

    if start_note:
        line += (
            f" – {start_note}"
        )


    return line


# ============================================================
# FINAL WHATSAPP MESSAGE
# ============================================================

def build_digest(
    schedule,
    cast_info
):

    sections = []
    closing_times = []


    for day in schedule.get(
        "days",
        []
    ):

        rows = day.get(
            "rows",
            []
        )

        selected = [
            row
            for row in rows
            if belongs_to_me(
                row,
                cast_info
            )
        ]

        if not selected:
            continue


        selected.sort(
            key=lambda row: (
                minutes(
                    row.get(
                        "start",
                        ""
                    )
                ),
                minutes(
                    row.get(
                        "end",
                        ""
                    )
                )
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

            lines.append(
                format_training(
                    trainings
                )
            )


        for row in rehearsals:

            lines.append(
                format_rehearsal(
                    row,
                    cast_info
                )
            )


        day_name = short_day(
            day.get(
                "day",
                ""
            )
        )

        date = day.get(
            "date",
            ""
        )

        sections.append(
            f"**{day_name} {date}**"
            + "\n\n"
            + "\n\n".join(lines)
        )


        # Feierabend

        valid_rows = [
            row
            for row in selected
            if minutes(
                personal_end_time(row)
            ) < 99999
        ]

        if valid_rows:

            latest = max(
                valid_rows,
                key=lambda row:
                    minutes(
                        personal_end_time(
                            row
                        )
                    )
            )

            end = personal_end_time(
                latest
            )

            only_voluntary = (
                len(selected) == 1
                and is_training(
                    selected[0]
                )
                and is_voluntary(
                    selected[0]
                )
            )

            suffix = (
                " (freiwillig)"
                if only_voluntary
                else ""
            )

            closing_times.append(
                f"{day_name} {end}{suffix}"
            )


    if not sections:
        return (
            "Keine passenden Proben gefunden."
        )


    digest = "\n\n".join(
        sections
    )

    if closing_times:

        digest += (
            "\n\n"
            "**Feierabend:**\n"
            + "\n".join(
                closing_times
            )
        )


    return digest


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(message):

    id_instance = os.environ.get(
        "GREEN_API_ID_INSTANCE",
        "710522730585"
    )

    api_token = os.environ[
        "GREEN_API_TOKEN"
    ]

    my_number = os.environ[
        "MY_WHATSAPP_NUMBER"
    ]

    chat_id = (
        f"{my_number}@c.us"
    )

    base_url = os.environ.get(
        "GREEN_API_BASE_URL",
        "https://7105.api.greenapi.com"
    ).rstrip("/")

    url = (
        f"{base_url}"
        f"/waInstance{id_instance}"
        f"/sendMessage/{api_token}"
    )

    response = requests.post(
        url,
        json={
            "chatId": chat_id,
            "message": message
        },
        timeout=30
    )

    response.raise_for_status()


# ============================================================
# RUN WEEKLY
# ============================================================

@app.route(
    "/run-weekly",
    methods=[
        "GET",
        "POST"
    ]
)
def run_weekly():

    if (
        request.args.get(
            "secret"
        )
        != os.environ.get(
            "CRON_SECRET"
        )
    ):
        return (
            "unauthorized",
            401
        )


    dry_run = (
        request.args.get(
            "dry"
        )
        == "1"
    )


    # 1. Weekly schedule

    (
        schedule_pdf,
        message_id,
        schedule_filename
    ) = find_latest_schedule_pdf()


    if not schedule_pdf:

        return (
            "no new weekly schedule found",
            200
        )


    # 2. Current Cast List email

    cast_pdfs = (
        find_current_cast_list_pdfs()
    )


    if not cast_pdfs:

        return (
            "No current Cast List email found. "
            "Not sending.",
            500
        )


    # 3. Claude reads schedule

    try:

        schedule = (
            read_schedule_with_claude(
                schedule_pdf
            )
        )

        print(
            "✅ Schedule extracted"
        )

    except Exception as exc:

        print(
            "❌ Schedule extraction failed:",
            repr(exc)
        )

        return (
            f"Schedule extraction failed: {repr(exc)}",
            500
        )


    # 4. Claude reads current roles

    try:

        cast_info = (
            read_cast_list_with_claude(
                cast_pdfs
            )
        )

        print(
            "✅ Cast List extracted"
        )

    except Exception as exc:

        print(
            "❌ Cast extraction failed:",
            repr(exc)
        )

        return (
            f"Cast extraction failed: {repr(exc)}",
            500
        )


    # 5. Python builds Laura's schedule

    digest = build_digest(
        schedule,
        cast_info
    )


    print(
        "\n"
        "================ CURRENT ROLES ================"
    )

    print(
        json.dumps(
            cast_info,
            ensure_ascii=False,
            indent=2
        )
    )

    print(
        "\n"
        "================ WHATSAPP ====================="
    )

    print(digest)

    print(
        "\n"
        "================================================"
    )


    # SAFE TEST

    if dry_run:

        return jsonify(
            {
                "dry_run": True,

                "schedule_file":
                    schedule_filename,

                "cast_files": [
                    item["filename"]
                    for item in cast_pdfs
                ],

                "cast_info":
                    cast_info,

                "digest":
                    digest,

                "schedule":
                    schedule
            }
        )


    # Actual WhatsApp

    try:

        send_whatsapp(
            digest
        )

    except Exception as exc:

        print(
            "❌ WhatsApp failed:",
            repr(exc)
        )

        return (
            f"WhatsApp failed: {repr(exc)}",
            500
        )


    # Only mark processed AFTER successful WhatsApp

    mark_as_processed(
        message_id
    )


    return (
        "sent",
        200
    )


# ============================================================
# TEST WHATSAPP
# ============================================================

@app.route(
    "/test-whatsapp",
    methods=[
        "GET"
    ]
)
def test_whatsapp():

    if (
        request.args.get(
            "secret"
        )
        != os.environ.get(
            "CRON_SECRET"
        )
    ):

        return (
            "unauthorized",
            401
        )

    send_whatsapp(
        "Test-Nachricht vom Schedule Bot 🎉"
    )

    return (
        "test sent",
        200
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )
