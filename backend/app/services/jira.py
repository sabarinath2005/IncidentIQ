"""Small Jira Cloud REST adapter with retry-safe create semantics."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit
import httpx

from app.services.connectors import validate_jira_base_url


class JiraProviderError(RuntimeError):
    pass


_RISK_PRIORITY = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Highest"}
_SUPPORTED_PRIORITIES = {"Highest", "High", "Medium", "Low", "Lowest"}


def jira_priority_for_risk(risk_level: object) -> str | None:
    """Map recommendation risk to common Jira priorities; unknown values use project default."""
    if not isinstance(risk_level, str):
        return None
    return _RISK_PRIORITY.get(risk_level.strip().casefold())


def incident_url_for_origin(frontend_origin: object, incident_id: object) -> str:
    if not isinstance(frontend_origin, str) or not isinstance(incident_id, str):
        return ""
    origin = frontend_origin.strip()
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return ""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", incident_id):
        return ""
    return origin.rstrip("/") + "/incidents/" + quote(incident_id, safe="")


def build_incident_description(*, incident_id: object, incident_title: object, incident_url: str,
        recommendation_description: object, expected_benefit: object = "", required_permissions=None,
        verification_steps=None, evidence_refs=None, investigation_context: object = "") -> str:
    def clean(value: object, limit: int) -> str:
        return str(value or "").strip()[:limit]

    sections = [
        "IncidentIQ incident reference",
        f"Incident ID: {clean(incident_id, 80)}",
        f"Incident title: {clean(incident_title, 240)}",
    ]
    if incident_url:
        sections.append(f"IncidentIQ link: {clean(incident_url, 1000)}")
    sections.extend(["", "Recommendation details", clean(recommendation_description, 3000)])
    benefit = clean(expected_benefit, 1000)
    if benefit:
        sections.extend(["", "Expected benefit", benefit])
    for label, values in (("Required permissions", required_permissions),
                          ("Verification steps", verification_steps), ("Evidence references", evidence_refs)):
        if isinstance(values, (list, tuple)) and values:
            safe_values = [clean(value, 500) for value in values[:30] if clean(value, 500)]
            if safe_values:
                sections.extend(["", label, *[f"- {value}" for value in safe_values]])
    context = clean(investigation_context, 1000)
    if context:
        sections.extend(["", "Investigation context", context])
    return "\n".join(sections)[:8000]


def _priority_was_rejected(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    errors = body.get("errors", {}) if isinstance(body, dict) else {}
    if isinstance(errors, dict) and any(str(field).casefold() == "priority" for field in errors):
        return True
    messages = body.get("errorMessages", []) if isinstance(body, dict) else []
    return isinstance(messages, list) and any("priority" in str(message).casefold() for message in messages)


class JiraAdapter:
    def __init__(self, base_url: str, email: str, api_token: str, transport: httpx.BaseTransport | None = None):
        self.base_url = validate_jira_base_url(base_url)
        self.email, self.api_token = email, api_token
        self.transport = transport

    def _client(self):
        return httpx.Client(auth=(self.email, self.api_token), timeout=httpx.Timeout(10.0),
                            transport=self.transport, headers={"Accept": "application/json"})

    def find_by_marker(self, project_key: str, marker: str) -> dict | None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", project_key) or not re.fullmatch(r"IIQ[A-F0-9]{32}", marker):
            raise JiraProviderError("Jira project mapping or idempotency marker is invalid")
        jql = f'project = "{project_key}" AND description ~ "{marker}" ORDER BY created DESC'
        try:
            with self._client() as client:
                response = client.get(self.base_url + "/rest/api/3/search/jql", params={
                    "jql": jql, "maxResults": 1, "fields": "status"})
        except httpx.HTTPError as exc:
            raise JiraProviderError("Could not verify whether Jira already created the approved issue") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise JiraProviderError("Jira issue lookup is temporarily unavailable")
        if response.status_code != 200:
            raise JiraProviderError(f"Jira issue lookup failed (HTTP {response.status_code})")
        try:
            matches = response.json().get("issues", [])
            return matches[0] if matches else None
        except (ValueError, AttributeError, TypeError) as exc:
            raise JiraProviderError("Jira returned an invalid issue lookup response") from exc

    def create_issue(self, *, project_key: str, summary: str, description: str, marker: str,
                     priority: str | None = None) -> dict:
        if not isinstance(project_key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", project_key):
            raise JiraProviderError("Jira project mapping is invalid")
        if not isinstance(summary, str) or not summary.strip():
            raise JiraProviderError("Jira issue summary is required")
        if not isinstance(description, str) or not description.strip():
            raise JiraProviderError("Jira issue description is required")
        if not isinstance(marker, str) or not re.fullmatch(r"IIQ[A-F0-9]{32}", marker):
            raise JiraProviderError("Jira issue idempotency marker is invalid")
        if not isinstance(priority, str) or priority not in _SUPPORTED_PRIORITIES:
            priority = None
        fields = {
            "project": {"key": project_key}, "issuetype": {"name": "Task"},
            "summary": summary.strip()[:250], "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line[:1000]}]}
                for line in description[:8000].splitlines() if line.strip()
            ] + [{"type": "paragraph", "content": [{"type": "text", "text": marker}]}]},
        }
        if priority:
            fields["priority"] = {"name": priority}
        payload = {"fields": fields}
        try:
            with self._client() as client:
                response = client.post(self.base_url + "/rest/api/3/issue", json=payload)
        except httpx.HTTPError as exc:
            # The next request always searches for the deterministic marker first.
            raise JiraProviderError("Jira issue creation outcome is uncertain; retry safely to reconcile") from exc
        if priority and _priority_was_rejected(response):
            # A definitive field-validation rejection means Jira did not create the issue.
            # Retry once with the project's configured default priority.
            payload["fields"].pop("priority", None)
            try:
                with self._client() as client:
                    response = client.post(self.base_url + "/rest/api/3/issue", json=payload)
            except httpx.HTTPError as exc:
                raise JiraProviderError("Jira issue creation outcome is uncertain; retry safely to reconcile") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise JiraProviderError("Jira issue creation outcome is uncertain; retry safely to reconcile")
        if response.is_error:
            raise JiraProviderError(f"Jira rejected the approved issue (HTTP {response.status_code})")
        try:
            data = response.json()
            if not data.get("id") or not data.get("key"):
                raise ValueError("missing id or key")
            return data
        except (ValueError, TypeError, AttributeError) as exc:
            raise JiraProviderError("Jira returned an invalid issue creation response") from exc

    def create_or_reconcile(self, *, project_key: str, summary: str, description: str, marker: str,
                            priority: str | None = None) -> dict:
        old = self.find_by_marker(project_key, marker)
        if old:
            return old
        return self.create_issue(project_key=project_key, summary=summary, description=description,
                                 marker=marker, priority=priority)

    def issue_status(self, issue_key: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}-[0-9]{1,12}", issue_key):
            raise JiraProviderError("Jira issue key is invalid")
        try:
            with self._client() as client:
                response = client.get(self.base_url + f"/rest/api/3/issue/{issue_key}",
                                      params={"fields": "status"})
        except httpx.HTTPError as exc:
            raise JiraProviderError("Jira status synchronization failed") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise JiraProviderError("Jira status synchronization is temporarily unavailable")
        if response.is_error:
            raise JiraProviderError(f"Jira status synchronization failed (HTTP {response.status_code})")
        try:
            return str(response.json()["fields"]["status"]["name"])[:80]
        except (ValueError, KeyError, TypeError) as exc:
            raise JiraProviderError("Jira returned an invalid status response") from exc
