import os
import json
import base64
import re
from difflib import SequenceMatcher

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
    "temporary-insecure-key-please-set-a-real-one"
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


# ============================================================
# WEEKLY SCHEDULE SETTINGS
# ============================================================

SENDER_EMAIL = (
    "ismenia.keck@wiener-staatsballett.at"
)

# Weekly schedule filenames currently look like:
# 07.09.26_Ballett-PP-1.pdf
SCHEDULE_FILENAME_HINT = "ballett-pp"

LABEL_NAME = "ScheduleBotProcessed"


# ============================================================
# CAST LIST SETTINGS
# ============================================================

# You send these emails to yourself.
# They can be weeks/months old.
CAST_LIST_SUBJECT = "Cast list"


# ============================================================
# CLAUDE PROMPT:
# WEEKLY SCHEDULE PDF -> JSON
# ============================================================

TRANSCRIPTION_PROMPT = """
You are extracting a Wiener Staatsballett weekly rehearsal schedule PDF.

The PDF is a VISUAL TABLE.

Each page represents one day.

Each page contains multiple vertical columns such as:

- Ballettsaal 1
- Ballettsaal 2
- Ballettsaal 3 / BS3
- BAK / Anproben
- div. Orte
- Gäste
- Sonstiges

CRITICAL VISUAL READING RULES:

1. Process ONE PAGE AT A TIME.

2. Within each page, process ONE COLUMN AT A TIME from TOP TO BOTTOM.

3. NEVER combine a time from one column with text from another column.

4. Every row must correspond to ONE visually continuous block inside ONE
   column.

5. A time may only be attached to a row if that time is printed inside the
   same visual block.

6. If a side column contains a named room such as:
   Hilverdingsaal
   Wiesenthalsaal
   Elsslersaal
   Hankasaal
   etc.
   use that named room as "studio".

7. Do NOT infer or invent times.

8. Do NOT decide whether a row belongs to Fernandez G.
   Your job is extraction only.

9. Preserve dancer text, staff text, and notes as faithfully as possible.

10. Extract ALL timed blocks including:
    - training
    - rehearsals
    - costume fittings
    - administrative entries
    - performances
    - side-column entries

11. If red/colored text is visually attached to the same rehearsal block,
    put it in "notes" unless it is clearly part of the dancer call itself.

12. Keep "Entire Cast" in the dancers field when it is the dancer call.

13. Keep performance-cast restrictions such as:
    "Bes. 18.09. & 25.09."
    exactly.

14. Keep personal time notes such as:
    "Fernandez G., Mitsumori bis 13:25"
    exactly.

15. Do not silently move a rehearsal into another room because another block
    on the same horizontal level looks related.

Return VALID JSON ONLY.

Exact schema:

{
  "days": [
    {
      "day": "Mo",
      "date": "07.09",
      "rows": [
        {
          "studio": "BS1",
          "start": "10:00",
          "end": "11:15",
          "piece": "Training Blue Group",
          "dancers": "",
          "staff": "Gomes/Takizawa",
          "notes": ""
        }
      ]
    }
  ]
}

Use these studio normalizations:

Ballettsaal 1 -> BS1
Ballettsaal 2 -> BS2
Ballettsaal 3 -> BS3

For a named room such as Hilverdingsaal, Wiesenthalsaal, Elsslersaal or
Hankasaal, preserve the named room itself.

Before returning JSON, verify EVERY ROW against the visual page:

- time and activity are from the same block
- dancer names are from the same block
- staff names are from the same block
- notes are from the same block
- studio belongs to that block

Output JSON only.

No markdown code fences.
No commentary.
"""


# ============================================================
# CLAUDE PROMPT:
# CAST LIST PDFs -> MY ROLES
# ============================================================

CAST_ROLE_PROMPT = f"""
You are reading Wiener Staatsballett CAST LIST PDFs for the dancer
"{MY_NAME}".

The files that follow are supplied NEWEST TO OLDEST.

IMPORTANT VERSION RULE:

If the same ballet appears in more than one PDF, use ONLY the FIRST
occurrence of that ballet in this input because it is the newest version.

Ignore older versions of the same ballet.

For each newest ballet PDF:

1. Identify the exact ballet title.

2. Find every place where "{MY_NAME}" appears in the cast table.

3. Determine the role and its category from the visual hierarchy of the
   table.

4. If a role is underneath a section like "Solo Damen":
   preserve the specific role/variation but set category to "Solo Dame".

Example:

Solo Damen
Variation 6
Avraam, Liz, Fernandez G.

means:

role = "Variation 6"
category = "Solo Dame"

5. If underneath "Solo Herren":
   category = "Solo Herr".

6. If it is a named character/role:
   category = "Named Role".

7. If it is clearly a group/ensemble assignment:
   category = "Group/Ensemble".

8. If Fernandez G. appears under Reserve rather than the main cast:
   keep the role and set reserve=true.

Covers/reserves still matter.

9. If the same role appears more than once in the same ballet PDF,
   deduplicate it.

10. Omit ballets where "{MY_NAME}" does not appear.

11. Do not infer a role that is not printed.

Return VALID JSON ONLY:

{{
  "ballets": [
    {{
      "ballet": "Rhapsody",
      "source_file": "filename.pdf",
      "roles": [
        {{
          "role": "Solo Dame",
          "category": "Solo Dame",
          "reserve": false
        }}
      ]
    }}
  ]
}}

Output JSON only.

No markdown code fences.
No commentary.
"""


