"""Configuration for the GitLab review webhook channel."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator

from nanobot.config_base import Base

DEFAULT_PORT = 3980
DEFAULT_WEBHOOK_PATH = "/gitlab/webhook"
DEFAULT_CHAT_ID = "gitlab-review"


class GitLabReviewConfig(Base):
    """GitLab webhook listener that drafts reviews and publishes them on approval.

    GitLab authenticates webhooks with a shared secret in the ``X-Gitlab-Token``
    header, not a body signature, so that token is the whole trust boundary of
    the listener.

    ``reviewOwnMergeRequests`` also reviews merge requests the reviewer
    authored; approving them is never proposed or published.

    ``lessonsDir`` points at the reviewer's memory notes; lessons tagged with
    ``applies_to``/``keywords`` that match the merge request are put into the
    prompt by code (see :mod:`lessons`).

    Every message names the tracker tasks of the MR (keys from its title and
    description): name and link from ClickUp when ``clickupToken`` and
    ``clickupTeamId`` are set, otherwise a link under ``jiraUrl``.

    The reviewer never publishes on its own. Drafts go to one Telegram chat;
    only an approval sent from that chat makes the channel post to GitLab, with
    ``gitlabToken``. The comments appear under that token's account, a real
    person, so every guard that keeps the bot quiet lives in code here rather
    than in the prompt.
    """

    enabled: bool = False
    webhook_secret_token: str = ""
    host: str = "127.0.0.1"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    project_path: str = "astana-group/astana-motors"
    chat_id: str = DEFAULT_CHAT_ID

    gitlab_url: str = ""
    gitlab_token: str = ""
    reviewer_usernames: list[str] = Field(default_factory=list)
    review_own_merge_requests: bool = False
    lessons_dir: str = ""
    clickup_token: str = ""
    clickup_team_id: str = ""
    jira_url: str = ""
    task_key_pattern: str = r"[A-Z][A-Z0-9]+-\d+"
    lessons_budget_chars: int = Field(default=40000, ge=0)
    people_dir: str = ""
    people_budget_chars: int = Field(default=6000, ge=0)
    people_excluded: list[str] = Field(default_factory=list)
    people_profile_chars: int = Field(default=3000, ge=500)
    people_dream_hour: int = Field(default=4, ge=0, le=23)
    people_history_days: int = Field(default=90, ge=7)
    catch_up_days: int = Field(default=3, ge=0)
    attachments_dir: str = ""
    vpn_control: str = ""
    vpn_check_interval_s: float = Field(default=60.0, gt=0)
    vpn_reminder_interval_s: float = Field(default=3600.0, gt=0)
    vpn_reconnect_after_s: float = Field(default=300.0, gt=0)

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_user_ids: list[str] = Field(default_factory=list)

    debounce_seconds: float = Field(default=90.0, ge=0)
    max_runs_per_mr_per_hour: int = Field(default=6, ge=1)
    review_timeout_s: float = Field(default=3900.0, gt=0)

    @field_validator("webhook_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("must be an absolute URL path without a query or fragment")
        return path.rstrip("/") or "/"

    @field_validator("project_path")
    @classmethod
    def validate_project_path(cls, value: str) -> str:
        path = value.strip()
        if path.count("/") != 1 or not all(part for part in path.split("/")):
            raise ValueError("projectPath must look like 'group/project'")
        return path

    @field_validator("gitlab_url")
    @classmethod
    def validate_gitlab_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("gitlabUrl must start with https:// or http://")
        return url

    @field_validator("reviewer_usernames", "telegram_user_ids", "people_excluded")
    @classmethod
    def normalize_names(cls, value: list[str]) -> list[str]:
        return [str(name).strip() for name in value if str(name).strip()]

    @model_validator(mode="after")
    def validate_enabled(self) -> GitLabReviewConfig:
        if self.enabled:
            self.validate_runtime()
        return self

    def missing_settings(self) -> list[str]:
        """Settings without which the channel would review unsafely or uselessly.

        ``reviewerUsernames`` is required because it is what stops the bot from
        answering its own published comments in an endless loop. A group chat
        needs ``telegramUserIds``: there the chat id says nothing about who
        sent the approval.
        """
        required = {
            "webhookSecretToken": self.webhook_secret_token,
            "gitlabUrl": self.gitlab_url,
            "gitlabToken": self.gitlab_token,
            "telegramBotToken": self.telegram_bot_token,
            "telegramChatId": self.telegram_chat_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if not self.reviewer_usernames:
            missing.append("reviewerUsernames")
        if self.telegram_chat_id.strip().startswith("-") and not self.telegram_user_ids:
            missing.append("telegramUserIds")
        return missing

    def validate_runtime(self) -> None:
        missing = self.missing_settings()
        if missing:
            raise ValueError("Missing GitLab review settings: " + ", ".join(missing))

    def review_chat_id(self, iid: int) -> str:
        """Give every merge request its own agent session."""
        return f"{self.chat_id}:{iid}"

    def iid_from_chat_id(self, chat_id: str) -> int | None:
        prefix = f"{self.chat_id}:"
        if not chat_id.startswith(prefix):
            return None
        tail = chat_id[len(prefix):]
        return int(tail) if tail.isdigit() else None

    def is_approver(self, chat_id: str, sender_id: str) -> bool:
        """Only the configured chat, and in it only its owner or a listed user."""
        chat = self.telegram_chat_id.strip()
        if not chat or chat_id != chat or not sender_id:
            return False
        if sender_id in self.telegram_user_ids:
            return True
        return not chat.startswith("-") and sender_id == chat

    def people_path(self) -> str:
        """Where developer profiles live: ``peopleDir``, else ``people/`` under ``lessonsDir``."""
        if self.people_dir.strip():
            return self.people_dir.strip()
        if self.lessons_dir.strip():
            return str(Path(self.lessons_dir.strip()).expanduser() / "people")
        return ""

    def may_profile(self, username: str) -> bool:
        """Whether observations about *username* may be kept: not for those who opted out."""
        excluded = {name.casefold() for name in self.people_excluded}
        return bool(username) and username.casefold() not in excluded

    def is_reviewer(self, username: str | None) -> bool:
        if not username:
            return False
        folded = username.casefold()
        return any(folded == name.casefold() for name in self.reviewer_usernames)
