import os
import json
import base64
from flask import Flask, request, redirect, session
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "temporary-insecure-key-please-set-real-one")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_raw_client_config = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
CLIENT_CONFIG = json.loads(_raw_client_config) if _raw_client_config else None
REDIRECT_URI = os.environ.get("REDIRECT_URI")

TOKEN_FILE = "gmail_token.json"

MY_NAME = "Fernandez G."
SENDER_EMAIL = "ismenia.keck@wiener-staatsballett.at"
BACKUP_SUBJECT = "Cast list"
CAST_LIST_SUBJECT = "Cast list"

MY_ROLES = {
    "Rhapsody": "Solo Dame (only one Solo Dame role in this piece; partnered with Mitsumori)",
    "Divertimento No. 15": "Solo Dame, Variation 6 (one of several Solo Dame variations - "
                            "only slots naming 'Variation 6' or her actual name are hers, "
                            "NOT other Solo Dame variations)",
}


def build_extraction_prompt(has_cast_list: bool) -> str:
    base = f"""You are reading a Wiener Staatsballett weekly rehearsal
schedule PDF (one page per day, Mon-Sun, with multiple room columns per
page: Ballettsaal 1, Ballettsaal 2, BS3, BAK/Anproben, div. Orte, Gäste,
Sonstiges).

Her last known roles by ballet (use this as a fallback to match generic role
labels in the schedule):
{chr(10).join(f"- {ballet}: {role}" for ballet, role in MY_ROLES.items())}
"""

    if has_cast_list:
        base += """
One or more additional PDFs are also attached: Cast Lists, one per ballet
(e.g. Rhapsody, Divertimento No. 15, and possibly others like Nijinsky that
are NOT relevant to her). Only use the Cast List(s) for ballets she actually
appears in this week's schedule. Cross-reference her name ("Fernandez G.")
in the relevant Cast List(s) to determine which roles/variations are hers,
then apply that when matching slots in the weekly schedule. Ignore Cast
Lists for ballets she has no role in.
"""
    else:
        base += """
NO Cast List PDF was found this week. Rely only on the fallback roles listed
above. IMPORTANT: Start the digest with this exact warning line before
anything else:
"⚠️ Keine Cast List gefunden – Rollen basieren auf letztem bekannten Stand, bitte prüfen falls sich was geändert hat"
"""

    base += """
CRITICAL - scan every single column on every single day, including the
BAK/Anproben and "div. Orte" columns. These are NOT purely administrative -
they often contain real rehearsals in named rooms (e.g. Hilverdingsaal,
Wiesenthalsaal, Elsslersaal, Hankasaal, Wiesenthalsaal), mixed in with
costume-fitting ("Anprobe") entries. Do not skip these columns. A rehearsal
slot with her name in it can appear in ANY column, not just Ballettsaal 1/2/3.

Matching rules - a slot belongs to her if AND ONLY IF one of these is
literally true:
(a) Her name "Fernandez G." is literally printed in that slot.
(b) The slot is labeled "Entire Cast" - this ALWAYS includes her, for any
    ballet she has a role in. Do not skip these - actively look for the
    exact phrase "Entire Cast" on every single day and studio column.
(c) The slot uses a GENERIC group label that matches her role type:
    - "Alle Solodamen & Herren" / "Solo Damen & Herren" / "alle available
      Solo Da. & Herr." -> matches her (she is a Solo Dame).
    - For Rhapsody specifically: the label "Solo Dame" (SINGULAR) refers
      directly to her, since she is the only Solo Dame in that piece. Match
      it even if the slot also lists other named dancers (e.g. "Solo Dame &
      6 Herren").
(d) Do NOT match slots with a SPECIFIC HEADCOUNT + gender label that is NOT
    the word "Solo", e.g. "6 Damen & 6 Herren", "9 Bakst Damen", "8 Damen
    Gruppe" - these refer to the corps/ensemble, not her solo role, even if
    the same day also has a Rhapsody or Divertimento slot.
(e) Do NOT match a slot that lists specific dancer names ONLY (a fixed
    named list, e.g. "Lynch, Fredianelli, Cislaghi, Kim, Liz, Vandervelde,
    Cagnin") unless her name is literally among those listed. A named list
    that excludes her by omission is NOT hers, even if it's in a ballet she
    normally dances.
(f) If a "Variation" number is specified (e.g. "Variation 3") and it is not
    her Variation 6, it is NOT hers, even if the ballet matches.
(g) When genuinely unsure, leave the slot OUT rather than guess.

Before finalizing, do a second pass: explicitly check every day for any
slot labeled "Entire Cast" and any slot with her literal name that you may
have missed on the first pass, especially in side columns.

Other rules:
1. ALWAYS include BOTH training sessions for each day, with studio and
   teacher(s), even though she's only in one of them.
2. For each of her rehearsals, list only: time, piece, studio. Do not list
   who else is dancing, coaching, or which other dancers are excluded/late/
   early - UNLESS that note directly affects HER call time (e.g. "Fernandez
   G. bis 13:25" or an "ab HH:MM" note that changes when she personally
   needs to arrive/can leave).
3. If a note changes HER OWN call time (e.g. "ab 16:30", "bis 13:30",
   "Fernandez G. bis 13:25"), adjust the shown time to reflect her real call
   time and add a brief 2-4 word reason.
4. If she is explicitly excluded ("ohne Fernandez G." or "ohne [her name]"),
   leave that slot out entirely.
5. Skip days with nothing relevant beyond the two trainings.
6. Format: "**Day DD.MM**" header per day, then bullet lines. Keep compact -
   this is read on a phone. No extra commentary, no "who's dancing with
   whom" unless it's her own partner in a named slot.

CRITICAL OUTPUT RULE: The output must be the clean final digest ONLY. Never
include your own reasoning, checkmarks (✅/❌), exclusion notes about why a
slot was left out, or any meta-commentary. Just silently omit anything that
doesn't belong to her - the reader should never see your decision process.
If the attached weekly schedule PDF is missing or unreadable, still do your
best with whatever is legible rather than refusing outright.

Output ONLY the digest text, ready to send as-is on WhatsApp. No preamble.
"""
    return base


