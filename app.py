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

TRANSCRIPTION_PROMPT = """You are transcribing a Wiener Staatsballett weekly
rehearsal schedule PDF (one page per day, Mon-Sun, with multiple room
columns per page: Ballettsaal 1, Ballettsaal 2, BS3, BAK/Anproben, div.
Orte, Gäste, Sonstiges - column names vary slightly by day).

Your ONLY job is to transcribe EVERY slot from EVERY column on EVERY day,
completely and literally, in reading order. Do NOT decide what is
relevant, do NOT filter anything out, do NOT summarize, do NOT skip
anything - including entries that look purely administrative, costume
fittings ("Anprobe"), or entries tucked into side columns like
"BAK/Anproben" or "div. Orte". Those side columns often contain real
rehearsals in named rooms (e.g. Hilverdingsaal, Wiesenthalsaal,
Elsslersaal, Hankasaal, Wiesenthalsaal) - transcribe those too.

For each day, output exactly in this format:

=== <Weekday>, DD.MM ===
<studio/column name> | <HH:MM-HH:MM> | <piece/activity name> | <ALL dancer
names listed in that slot, exactly as printed> | <ALL teacher/coach/pianist
names exactly as printed, including slashes> | <any notes: "ohne ...", "n.
Mög.", "Bes. [date]", "ab/bis" time adjustments, anything else printed in
that slot>

One line per slot. Use "-" for any field that is empty/not applicable, but
never drop a field - always keep the five " | "-separated parts (piece,
names, teachers, notes) after studio and time. Preserve exact wording,
spelling, and abbreviations of names and notes - do not paraphrase.

Output ONLY the transcript in this format, nothing else. No commentary, no
introduction, no summary."""


