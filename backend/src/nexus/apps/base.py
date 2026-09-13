from datetime import date
from typing import Any, Protocol

from nexus.models import Account


class AppError(RuntimeError):
    pass


class Apps(Protocol):
    def cancel_date(self) -> date: ...

    def stripe_account(self) -> Account: ...

    def sentry_find_user_issues(self, user_id: str, since: str, until: str) -> list[dict[str, Any]]: ...

    def sentry_get_issue(self, issue_id: str, user_id: str) -> dict[str, Any] | None: ...

    def datadog_find_incidents(self, region: str | None, since: str, until: str) -> list[dict[str, Any]]: ...

    def datadog_get_incident(self, incident_id: str) -> dict[str, Any] | None: ...

    def mixpanel_feature_usage(self, user_id: str, since: str, until: str, day: str) -> dict[str, Any]: ...