# ============================================================
# HELPERS
# ============================================================

def walk_parts(part):
    """
    Recursively walk through Gmail MIME parts.
    """

    yield part

    for child in part.get(
        "parts",
        []
    ):
        yield from walk_parts(
            child
        )


def decode_gmail_data(
    data: str
) -> bytes:

    padded = (
        data
        + "=" * (
            -len(data) % 4
        )
    )

    return base64.urlsafe_b64decode(
        padded
    )


def parse_json_response(
    text: str
) -> dict:

    text = text.strip()

    # Claude normally returns clean JSON because we ask it to,
    # but this protects against accidental ```json fences.

    if text.startswith("```"):

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

    start = text.find("{")
    end = text.rfind("}")

    if (
        start == -1
        or end == -1
        or end <= start
    ):

        raise ValueError(
            "No JSON object found in Claude response: "
            + text[:500]
        )

    return json.loads(
        text[start:end + 1]
    )


def norm(
    text: str
) -> str:

    text = (
        text
        or ""
    ).lower()

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


def normalize_studio(
    studio: str
) -> str:

    value = (
        studio
        or ""
    ).strip()

    n = norm(
        value
    )

    if (
        "ballettsaal 1" in n
        or n == "bs1"
    ):
        return "BS1"

    if (
        "ballettsaal 2" in n
        or n == "bs2"
    ):
        return "BS2"

    if (
        "ballettsaal 3" in n
        or n == "bs3"
    ):
        return "BS3"

    return value


def first_staff_name(
    staff: str
) -> str:

    staff = (
        staff
        or ""
    ).strip()

    if not staff:
        return ""

    return staff.split(
        "/"
    )[0].strip()


def minutes(
    time_text: str
) -> int:

    h, m = map(
        int,
        time_text.split(":")
    )

    return (
        h * 60
        + m
    )


