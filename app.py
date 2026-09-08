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
schedule PDF (one page per day, Mon-Sun). Build a WhatsApp-ready digest for
the dancer "{MY_NAME}".

Her last known roles by ballet (use this as a fallback to match generic role
labels in the schedule):
{chr(10).join(f"- {ballet}: {role}" for ballet, role in MY_ROLES.items())}
"""

    if has_cast_list:
        base += """
A SECOND PDF is also attached: a current Cast List. Use it as the
authoritative source for her current roles - it may show new or updated
roles beyond the fallback list above. Cross-reference her name ("Fernandez
G.") in the Cast List to determine which roles/variations are hers, then
apply that when matching slots in the weekly schedule.
"""
    else:
        base += """
NO Cast List PDF was found this week. Rely only on the fallback roles listed
above. IMPORTANT: Start the digest with this exact warning line before
anything else:
"⚠️ Keine Cast List gefunden – Rollen basieren auf letztem bekannten Stand, bitte prüfen falls sich was geändert hat"
"""

    base += """
Rules:
1a. Include a slot ONLY if "Fernandez G." is literally named in it, OR the
    slot label is "Entire Cast" (always applies to everyone in that ballet),
    OR the slot label is a generic group that includes her role (e.g. "Alle
    Solodamen & Herren", "Solo Damen & Herren") - these apply to her.
1b. Do NOT include a slot just because it mentions "Solo Dame" if there are
    MULTIPLE solo dame variations in that ballet and the slot names a
    different variation number or different dancers than her - only match
    her specific variation/role, or her literal name.
1c. NEVER invent or assume a slot applies to her without one of the above
    being literally true in the source text. If unsure, leave it out rather
    than guess.
2. ALWAYS include BOTH training sessions for each day, with studio and
   teacher(s), even though she's only in one of them.
3. For each of her rehearsals, list: time, piece, studio, and who else is
   dancing/coaching in that slot.
4. Watch for notes like "ab 16:30" or "bis 13:30" - adjust the shown time to
   reflect her real call time, and note briefly why.
5. Watch for "ohne [Name]" exclusion notes - if she's excluded, leave the
   slot out entirely.
6. Skip days with nothing relevant beyond the two trainings.
7. Format: "**Day DD.MM**" header per day, then bullet lines. Keep compact.

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
    regardless of age. Returns the newest match's PDF, or None if none
    exists. This is independent of the weekly schedule search/label."""
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)

    query = f'subject:"{CAST_LIST_SUBJECT}" has:attachment'
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = results.get("messages", [])

    if not messages:
        return None

    msg_id = messages[0]["id"]
    msg = service.users().messages().get(userId="me", id=msg_id).execute()
    for part in msg["payload"].get("parts", []):
        if part["filename"].lower().endswith(".pdf"):
            att_id = part["body"]["attachmentId"]
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att_id
            ).execute()
            return base64.urlsafe_b64decode(att["data"])
    return None


def mark_as_processed(message_id):
    from googleapiclient.discovery import build

    creds = get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds)
    label_id = get_or_create_label(service)
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()


def call_claude_extraction(pdf_bytes: bytes, cast_list_bytes: bytes = None) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
    ]

    if cast_list_bytes:
        cast_b64 = base64.standard_b64encode(cast_list_bytes).decode("utf-8")
        content.append(
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": cast_b64}}
        )

    prompt = build_extraction_prompt(has_cast_list=cast_list_bytes is not None)
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

    cast_list_bytes = find_cast_list_pdf()
    digest = call_claude_extraction(pdf_bytes, cast_list_bytes)
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
