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
    Gruppe", "6 Damen & Mitsumori" - these refer to the corps/ensemble, not
    her solo role, even if the same day also has a real "Solo Dame" slot
    elsewhere. Be very careful not to confuse a headcount slot with the
    singular "Solo Dame" slot on the same day - they are different rows.
(e) Do NOT match a slot that lists specific dancer names ONLY (a fixed
    named list, e.g. "Lynch, Fredianelli, Cislaghi, Kim, Liz, Vandervelde,
    Cagnin") unless her name is literally among those listed. A named list
    that excludes her by omission is NOT hers, even if it's in a ballet she
    normally dances.
(f) If a "Variation" number is specified (e.g. "Variation 3") and it is not
    her Variation 6, it is NOT hers, even if the ballet matches.
(g) A slot labeled with "Bes. [date(s)]" (e.g. "Solo Damen & Herren Bes.
    18.09. & 25.09.", "3 Principal couples Bes. 25.09.") refers to a
    SPECIFIC PERFORMANCE cast assignment for those particular show dates,
    NOT a general availability rehearsal. Do NOT auto-match this via rule
    (c) unless her name is literally written in the slot - performance-
    specific casting can't be reliably inferred from the general role list.
    This applies even if the slot otherwise looks like a generic Solo
    Damen & Herren rehearsal.
(h) When genuinely unsure, leave the slot OUT rather than guess.

Before finalizing, do a second pass: explicitly check every day for any
slot labeled "Entire Cast" and any slot with her literal name that you may
have missed on the first pass, especially in side columns.

Content rules - what to include per matched slot:
1. Studio/room, always.
2. If the slot explicitly names specific dancers alongside her (e.g.
   "Fernandez G., Mitsumori" or "Trenary, Casalinho, Fernandez G."),
   ALWAYS include those names - do not drop them.
3. Teacher/coach names:
   - For Training sessions, ALWAYS include the teacher name.
   - For any Rhapsody slot that is hers, ALWAYS include the teacher/coach
     name(s).
   - For large generic Divertimento sessions with the standard recurring
     coaching team (e.g. Jennings/Necsea/Alosa/Kohoutková/Takizawa), you do
     NOT need to list the teacher name, unless it includes Ferri - if Ferri
     is among the teachers, always include "Ferri".
   - NEVER include the pianist/répétiteur. In a slash-separated name list
     (e.g. "Gomes/ Takizawa", "Ferri/ Ishida", "Jennings/ Necsea/
     Takizawa"), the LAST name is the pianist/répétiteur - drop it, keep
     only the name(s) before it as the teacher/coach(es).
4. Do not mention who else is dancing/coaching beyond the above, and do not
   mention which other dancers are excluded, late, or early - UNLESS that
   note directly affects HER OWN call time (e.g. "Fernandez G. bis 13:25").
5. Both training sessions of the day MUST be combined into ONE single
   bullet line joined by " ODER " - never two separate bullets for
   training. Each side shows its own studio and teacher (drop the
   pianist). Example of the REQUIRED format:
   "• **10:00-11:15** Training Blue Group – BS1 (Gomes) ODER Training
   Purple Group – BS2 (Rachedi)"
   Do NOT output two separate training bullets under any circumstances.
6. If a note changes HER OWN call time (e.g. "ab 16:30", "bis 13:30",
   "Fernandez G. bis 13:25"), adjust the shown time to reflect her real call
   time and add a brief 2-4 word reason.
7. If she is explicitly excluded ("ohne Fernandez G." or "ohne [her name]"),
   leave that slot out entirely.
8. Skip days with nothing relevant beyond the training line.
9. Format: "**Day DD.MM**" header per day, then one bullet per line, each
   starting with "•", with a blank line between bullets. Keep compact -
   this is read on a phone.
10. Bold every time/time-range shown, using markdown double-asterisks (e.g.
    "**10:00-11:15**").
11. At the very end of the digest, after all days, add a section titled
    "**Feierabend:**" listing, for each day that has any entries, the day
    abbreviation and the LATEST end time she has that day (when she is done
    for the day) - e.g.:
    Mo 14:20
    Di 14:20
    Mi 16:10
    Only include days that had at least one relevant slot for her. Use her
    adjusted/real end time (per rule 6) when computing this, not the
    printed slot time if a note changed her actual end time. If her only
    relevant slot that day is a voluntary training ("Training freiw."),
    still include it and label it "(freiwillig)".

CRITICAL OUTPUT RULE: The output must be the clean final digest ONLY. Never
include your own reasoning, checkmarks (✅/❌), exclusion notes about why a
slot was left out, or any meta-commentary. Just silently omit anything that
doesn't belong to her - the reader should never see your decision process.
If the attached weekly schedule PDF is missing or unreadable, still do your
best with whatever is legible rather than refusing outright.

FINAL SELF-CHECK before outputting - go through this checklist explicitly
for every day, one item at a time:
□ Did I combine both trainings into ONE line with " ODER ", each showing
  its own teacher (not the pianist)?
□ For every Rhapsody slot I included, did I include the teacher/coach name
  (not the pianist)?
□ For every slot with named dancers alongside her, did I include those
  names?
□ Did I check every slot for a "Bes. [date]" label and EXCLUDE it unless
  her name is literally written in it, even if the general group label
  would otherwise match?
□ Did I check every day for "Entire Cast" slots I might have missed?
□ Did I re-verify that any headcount+gender slot (e.g. "6 Damen & X") is
  NOT included, and that I didn't mix it up with a real "Solo Dame"
  (singular) slot on the same day?
□ Did I include any "bis HH:MM [her name]" or "ab HH:MM" notes that affect
  her own call time?
Fix any gaps found in this check before producing the final output.

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


def find_cast_list_
