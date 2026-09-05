import os
import json
import base64
from flask import Flask, request, redirect, session
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import anthropic

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_CONFIG = json.loads(os.environ["GOOGLE_CLIENT_SECRET_JSON"])
REDIRECT_URI = os.environ["REDIRECT_URI"]

TOKEN_FILE = "gmail_token.json"

MY_NAME = "Fernandez G."
SENDER_EMAIL = "ismenia.keck@wiener-staatsballett.at"
BACKUP_SUBJECT = "Cast list"

MY_ROLES = {
    "Rhapsody": "Solo Dame (only one Solo Dame role in this piece; partnered with Mitsumori)",
    "Divertimento No. 15": "Solo Dame, Variation 6 (one of several Solo Dame variations - "
                            "only slots naming 'Variation 6' or her actual name are hers, "
                            "NOT other Solo Dame variations)",
}

EXTRACTION_PROMPT = f"""You are reading a Wiener Staatsballett weekly rehearsal
schedule PDF (one page per day, Mon-Sun). Build a WhatsApp-ready digest for
the dancer "{MY_NAME}".

Her roles by ballet (use this to match generic role labels in the schedule):
{chr(10).join(f"- {ballet}: {role}" for ballet, role in MY_ROLES.items())}

Rules:
1a. Include a slot ONLY if "{MY_NAME}" is literally named in it, OR the slot
    label is "Entire Cast" (always applies to everyone in that ballet), OR the
    slot label is a generic group that includes her role (e.g. "Alle
    Solodamen & Herren", "Solo Damen & Herren") - these apply to her.
1b. Do NOT include a slot just because it mentions "Solo Dame" if there are
    MULTIPLE solo dame variations in that ballet and the slot names a
    different variation
