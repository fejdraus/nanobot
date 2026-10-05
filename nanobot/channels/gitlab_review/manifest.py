"""Dependency-free management contract for the GitLab review channel."""

from nanobot.channels._manifest import field, required_fields
from nanobot.channels.contracts import ChannelSetupSpec
from nanobot.channels.gitlab_review.validation import validate
from nanobot.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "webhookSecretToken": field("secret"),
        "host": field(default="127.0.0.1"),
        "port": field("int", default=3980),
        "webhookPath": field(default="/gitlab/webhook"),
        "projectPath": field(default="astana-group/astana-motors"),
        "chatId": field(default="gitlab-review"),
        "gitlabUrl": field(),
        "gitlabToken": field("secret"),
        "reviewerUsernames": field("list"),
        "reviewOwnMergeRequests": field("bool", default=False),
        "lessonsDir": field(),
        "lessonsBudgetChars": field("int", default=40000),
        "telegramBotToken": field("secret"),
        "telegramChatId": field(),
        "telegramUserIds": field("list"),
        "debounceSeconds": field("float", default=90.0),
        "maxRunsPerMrPerHour": field("int", default=6),
        "reviewTimeoutS": field("float", default=3900.0),
    },
    required=required_fields(
        "webhookSecretToken",
        "gitlabUrl",
        "gitlabToken",
        "reviewerUsernames",
        "telegramBotToken",
        "telegramChatId",
    ),
    validator=validate,
)

PLUGIN = ChannelPlugin(
    name="gitlab_review",
    display_name="GitLab Review",
    runtime=f"{__package__}.runtime:GitLabReviewChannel",
    setup=SETUP_SPEC,
)