def get_gmail_credentials():
    """Prefer the token stored in the GMAIL_TOKEN_JSON env var (survives
    restarts). Fall back to the local file (works within one running
    instance, e.g. right after a fresh /auth)."""
    env_token = os.environ.get("GMAIL_TOKEN_JSON")
    if env_token:
        try:
            return Credentials.from_authorized_user_info(json.loads(env_token), SCOPES)
        except Exception:
            pass
    if not os.path.exists(TOKEN_FILE):
        return None
    return Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)


@app.route("/")
def home():
    creds = get_gmail_credentials()
    gmail_status = "✅ Gmail connected" if creds else "❌ Not connected yet"
    config_status = "✅ Google client config loaded" if CLIENT_CONFIG else "❌ GOOGLE_CLIENT_SECRET_JSON missing/invalid"
    redirect_status = f"Redirect URI: {REDIRECT_URI or '❌ REDIRECT_URI missing'}"
    return (
        f"Schedule Bot is running.<br>{gmail_status}<br>{config_status}<br>{redirect_status}"
        f"<br><a href='/auth'>Connect / Reconnect Gmail</a>"
    )


@app.route("/auth")
def auth():
    if not CLIENT_CONFIG or not REDIRECT_URI:
        return "Missing GOOGLE_CLIENT_SECRET_JSON or REDIRECT_URI environment variable. Check Render settings.", 500
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    flow = Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI, state=session["state"]
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    token_json = creds.to_json()
    with open(TOKEN_FILE, "w") as f:
        f.write(token_json)
    return (
        "Gmail connected! To make this survive server restarts, copy the text box below "
        "and paste it as a NEW Render environment variable named GMAIL_TOKEN_JSON "
        "(Render dashboard > your service > Environment > Add Environment Variable), "
        "then save.<br><br>"
        f"<textarea readonly style='width:95%;height:150px;'>{token_json}</textarea>"
    )


