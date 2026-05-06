import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    gcp_project: str
    gcp_location: str
    use_vertex: bool
    model: str
    auth_mode: str
    firebase_project_id: str
    google_oauth_client_id: str
    seed_user_email: str
    seed_partner_email: str
    default_household_name: str
    firebase_api_key: str
    firebase_auth_domain: str
    firebase_app_id: str
    firebase_messaging_sender_id: str
    web_push_vapid_public_key: str
    web_push_vapid_private_key: str
    web_push_vapid_subject: str
    session_secret: str
    session_ttl_seconds: int


def settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "europe-north1"),
        use_vertex=os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true",
        model=os.getenv("BUDGET_AGENT_MODEL", "gemini-2.5-flash"),
        auth_mode=os.getenv("AUTH_MODE", "firebase").lower(),
        firebase_project_id=os.getenv("FIREBASE_PROJECT_ID", ""),
        google_oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
        seed_user_email=os.getenv("SEED_USER_EMAIL", "you@example.com"),
        seed_partner_email=os.getenv("SEED_PARTNER_EMAIL", "partner@example.com"),
        default_household_name=os.getenv("DEFAULT_HOUSEHOLD_NAME", "Grorudveien 39A"),
        firebase_api_key=os.getenv("FIREBASE_API_KEY", ""),
        firebase_auth_domain=os.getenv("FIREBASE_AUTH_DOMAIN", ""),
        firebase_app_id=os.getenv("FIREBASE_APP_ID", ""),
        firebase_messaging_sender_id=os.getenv("FIREBASE_MESSAGING_SENDER_ID", ""),
        web_push_vapid_public_key=os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", ""),
        web_push_vapid_private_key=os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", ""),
        web_push_vapid_subject=os.getenv("WEB_PUSH_VAPID_SUBJECT", "mailto:you@example.com"),
        # Used to sign the session cookie. MUST be set in production. The
        # dev fallback is intentionally weak and will print a warning on use
        # so it doesn't quietly survive into a real deploy.
        session_secret=os.getenv("BUDGET_AGENT_SESSION_SECRET", "dev-only-not-for-production"),
        session_ttl_seconds=int(os.getenv("BUDGET_AGENT_SESSION_TTL_SECONDS", str(30 * 24 * 3600))),
    )
