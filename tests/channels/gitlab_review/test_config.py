from typing import Any

import pytest

from nanobot.channels.gitlab_review.config import GitLabReviewConfig
from nanobot.channels.gitlab_review.validation import validate

FULL: dict[str, Any] = {
    "enabled": True,
    "webhookSecretToken": "tok",
    "gitlabUrl": "https://gitlab.example.com/",
    "gitlabToken": "glpat",
    "reviewerUsernames": ["a.tyra"],
    "telegramBotToken": "123:abc",
    "telegramChatId": "49816954",
}


def test_config_accepts_camel_case_keys() -> None:
    config = GitLabReviewConfig.model_validate({**FULL, "webhookPath": "/hook"})

    assert config.webhook_secret_token == "tok"
    assert config.reviewer_usernames == ["a.tyra"]
    assert config.webhook_path == "/hook"
    assert config.gitlab_url == "https://gitlab.example.com"


@pytest.mark.parametrize(
    "missing",
    ["webhookSecretToken", "gitlabUrl", "gitlabToken", "telegramBotToken", "telegramChatId"],
)
def test_enabled_requires_every_setting(missing: str) -> None:
    values = {**FULL}
    values.pop(missing)
    with pytest.raises(ValueError, match=missing):
        GitLabReviewConfig.model_validate(values)


def test_enabled_requires_reviewer_usernames() -> None:
    """Without them the bot's own comments would wake it again, forever."""
    with pytest.raises(ValueError, match="reviewerUsernames"):
        GitLabReviewConfig.model_validate({**FULL, "reviewerUsernames": ["  "]})


@pytest.mark.parametrize("project_path", ["nogroup", "a/b/c", "", "/x", "x/"])
def test_malformed_project_path_is_rejected(project_path: str) -> None:
    with pytest.raises(ValueError):
        GitLabReviewConfig.model_validate({**FULL, "projectPath": project_path})


@pytest.mark.parametrize("path", ["relative", "with?query", "with#frag"])
def test_malformed_webhook_path_is_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        GitLabReviewConfig.model_validate({**FULL, "webhookPath": path})


def test_gitlab_url_must_be_http() -> None:
    with pytest.raises(ValueError):
        GitLabReviewConfig.model_validate({**FULL, "gitlabUrl": "gitlab.example.com"})


def test_reviewer_match_is_case_insensitive() -> None:
    config = GitLabReviewConfig.model_validate(FULL)

    assert config.is_reviewer("A.Tyra")
    assert not config.is_reviewer("someone")
    assert not config.is_reviewer(None)


def test_chat_id_round_trip() -> None:
    config = GitLabReviewConfig.model_validate(FULL)

    assert config.iid_from_chat_id(config.review_chat_id(6260)) == 6260
    assert config.iid_from_chat_id("telegram:1") is None


def test_validation_reports_missing_settings() -> None:
    result = validate({"webhookSecretToken": "tok"}, None)  # type: ignore[arg-type]

    assert "gitlabToken" in str(result)