def day_abbreviation(
    day: str
) -> str:

    d = norm(
        day
    )

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

    return mapping.get(
        d,
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

            creds = (
                Credentials
                .from_authorized_user_info(
                    json.loads(
                        env_token
                    ),
                    SCOPES
                )
            )

        except Exception as exc:

            print(
                "Could not load GMAIL_TOKEN_JSON:",
                exc
            )

    if (
        creds is None
        and os.path.exists(
            TOKEN_FILE
        )
    ):

        creds = (
            Credentials
            .from_authorized_user_file(
                TOKEN_FILE,
                SCOPES
            )
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
            ) as f:

                f.write(
                    creds.to_json()
                )

        except Exception:
            pass

    return creds


# ============================================================
# HOME PAGE
# ============================================================

@app.route("/")
def home():

    creds = get_gmail_credentials()

    gmail_status = (
        "✅ Gmail connected"
        if creds
        else "❌ Not connected yet"
    )

    config_status = (
        "✅ Google client config loaded"
        if CLIENT_CONFIG
        else "❌ GOOGLE_CLIENT_SECRET_JSON missing/invalid"
    )

    redirect_status = (
        f"Redirect URI: {REDIRECT_URI}"
        if REDIRECT_URI
        else "❌ REDIRECT_URI missing"
    )

    return (
        "Schedule Bot is running.<br>"
        f"{gmail_status}<br>"
        f"{config_status}<br>"
        f"{redirect_status}<br><br>"
        "<a href='/auth'>Connect / Reconnect Gmail</a>"
    )


# ============================================================
# GOOGLE AUTH
# ============================================================

@app.route("/auth")
def auth():

    if (
        not CLIENT_CONFIG
        or not REDIRECT_URI
    ):

        return (
            "Missing GOOGLE_CLIENT_SECRET_JSON or REDIRECT_URI. "
            "Check Render environment variables.",
            500
        )

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    auth_url, state = (
        flow.authorization_url(
            access_type="offline",
            prompt="consent"
        )
    )

    session["state"] = state

    return redirect(
        auth_url
    )


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

    token_json = (
        creds.to_json()
    )

    with open(
        TOKEN_FILE,
        "w"
    ) as f:

        f.write(
            token_json
        )

    return (
        "Gmail connected! 🎉<br><br>"
        "To make the connection survive Render restarts, "
        "copy the text below and save it in Render as:<br>"
        "<b>GMAIL_TOKEN_JSON</b><br><br>"
        f"<textarea readonly "
        f"style='width:95%;height:180px;'>"
        f"{token_json}"
        f"</textarea>"
    )


# ============================================================
# GMAIL LABEL
# ============================================================

def get_or_create_label(
    service
):

    labels = (
        service
        .users()
        .labels()
        .list(
            userId="me"
        )
        .execute()
        .get(
            "labels",
            []
        )
    )

    for label in labels:

        if (
            label["name"]
            == LABEL_NAME
        ):

            return label["id"]

    new_label = (
        service
        .users()
        .labels()
        .create(
            userId="me",
            body={
                "name":
                    LABEL_NAME,

                "labelListVisibility":
                    "labelHide",

                "messageListVisibility":
                    "hide",
            }
        )
        .execute()
    )

    return new_label["id"]


def mark_as_processed(
    message_id
):

    from googleapiclient.discovery import build

    creds = (
        get_gmail_credentials()
    )

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    label_id = (
        get_or_create_label(
            service
        )
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
# FIND NEW WEEKLY SCHEDULE
# ============================================================

def find_latest_schedule_pdf():
    """
    Every Friday:

    1. Search new/unprocessed emails from Ismenia.
    2. Search for a PDF containing "Ballett-PP" in the filename.
    3. Never use Cast List as a fallback.
    """

    from googleapiclient.discovery import build

    creds = (
        get_gmail_credentials()
    )

    if not creds:
        return (
            None,
            None,
            None
        )

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    get_or_create_label(
        service
    )

    query = (
        f'from:{SENDER_EMAIL} '
        f'has:attachment '
        f'newer_than:10d '
        f'-label:{LABEL_NAME}'
    )

    results = (
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

    messages = results.get(
        "messages",
        []
    )

    for item in messages:

        msg_id = (
            item["id"]
        )

        msg = (
            service
            .users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="full"
            )
            .execute()
        )

        for part in walk_parts(
            msg["payload"]
        ):

            filename = (
                part.get(
                    "filename",
                    ""
                )
            )

            filename_lower = (
                filename.lower()
            )

            if not filename_lower.endswith(
                ".pdf"
            ):
                continue

            if (
                SCHEDULE_FILENAME_HINT
                not in filename_lower
            ):
                continue

            body = (
                part.get(
                    "body",
                    {}
                )
            )

            if body.get(
                "attachmentId"
            ):

                att = (
                    service
                    .users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=msg_id,
                        id=body[
                            "attachmentId"
                        ]
                    )
                    .execute()
                )

                pdf_bytes = (
                    decode_gmail_data(
                        att["data"]
                    )
                )

                print(
                    "✅ Weekly schedule found:",
                    filename
                )

                return (
                    pdf_bytes,
                    msg_id,
                    filename
                )

            if body.get(
                "data"
            ):

                pdf_bytes = (
                    decode_gmail_data(
                        body["data"]
                    )
                )

                print(
                    "✅ Weekly schedule found inline:",
                    filename
                )

                return (
                    pdf_bytes,
                    msg_id,
                    filename
                )

    print(
        "❌ No new Ballett-PP weekly schedule found"
    )

    return (
        None,
        None,
        None
    )


# ============================================================
# FIND CAST LIST PDFs
# ============================================================

def find_cast_list_pdfs():
    """
    Searches ALL emails sent by you with subject "Cast list".

    Cast List emails:
    - do NOT have to be recent
    - are NOT marked processed
    - remain available indefinitely

    Newer Cast List emails are sent to Claude first.

    Example:

    August Cast list email:
        Rhapsody
        Divertimento
        Nijinsky

    October Cast list email:
        Swan Lake

    Result:
        Swan Lake
        Rhapsody
        Divertimento
        Nijinsky

    If later you send a NEW Rhapsody cast list,
    Claude is instructed to use the newest Rhapsody
    and ignore the old Rhapsody.
    """

    from googleapiclient.discovery import build

    creds = (
        get_gmail_credentials()
    )

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

    all_message_ids = []

    page_token = None

    while (
        len(all_message_ids)
        < 200
    ):

        result = (
            service
            .users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=100,
                pageToken=page_token
            )
            .execute()
        )

        all_message_ids.extend(
            result.get(
                "messages",
                []
            )
        )

        page_token = (
            result.get(
                "nextPageToken"
            )
        )

        if not page_token:
            break

    pdfs = []

    for item in all_message_ids:

        msg_id = (
            item["id"]
        )

        msg = (
            service
            .users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="full"
            )
            .execute()
        )

        internal_date = int(
            msg.get(
                "internalDate",
                "0"
            )
        )

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

            body = (
                part.get(
                    "body",
                    {}
                )
            )

            if body.get(
                "attachmentId"
            ):

                att = (
                    service
                    .users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=msg_id,
                        id=body[
                            "attachmentId"
                        ]
                    )
                    .execute()
                )

                pdf_bytes = (
                    decode_gmail_data(
                        att["data"]
                    )
                )

            elif body.get(
                "data"
            ):

                pdf_bytes = (
                    decode_gmail_data(
                        body["data"]
                    )
                )

            else:

                continue

            pdfs.append(
                {
                    "filename":
                        filename,

                    "bytes":
                        pdf_bytes,

                    "internal_date":
                        internal_date,
                }
            )

    # Newest emails/files first.
    pdfs.sort(
        key=lambda item:
            item["internal_date"],
        reverse=True
    )

    # Safety limit.
    # It should normally be far fewer than this.
    pdfs = pdfs[:30]

    print(
        f"✅ Found {len(pdfs)} Cast List PDF(s)"
    )

    for item in pdfs:

        print(
            "   •",
            item["filename"]
        )

    return pdfs


