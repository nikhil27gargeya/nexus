import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

FAULT_WORDS = ("sorry", "apolog", "outage", "incident", "error", "fault", "let you down", "our mistake", "on us")

# Whole-word match from the start of a word. Single words may take an ending ("outages"); phrases must end on a
# word boundary, so "focus on usability" and "coincidentally" don't match.
_FAULT_RE = re.compile(
    "|".join(rf"\b{re.escape(w)}" + (r"\b" if " " in w else r"\w*\b") for w in FAULT_WORDS), re.IGNORECASE
)


def fault_words_in(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _FAULT_RE.finditer(text)})


class GateConfig(BaseModel):
    min_errors: int = 10
    max_days_from_outage: int = 1
    min_feature_share: float = 0.5
    min_active_members: int = 1
    min_confidence: float = 0.75


FEATURE_LABELS = {"sync": "data syncing"}


def feature_label(feature: str) -> str:
    return FEATURE_LABELS.get(feature, feature)


DEFAULT_FEATURES = {
    "SyncTimeoutError": "sync",
    "WebhookRetryExceeded": "sync",
    "ExportRenderWarning": "export",
    "AvatarUploadFailed": "profile",
    "Sync jobs timing out": "sync",
}


class RemediationConfig(BaseModel):
    credit_months: int = 2
    headline: str = "Before you go, our systems had issues, and we want to make it right."
    accept_label: str = "Keep {plan} with credit"
    decline_label: str = "Cancel anyway"


class StandardOfferConfig(BaseModel):
    kind: Literal["percent_off", "pause", "none"] = "percent_off"
    percent_off: int = 20
    months: int = 3
    headline: str = "Before you go, stay for {percent_off}% off"
    body: str = "Keep your {plan} plan at {percent_off}% off for the next {months} months."
    accept_label: str = "Keep {plan} at {percent_off}% off"
    decline_label: str = "Cancel subscription"

    @model_validator(mode="after")
    def must_not_admit_fault(self) -> "StandardOfferConfig":
        found = fault_words_in(" ".join([self.headline, self.body, self.accept_label, self.decline_label]))
        if found:
            raise ValueError(f"standard_offer must not admit fault without proof; found {found}")
        return self


class BusinessConfig(BaseModel):
    product: str = "Mergeline"
    remediation: RemediationConfig = RemediationConfig()
    standard_offer: StandardOfferConfig = StandardOfferConfig()
    features: dict[str, str] = DEFAULT_FEATURES


def load_business(path: Path) -> BusinessConfig:
    if not path.exists():
        return BusinessConfig()
    return BusinessConfig.model_validate(tomllib.loads(path.read_text()))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"), extra="ignore"
    )

    anthropic_api_key: str = ""
    agent_model: str = "claude-sonnet-5"
    agent_max_turns: int = 10
    agent_max_tokens: int = 4096

    fixtures_dir: Path = REPO_ROOT / "fixtures"
    business_config: Path = REPO_ROOT / "backend" / "config" / "business.toml"
    window_days: int = 56

    mixpanel_project_token: str = ""
    mixpanel_project_id: str = ""
    mixpanel_service_account_user: str = ""
    mixpanel_service_account_secret: str = ""
    mixpanel_region: str = "api"

    sentry_auth_token: str = ""
    sentry_org: str = ""
    sentry_project: str = "mergeline-sync"
    sentry_dsn: str = ""

    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"

    stripe_secret_key: str = ""

    cors_origins: str = "http://localhost:5173,http://localhost:8000"

    gates: GateConfig = GateConfig()


settings = Settings()
