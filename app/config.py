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
    dev_user_email: str
    dev_household_name: str


def settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "europe-north1"),
        use_vertex=os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true",
        model=os.getenv("BUDGET_AGENT_MODEL", "gemini-2.5-flash"),
        dev_user_email=os.getenv("DEV_DEFAULT_USER_EMAIL", "you@example.com"),
        dev_household_name=os.getenv("DEV_DEFAULT_HOUSEHOLD_NAME", "Grorudveien 39A"),
    )