# ============================================================
# CLAUDE READS WEEKLY SCHEDULE
# ============================================================

def call_claude_transcribe(
    pdf_bytes: bytes
) -> dict:

    client = (
        anthropic.Anthropic(
            api_key=os.environ[
                "ANTHROPIC_API_KEY"
            ]
        )
    )

    pdf_b64 = (
        base64
        .standard_b64encode(
            pdf_bytes
        )
        .decode(
            "utf-8"
        )
    )

    response = (
        client.messages.create(
            model="claude-sonnet-4-6",

            max_tokens=12000,

            messages=[
                {
                    "role":
                        "user",

                    "content":
                    [
                        {
                            "type":
                                "document",

                            "source":
                            {
                                "type":
                                    "base64",

                                "media_type":
                                    "application/pdf",

                                "data":
                                    pdf_b64
                            }
                        },

                        {
                            "type":
                                "text",

                            "text":
                                TRANSCRIPTION_PROMPT
                        }
                    ]
                }
            ]
        )
    )

    text = "".join(
        block.text

        for block
        in response.content

        if block.type
        == "text"
    )

    data = (
        parse_json_response(
            text
        )
    )

    if not data.get(
        "days"
    ):

        raise ValueError(
            "Schedule JSON contains no days"
        )

    return data


# ============================================================
# CLAUDE READS CAST LISTS
# ============================================================

def call_claude_extract_cast_roles(
    cast_pdfs: list
) -> dict:

    if not cast_pdfs:

        return {
            "ballets": []
        }

    client = (
        anthropic.Anthropic(
            api_key=os.environ[
                "ANTHROPIC_API_KEY"
            ]
        )
    )

    content = [
        {
            "type":
                "text",

            "text":
                CAST_ROLE_PROMPT
        }
    ]

    for item in cast_pdfs:

        content.append(
            {
                "type":
                    "text",

                "text":
                    (
                        "SOURCE FILE "
                        "(newer files appear earlier): "
                        + item[
                            "filename"
                        ]
                    )
            }
        )

        cast_b64 = (
            base64
            .standard_b64encode(
                item["bytes"]
            )
            .decode(
                "utf-8"
            )
        )

        content.append(
            {
                "type":
                    "document",

                "source":
                {
                    "type":
                        "base64",

                    "media_type":
                        "application/pdf",

                    "data":
                        cast_b64
                }
            }
        )

    response = (
        client.messages.create(
            model="claude-sonnet-4-6",

            max_tokens=5000,

            messages=[
                {
                    "role":
                        "user",

                    "content":
                        content
                }
            ]
        )
    )

    text = "".join(
        block.text

        for block
        in response.content

        if block.type
        == "text"
    )

    data = (
        parse_json_response(
            text
        )
    )

    data.setdefault(
        "ballets",
        []
    )

    return data


# ============================================================
# TRAINING DETECTION
# ============================================================

def is_normal_training(
    row: dict
) -> bool:

    piece = norm(
        row.get(
            "piece",
            ""
        )
    )

    studio = (
        normalize_studio(
            row.get(
                "studio",
                ""
            )
        )
    )

    # We only want normal company class options
    # in BS1 / BS2.

    if studio not in {
        "BS1",
        "BS2"
    }:

        return False

    if "training" not in piece:

        return False

    # Exclude Reha Training.

    if "reha" in piece:

        return False

    # Exclude JK Training.

    if re.search(
        r"\bjk\b",
        piece
    ):

        return False

    return True