LABEL_NAME = "ScheduleBotProcessed"


def get_or_create_label(service):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == LABEL_NAME:
            return lbl["id"]
    new_label = service.users().labels().create(
        userId="me", body={"name": LABEL_NAME, "labelListVisibility": "labelHide", "messageListVisibility": "hide"}
    ).execute()
    return new_label["id"]


def find_latest_schedule_pdf():
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)
    get_or_create_label(service)

    query = f'from:{SENDER_EMAIL} has:attachment newer_than:7d -label:{LABEL_NAME}'
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = results.get("messages", [])

    if not messages:
        query = f'subject:"{BACKUP_SUBJECT}" has:attachment newer_than:7d -label:{LABEL_NAME}'
        results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        messages = results.get("messages", [])

    if not messages:
        return None, None

    msg_id = messages[0]["id"]
    msg = service.users().messages().get(userId="me", id=msg_id).execute()
    for part in msg["payload"].get("parts", []):
        if part["filename"].lower().endswith(".pdf"):
            att_id = part["body"]["attachmentId"]
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att_id
            ).execute()
            pdf_bytes = base64.urlsafe_b64decode(att["data"])
            return pdf_bytes, msg_id
    return None, None


def find_cast_list_pdf():
    """Looks for an email with 'Cast list' in the subject, from anyone,
    regardless of age. Returns a LIST of all PDF attachments found (the
    email may contain one PDF per ballet, e.g. Rhapsody, Divertimento,
    Nijinsky). Returns an empty list if none found."""
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)

    query = f'subject:"{CAST_LIST_SUBJECT}" has:attachment'
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = results.get("messages", [])

    if not messages:
        return []

    msg_id = messages[0]["id"]
    msg = service.users().messages().get(userId="me", id=msg_id).execute()

    pdfs = []
    for part in msg["payload"].get("parts", []):
        if part["filename"].lower().endswith(".pdf"):
            att_id = part["body"]["attachmentId"]
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att_id
            ).execute()
            pdfs.append(base64.urlsafe_b64decode(att["data"]))
    return pdfs


def mark_as_processed(message_id):
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)
    label_id = get_or_create_label(service)
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()


def call_claude_extraction(pdf_bytes: bytes, cast_list_pdfs: list = None) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
    ]

    cast_list_pdfs = cast_list_pdfs or []
    for cast_pdf in cast_list_pdfs:
        cast_b64 = base64.standard_b64encode(cast_pdf).decode("utf-8")
        content.append(
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": cast_b64}}
        )

    prompt = build_extraction_prompt(has_cast_list=len(cast_list_pdfs) > 0)
    content.append({"type": "text", "text": prompt})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def send_whatsapp(message: str):
    import requests

    id_instance = "710522730585"
    api_token = "a11f9c07c9454de4b706b1c38da02fb6af528d66bbe045f19a"
    chat_id = f"{os.environ['MY_WHATSAPP_NUMBER']}@c.us"

    url = f"https://7105.api.greenapi.com/waInstance{id_instance}/sendMessage/{api_token}"
    payload = {
        "chatId": chat_id,
        "message": message,
    }
    response = requests.post(url, json=payload)
    response.raise_for_status()


@app.route("/run-weekly", methods=["POST", "GET"])
def run_weekly():
    """Triggered by cron-job.org, hourly on Fridays."""
    if request.args.get("secret") != os.environ["CRON_SECRET"]:
        return "unauthorized", 401

    pdf_bytes, message_id = find_latest_schedule_pdf()
    if not pdf_bytes:
        return "no new schedule email found", 200

    cast_list_pdfs = find_cast_list_pdf()
    digest = call_claude_extraction(pdf_bytes, cast_list_pdfs)
    send_whatsapp(digest)
    mark_as_processed(message_id)
    return "sent", 200


@app.route("/test-whatsapp", methods=["GET"])
def test_whatsapp():
    if request.args.get("secret") != os.environ["CRON_SECRET"]:
        return "unauthorized", 401
    send_whatsapp("Test-Nachricht vom Bot 🎉")
    return "test sent", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