def build_digest_prompt(has_cast_list: bool) -> str:
    base = f"""You are given a literal transcript of a Wiener Staatsballett
weekly rehearsal schedule (produced by a separate transcription pass), for
the dancer "{MY_NAME}". Build a WhatsApp-ready digest for her.

Her last known roles by ballet (use this as a fallback to match generic role
labels in the schedule):
{chr(10).join(f"- {ballet}: {role}" for ballet, role in MY_ROLES.items())}
"""

    if has_cast_list:
        base += """
One or more Cast List PDFs are also attached, one per ballet (e.g.
Rhapsody, Divertimento No. 15, and possibly others like Nijinsky that are
NOT relevant to her). Only use the Cast List(s) for ballets she actually
appears in this week's transcript. Cross-reference her name ("Fernandez
G.") in the relevant Cast List(s) to determine which roles/variations are
hers, then apply that when matching slots in the transcript. Ignore Cast
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
The transcript format is:
studio | time | piece | dancer names | teacher/pianist names | notes

Matching rules - a slot belongs to her if AND ONLY IF one of these is
literally true in the transcript's "dancer names" or "piece" field:
(a) Her name "Fernandez G." is literally in the dancer names field.
(b) The piece field says "Entire Cast" - this ALWAYS includes her, for any
    ballet she has a role in. Actively check every single transcribed line
    for this exact phrase.
(c) The dancer names field is a GENERIC group label matching her role type:
    - "Alle Solodamen & Herren" / "Solo Damen & Herren" / "alle available
      Solo Da. & Herr." -> matches her (she is a Solo Dame). EXCEPTION: see
      rule (g) below.
    - For Rhapsody specifically: the label "Solo Dame" (SINGULAR) refers
      directly to her, since she is the only Solo Dame in that piece. Match
      it even if other named dancers are also listed (e.g. "Solo Dame & 6
      Herren").
(d) Do NOT match a dancer-names field that is a HEADCOUNT + gender label
    NOT containing the word "Solo", e.g. "6 Damen & 6 Herren", "9 Bakst
    Damen", "8 Damen Gruppe", "6 Damen & Mitsumori" - these are the
    corps/ensemble, not her. A day may have BOTH a headcount line (not
    hers) and a separate "Solo Dame" line (hers) - check the exact wording
    of each transcript line independently, do not assume based on the
    piece name alone.
(e) Do NOT match a dancer-names field that is a fixed named list NOT
    including her (e.g. "Lynch, Fredianelli, Cislaghi, Kim, Liz,
    Vandervelde, Cagnin" or "Trenary, Casalinho" without her name) - a
    named list that omits her is not hers, even in a ballet she normally
    dances.
(f) If a "Variation" number is specified and it is not her Variation 6, it
    is NOT hers, even if the ballet matches.
(g) CRITICAL - if the notes field or dancer-names field contains "Bes.
    [date]" (e.g. "Bes. 18.09. & 25.09."), this is a specific PERFORMANCE
    cast assignment for those show dates, NOT a general rehearsal. This
    OVERRIDES rule (c) - exclude it unless her name is literally in the
    dancer-names field for that same line.
(h) When genuinely unsure, leave the slot OUT rather than guess.

Do a second full pass over the entire transcript checking specifically for
any line with piece="Entire Cast" or dancer-names containing her literal
name that you may have missed.

Content rules - what to include per matched slot:
1. Studio/room, always.
2. If the dancer-names field lists specific dancers alongside her, include
   those names exactly as transcribed.
3. Include the teacher/pianist names field as transcribed, for Training and
   Rhapsody slots always; for large generic Divertimento sessions with the
   standard recurring team you may omit it unless "Ferri" appears in it (if
   so, always include Ferri).
4. Do not add any other information from the notes field about who else is
   excluded/late/early UNLESS it affects HER OWN call time (e.g. "Fernandez
   G. bis 13:25").
5. Both training lines for a day MUST be combined into ONE bullet joined by
   " ODER ", each with its own studio and teacher/pianist names. Required
   format: "• **10:00-11:15** Training Blue Group – BS1 (Gomes/Takizawa)
   ODER Training Purple Group – BS2 (Rachedi/Zapravdin)"
6. If the notes field has a time-adjustment affecting HER (e.g. "ab
   16:30", "bis 13:30", "Fernandez G. bis 13:25"), adjust the shown time
   and add a brief 2-4 word reason.
7. If the notes field says she's excluded ("ohne Fernandez G." or "ohne
   [her name]"), leave that slot out entirely.
8. Skip days with nothing relevant beyond the training line.
9. Format: "**Day DD.MM**" header per day, then one bullet per line
   starting with "•", blank line between bullets. Keep compact.
10. Bold every time/time-range using markdown double-asterisks.
11. At the very end, add "**Feierabend:**" listing, per day with any
    entries, the day abbreviation and her LATEST end time that day (using
    her adjusted end time per rule 6, not the raw printed time if a note
    changed it). If her only slot that day is voluntary training, include
    it labeled "(freiwillig)".

CRITICAL OUTPUT RULE: Output the clean final digest ONLY. Never include
reasoning, checkmarks, or meta-commentary about why something was
included/excluded - just silently apply the rules.

FINAL SELF-CHECK before outputting, go through explicitly for every day:
□ Both trainings combined into one " ODER " line with teacher/pianist
  names?
□ Every included Rhapsody line has teacher/pianist names?
□ Every line with named dancers alongside her keeps those names?
□ Every line checked for "Bes. [date]" and excluded unless her name is
  literally present on that line?
□ Every "Entire Cast" line found and included?
□ No headcount-only line (e.g. "6 Damen & X") confused with a real "Solo
  Dame" (singular) line?
□ Any "bis HH:MM [her name]" or "ab HH:MM" note reflected in her time?
Fix any gaps before finalizing.

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


def call_claude_transcribe(pdf_bytes: bytes) -> str:
    """Step 1: pure transcription of the schedule PDF, no filtering."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": TRANSCRIPTION_PROMPT},
            ],
        }],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def call_claude_digest(transcript: str, cast_list_pdfs: list = None) -> str:
    """Step 2: build the filtered, formatted digest from the transcript."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content = [
        {"type": "text", "text": f"TRANSCRIPT:\n\n{transcript}"},
    ]

    cast_list_pdfs = cast_list_pdfs or []
    for cast_pdf in cast_list_pdfs:
        cast_b64 = base64.standard_b64encode(cast_pdf).decode("utf-8")
        content.append(
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": cast_b64}}
        )

    prompt = build_digest_prompt(has_cast_list=len(cast_list_pdfs) > 0)
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

    transcript = call_claude_transcribe(pdf_bytes)
    cast_list_pdfs = find_cast_list_pdf()
    digest = call_claude_digest(transcript, cast_list_pdfs)
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