def is_voluntary_training(
    row: dict
) -> bool:

    text = norm(
        f"{row.get('piece', '')} "
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    return (
        "freiw" in text
        or "freiwillig" in text
    )


# ============================================================
# MATCH A SCHEDULE PIECE TO A CAST LIST BALLET
# ============================================================

def find_cast_ballet_for_piece(
    piece: str,
    cast_info: dict
):

    piece_n = norm(
        piece
    )

    if not piece_n:
        return None

    best = None
    best_score = 0.0

    for ballet in cast_info.get(
        "ballets",
        []
    ):

        ballet_name = (
            ballet.get(
                "ballet",
                ""
            )
        )

        ballet_n = norm(
            ballet_name
        )

        if not ballet_n:
            continue

        # Example:
        # schedule = "Divertimento"
        # cast list = "Divertimento No. 15"

        if (
            ballet_n in piece_n
            or piece_n in ballet_n
        ):

            score = 1.0

        else:

            score = (
                SequenceMatcher(
                    None,
                    piece_n,
                    ballet_n
                )
                .ratio()
            )

            # Also compare the first important word.
            # Helps if Claude/PDF has a small typo such as Rhapsdoy.

            first_piece = (
                piece_n.split()[0]
                if piece_n.split()
                else ""
            )

            first_ballet = (
                ballet_n.split()[0]
                if ballet_n.split()
                else ""
            )

            if (
                len(first_piece)
                >= 5

                and len(first_ballet)
                >= 5
            ):

                score = max(
                    score,

                    SequenceMatcher(
                        None,
                        first_piece,
                        first_ballet
                    ).ratio()
                )

        if score > best_score:

            best_score = score
            best = ballet

    # If uncertain, leave it out rather than guess.

    if best_score < 0.72:
        return None

    return best


# ============================================================
# CAST ROLE HELPERS
# ============================================================

def ballet_has_category(
    ballet: dict,
    category: str
) -> bool:

    target = norm(
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
        ) == target:

            return True

    return False


def ballet_has_variation(
    ballet: dict,
    variation_number: str
) -> bool:

    needle = (
        f"variation {variation_number}"
    )

    for role in ballet.get(
        "roles",
        []
    ):

        if needle in norm(
            role.get(
                "role",
                ""
            )
        ):

            return True

    return False


# ============================================================
# PERFORMANCE CAST RESTRICTION
# ============================================================

def performance_date_restriction(
    text: str
) -> bool:

    # Examples:
    #
    # Bes. 18.09.
    # Bes. 18.09. & 25.09.

    return bool(
        re.search(
            r"\bbes\.\s*\d{1,2}\.\d{1,2}\.?",
            text or "",
            flags=re.IGNORECASE
        )
    )


# ============================================================
# DOES THIS ROW BELONG TO ME?
# ============================================================

def belongs_to_me(
    row: dict,
    cast_info: dict
) -> bool:

    # All normal training options are included.

    if is_normal_training(
        row
    ):

        return True

    piece = (
        row.get(
            "piece",
            ""
        )
    )

    dancers = (
        row.get(
            "dancers",
            ""
        )
    )

    notes = (
        row.get(
            "notes",
            ""
        )
    )

    ballet = (
        find_cast_ballet_for_piece(
            piece,
            cast_info
        )
    )

    # Not one of her ballets.

    if ballet is None:
        return False

    piece_n = norm(
        piece
    )

    dancers_n = norm(
        dancers
    )

    combined_n = norm(
        f"{dancers} {notes}"
    )

    # ========================================================
    # 1. Explicitly excluded
    # ========================================================

    if (
        "ohne fernandez g."
        in combined_n
    ):

        return False


    # ========================================================
    # 2. Her literal name appears
    # ========================================================

    if (
        "fernandez g."
        in dancers_n
    ):

        return True


    # ========================================================
    # 3. Entire Cast
    # ========================================================

    if (
        "entire cast"
        in dancers_n

        or

        "entire cast"
        in piece_n
    ):

        return True


    # ========================================================
    # 4. PERFORMANCE CAST RESTRICTION
    # ========================================================

    # This comes AFTER literal name and Entire Cast.
    #
    # Therefore:
    #
    # Saturday:
    # Solo Damen & Herren Bes. 18.09. & 25.09.
    # -> excluded
    #
    # Friday:
    # Entire Cast
    # + separate "Bes. 18.09. ... in Kostüm"
    # -> already included above

    if performance_date_restriction(
        f"{dancers} {notes}"
    ):

        return False


    # ========================================================
    # 5. SPECIFIC VARIATION
    # ========================================================

    variation_match = re.search(
        r"\bvariation\s*(\d+)\b",
        combined_n
    )

    if variation_match:

        return ballet_has_variation(
            ballet,
            variation_match.group(1)
        )


    # ========================================================
    # 6. GENERIC SOLO DAME CALL
    # ========================================================

    # Matches:
    #
    # Solo Dame
    # Solo Damen
    # Alle Solo Damen & Herren
    # Alle Solodamen & Herren
    # all available Solo Da. & Herr.

    solo_dame_call = (
        "solo dame" in dancers_n
        or "solodame" in dancers_n
        or "solo da" in dancers_n
    )

    if (
        solo_dame_call
        and ballet_has_category(
            ballet,
            "Solo Dame"
        )
    ):

        return True


    # ========================================================
    # 7. GENERIC SOLO HERR CALL
    # ========================================================

    solo_herr_call = (
        "solo herr" in dancers_n
        or "soloherr" in dancers_n
    )

    if (
        solo_herr_call
        and ballet_has_category(
            ballet,
            "Solo Herr"
        )
    ):

        return True


    # ========================================================
    # 8. NAMED ROLE
    # ========================================================

    # Useful for future ballets.
    #
    # Example:
    # if cast list says her role is "Die Ballerina"
    # and schedule explicitly calls "Die Ballerina".

    combined_role_text = norm(
        f"{piece} {dancers}"
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

        if (
            len(role_name)
            >= 4

            and role_name
            in combined_role_text
        ):

            return True


    # ========================================================
    # 9. FIXED NAMED LIST WITHOUT HER
    # ========================================================

    # Example:
    #
    # Lynch, Fredianelli, Cislaghi, Kim...
    #
    # If Fernandez G. is not in that list, it is NOT hers.

    return False


# ============================================================
# PERSONAL END TIMES
# ============================================================

def personal_end_time(
    row: dict
) -> str:

    text = (
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    # Example:
    #
    # Fernandez G., Mitsumori bis 13:25

    match = re.search(
        r"Fernandez\s+G\..{0,80}?\bbis\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if match:

        return match.group(1)

    return row.get(
        "end",
        ""
    )


def personal_end_note(
    row: dict
):

    text = (
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    match = re.search(
        r"Fernandez\s+G\..{0,80}?\bbis\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if match:

        return (
            f"bis {match.group(1)} "
            f"für Fernandez G."
        )

    return None


# ============================================================
# PERSONAL START TIMES
# ============================================================

def personal_start_note(
    row: dict,
    selected_rows_for_day: list
):

    text = (
        f"{row.get('dancers', '')} "
        f"{row.get('notes', '')}"
    )

    # Explicit:
    #
    # Fernandez G. ab 16:30

    explicit = re.search(
        r"Fernandez\s+G\..{0,80}?\bab\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if explicit:

        return (
            f"ab {explicit.group(1)}"
        )


    # Generic note:
    #
    # Bes. Rhapsody & WTGH ab 16:30

    generic = re.search(
        r"\bab\s+(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE
    )

    if not generic:

        return None

    text_n = norm(
        text
    )

    # Check if the note names another ballet/activity
    # that she is actually doing that same day.
    #
    # Friday:
    #
    # note = Bes. Rhapsody & WTGH ab 16:30
    #
    # she has Rhapsody that day
    #
    # -> yes, show ab 16:30
    #
    # Thursday:
    #
    # Bes. Galagesellschaft ab 13:35
    #
    # she has no selected Galagesellschaft rehearsal
    #
    # -> ignore

    for other in selected_rows_for_day:

        if other is row:
            continue

        other_piece = norm(
            other.get(
                "piece",
                ""
            )
        )

        if not other_piece:
            continue

        keyword = (
            other_piece.split()[0]
        )

        if (
            len(keyword)
            >= 5

            and keyword
            in text_n
        ):

            return (
                f"ab {generic.group(1)}"
            )

    return None


# ============================================================
# DISPLAY PIECE NAMES
# ============================================================

def pretty_piece_name(
    row: dict,
    cast_info: dict
) -> str:

    ballet = (
        find_cast_ballet_for_piece(
            row.get(
                "piece",
                ""
            ),
            cast_info
        )
    )

    if ballet:

        b = norm(
            ballet.get(
                "ballet",
                ""
            )
        )

        if "rhapsody" in b:

            return "Rhapsody"

        if "divertimento" in b:

            return "Divertimento"

    return (
        row.get(
            "piece",
            ""
        )
        or ""
    ).strip()


# ============================================================
# FORMAT TRAINING
# ============================================================

def format_training_rows(
    training_rows: list
) -> str:

    training_rows = sorted(
        training_rows,

        key=lambda r: (
            minutes(
                r.get(
                    "start",
                    "23:59"
                )
            ),

            0
            if normalize_studio(
                r.get(
                    "studio",
                    ""
                )
            ) == "BS1"

            else 1
        )
    )

    if not training_rows:

        return ""


    # ========================================================
    # NORMAL CASE:
    # TWO TRAINING OPTIONS
    # ========================================================

    if (
        len(training_rows)
        >= 2

        and

        training_rows[0].get(
            "start"
        )
        ==
        training_rows[1].get(
            "start"
        )

        and

        training_rows[0].get(
            "end"
        )
        ==
        training_rows[1].get(
            "end"
        )
    ):

        first = (
            training_rows[0]
        )

        second = (
            training_rows[1]
        )

        first_part = (
            f"{first.get('piece', '').strip()} "
            f"– "
            f"{normalize_studio(first.get('studio', ''))}"
        )

        first_staff = (
            first_staff_name(
                first.get(
                    "staff",
                    ""
                )
            )
        )

        if first_staff:

            first_part += (
                f" ({first_staff})"
            )


        second_part = (
            f"{second.get('piece', '').strip()} "
            f"– "
            f"{normalize_studio(second.get('studio', ''))}"
        )

        second_staff = (
            first_staff_name(
                second.get(
                    "staff",
                    ""
                )
            )
        )

        if second_staff:

            second_part += (
                f" ({second_staff})"
            )


        return (
            f"• **{first.get('start')}-{first.get('end')}** "
            f"{first_part} "
            f"ODER "
            f"{second_part}"
        )


    # ========================================================
    # ONE TRAINING ONLY
    # Example: Saturday freiwillig
    # ========================================================

    if len(
        training_rows
    ) == 1:

        row = (
            training_rows[0]
        )

        line = (
            f"• **{row.get('start')}-{row.get('end')}** "
            f"{row.get('piece', '').strip()} "
            f"– "
            f"{normalize_studio(row.get('studio', ''))}"
        )

        staff = (
            first_staff_name(
                row.get(
                    "staff",
                    ""
                )
            )
        )

        if staff:

            line += (
                f" ({staff})"
            )

        return line


    # ========================================================
    # RARE FALLBACK
    # ========================================================

    parts = []

    for row in training_rows:

        part = (
            f"**{row.get('start')}-{row.get('end')}** "
            f"{row.get('piece', '').strip()} "
            f"– "
            f"{normalize_studio(row.get('studio', ''))}"
        )

        staff = (
            first_staff_name(
                row.get(
                    "staff",
                    ""
                )
            )
        )

        if staff:

            part += (
                f" ({staff})"
            )

        parts.append(
            part
        )

    return (
        "• "
        + " ODER ".join(
            parts
        )
    )


# ============================================================
# FORMAT ONE REHEARSAL
# ============================================================

def format_rehearsal_row(
    row: dict,
    cast_info: dict,
    selected_rows_for_day: list
) -> str:

    start = (
        row.get(
            "start",
            ""
        )
    )

    end = (
        row.get(
            "end",
            ""
        )
    )

    studio = (
        normalize_studio(
            row.get(
                "studio",
                ""
            )
        )
    )

    dancers = (
        row.get(
            "dancers",
            ""
        )
        or ""
    ).strip()

    piece = (
        pretty_piece_name(
            row,
            cast_info
        )
    )

    line = (
        f"• **{start}-{end}** "
        f"{piece} "
        f"– "
        f"{studio}"
    )


    # ========================================================
    # DANCER CALL
    # ========================================================

    if dancers:

        line += (
            f" ({dancers})"
        )


    piece_n = norm(
        piece
    )


    # ========================================================
    # RHAPSODY STAFF
    # ========================================================

    # Desired:
    #
    # Ferri/Ishida -> Ferri
    # Gomes/Ishida -> Gomes

    if "rhapsody" in piece_n:

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

            coach = (
                first_staff_name(
                    staff
                )
            )

            if coach:

                line += (
                    f" {coach}"
                )


    # ========================================================
    # OTHER FUTURE BALLETS
    # ========================================================

    elif (
        "divertimento"
        not in piece_n
    ):

        coach = (
            first_staff_name(
                row.get(
                    "staff",
                    ""
                )
            )
        )

        if coach:

            line += (
                f" {coach}"
            )


    # ========================================================
    # PERSONAL "BIS" NOTE
    # ========================================================

    end_note = (
        personal_end_note(
            row
        )
    )

    if end_note:

        line += (
            f" – {end_note}"
        )


    # ========================================================
    # PERSONAL "AB" NOTE
    # ========================================================

    start_note = (
        personal_start_note(
            row,
            selected_rows_for_day
        )
    )

    if start_note:

        line += (
            f" – {start_note}"
        )


    return line


# ============================================================
# BUILD FINAL WHATSAPP MESSAGE
# ============================================================

def build_whatsapp_digest(
    schedule_data: dict,
    cast_info: dict
) -> str:

    sections = []
    closing_times = []

    for day in schedule_data.get(
        "days",
        []
    ):

        rows = (
            day.get(
                "rows",
                []
            )
        )


        # Normalize room names.

        for row in rows:

            row["studio"] = (
                normalize_studio(
                    row.get(
                        "studio",
                        ""
                    )
                )
            )


        # ====================================================
        # PYTHON SELECTS ONLY MY ROWS
        # ====================================================

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


        # Sort chronologically.

        selected.sort(
            key=lambda r: (
                minutes(
                    r.get(
                        "start",
                        "23:59"
                    )
                ),

                minutes(
                    r.get(
                        "end",
                        "23:59"
                    )
                )
            )
        )


        # ====================================================
        # TRAINING
        # ====================================================

        training_rows = [
            row

            for row in selected

            if is_normal_training(
                row
            )
        ]


        # ====================================================
        # REHEARSALS
        # ====================================================

        rehearsal_rows = [
            row

            for row in selected

            if not is_normal_training(
                row
            )
        ]


        lines = []


        # Training first.

        training_line = (
            format_training_rows(
                training_rows
            )
        )

        if training_line:

            lines.append(
                training_line
            )


        # Then rehearsals.

        for row in rehearsal_rows:

            lines.append(
                format_rehearsal_row(
                    row,
                    cast_info,
                    selected
                )
            )


        if not lines:
            continue


        # ====================================================
        # DAY HEADER
        # ====================================================

        day_short = (
            day_abbreviation(
                day.get(
                    "day",
                    ""
                )
            )
        )

        date = (
            day.get(
                "date",
                ""
            )
        )

        header = (
            f"**{day_short} {date}**"
        )

        section = (
            header
            + "\n\n"
            + "\n\n".join(
                lines
            )
        )

        sections.append(
            section
        )


        # ====================================================
        # FEIERABEND
        # ====================================================

        valid_end_rows = [
            row

            for row in selected

            if personal_end_time(
                row
            )
        ]

        if valid_end_rows:

            latest_row = max(
                valid_end_rows,

                key=lambda r:
                    minutes(
                        personal_end_time(
                            r
                        )
                    )
            )

            latest_end = (
                personal_end_time(
                    latest_row
                )
            )


            # Saturday example:
            #
            # only Training freiw.
            # -> Sa 11:45 (freiwillig)

            only_voluntary_training = (
                len(selected) == 1

                and

                is_normal_training(
                    selected[0]
                )

                and

                is_voluntary_training(
                    selected[0]
                )
            )

            suffix = (
                " (freiwillig)"
                if only_voluntary_training
                else ""
            )

            closing_times.append(
                f"{day_short} "
                f"{latest_end}"
                f"{suffix}"
            )


    # ========================================================
    # NO SCHEDULE
    # ========================================================

    if not sections:

        return (
            "Keine passenden Proben gefunden."
        )


    # ========================================================
    # FINAL MESSAGE
    # ========================================================

    digest = (
        "\n\n".join(
            sections
        )
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

def send_whatsapp(
    message: str
):

    id_instance = (
        os.environ[
            "GREEN_API_ID_INSTANCE"
        ]
    )

    api_token = (
        os.environ[
            "GREEN_API_TOKEN"
        ]
    )

    my_number = (
        os.environ[
            "MY_WHATSAPP_NUMBER"
        ]
    )

    chat_id = (
        f"{my_number}@c.us"
    )

    base_url = (
        os.environ.get(
            "GREEN_API_BASE_URL",
            "https://7105.api.greenapi.com"
        )
        .rstrip("/")
    )

    url = (
        f"{base_url}"
        f"/waInstance{id_instance}"
        f"/sendMessage/{api_token}"
    )

    payload = {
        "chatId":
            chat_id,

        "message":
            message
    }

    response = requests.post(
        url,
        json=payload,
        timeout=30
    )

    response.raise_for_status()


# ============================================================
# WEEKLY RUN
# ============================================================

@app.route(
    "/run-weekly",
    methods=[
        "GET",
        "POST"
    ]
)
def run_weekly():

    """
    NORMAL:

    /run-weekly?secret=YOUR_SECRET


    TEST WITHOUT SENDING WHATSAPP:

    /run-weekly?secret=YOUR_SECRET&dry=1

    Dry mode:
    - reads schedule
    - reads cast lists
    - creates digest
    - DOES NOT send WhatsApp
    - DOES NOT mark schedule processed
    """

    cron_secret = (
        os.environ.get(
            "CRON_SECRET"
        )
    )

    if (
        not cron_secret

        or

        request.args.get(
            "secret"
        )
        != cron_secret
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


    # ========================================================
    # 1. FIND THIS WEEK'S SCHEDULE
    # ========================================================

    (
        pdf_bytes,
        message_id,
        schedule_filename

    ) = (
        find_latest_schedule_pdf()
    )

    if not pdf_bytes:

        return (
            "no new weekly schedule found",
            200
        )


    # ========================================================
    # 2. CLAUDE READS SCHEDULE
    # ========================================================

    try:

        schedule_data = (
            call_claude_transcribe(
                pdf_bytes
            )
        )

    except Exception as exc:

        print(
            "❌ Schedule transcription failed:",
            exc
        )

        return (
            "schedule transcription failed; "
            f"not sending: {exc}",
            500
        )


    # ========================================================
    # 3. FIND CAST LISTS
    # ========================================================

    cast_pdfs = (
        find_cast_list_pdfs()
    )

    if not cast_pdfs:

        return (
            "no Cast List PDFs found in sent mail; "
            "not sending",
            500
        )


    # ========================================================
    # 4. CLAUDE READS MY CURRENT ROLES
    # ========================================================

    try:

        cast_info = (
            call_claude_extract_cast_roles(
                cast_pdfs
            )
        )

    except Exception as exc:

        print(
            "❌ Cast List extraction failed:",
            exc
        )

        return (
            "cast list extraction failed; "
            f"not sending: {exc}",
            500
        )


    if not cast_info.get(
        "ballets"
    ):

        return (
            "Cast Lists were found, but no roles "
            "for Fernandez G. were extracted. "
            "Not sending.",
            500
        )


    # ========================================================
    # 5. PYTHON BUILDS MY SCHEDULE
    # ========================================================

    digest = (
        build_whatsapp_digest(
            schedule_data,
            cast_info
        )
    )


    # ========================================================
    # DEBUG LOGS
    # ========================================================

    print(
        "\n"
        "================ SCHEDULE JSON ================"
        "\n"
    )

    print(
        json.dumps(
            schedule_data,
            ensure_ascii=False,
            indent=2
        )
    )


    print(
        "\n"
        "================ CAST INFO ===================="
        "\n"
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
        "================ DIGEST ======================="
        "\n"
    )

    print(
        digest
    )

    print(
        "\n"
        "================================================"
        "\n"
    )


    # ========================================================
    # DRY TEST
    # ========================================================

    if dry_run:

        return jsonify(
            {
                "dry_run":
                    True,

                "schedule_file":
                    schedule_filename,

                "cast_files":
                    [
                        item[
                            "filename"
                        ]

                        for item
                        in cast_pdfs
                    ],

                "cast_info":
                    cast_info,

                "schedule":
                    schedule_data,

                "digest":
                    digest
            }
        )


    # ========================================================
    # 6. SEND WHATSAPP
    # ========================================================

    try:

        send_whatsapp(
            digest
        )

    except Exception as exc:

        print(
            "❌ WhatsApp send failed:",
            exc
        )

        return (
            "WhatsApp send failed. "
            "Schedule was NOT marked processed. "
            f"Error: {exc}",
            500
        )


    # ========================================================
    # 7. MARK ONLY WEEKLY SCHEDULE EMAIL PROCESSED
    # ========================================================

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

    cron_secret = (
        os.environ.get(
            "CRON_SECRET"
        )
    )

    if (
        not cron_secret

        or

        request.args.get(
            "secret"
        )
        != cron_secret
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
# START APP
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
