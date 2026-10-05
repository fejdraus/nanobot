"""Setup validation for the GitLab review channel."""

from typing import Any

from pydantic import ValidationError

from nanobot.channels.contracts import ChannelValidationContext
from nanobot.channels.gitlab_review.config import GitLabReviewConfig
from nanobot.channels.validation import check, required_checks, status_from_checks


def validate(values: dict[str, Any], _context: ChannelValidationContext) -> dict[str, Any]:
    checks, missing = required_checks("gitlab_review", values)
    try:
        config = GitLabReviewConfig.model_validate(values)
        config.validate_runtime()
    except (ValidationError, ValueError) as exc:
        checks.append(check("config", "Review settings", "fail", str(exc)))
    else:
        checks.append(
            check(
                "config",
                "Review settings",
                "pass",
                f"Listening on {config.host}:{config.port}{config.webhook_path} "
                f"for {config.project_path}; drafts go to Telegram chat "
                f"{config.telegram_chat_id}; reviewer: {', '.join(config.reviewer_usernames)}",
            )
        )
    checks.append(
        check(
            "gitlab_project",
            "GitLab webhook settings",
            "skipped",
            "Enable Merge request events and Comments on the project webhook and set "
            "webhookSecretToken as its Secret token. The token is verified on the first "
            "delivered webhook.",
        )
    )
    return status_from_checks("gitlab_review", checks, missing)


__all__ = ["validate"]
