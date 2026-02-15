"""GitHub Copilot token manager with proactive refresh (like OpenClaw).

Refreshes token 5 minutes before expiry, not after.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from litellm.llms.github_copilot.authenticator import Authenticator

# Refresh token 5 minutes before expiry (like OpenClaw)
REFRESH_THRESHOLD_SECONDS = 300


class CopilotTokenManager:
    """Manages GitHub Copilot tokens with proactive refresh."""
    
    def __init__(self):
        self._auth = Authenticator()
    
    @property
    def token_dir(self) -> str:
        return self._auth.token_dir
    
    @property
    def api_key_file(self) -> str:
        return self._auth.api_key_file
    
    def is_token_usable(self) -> bool:
        """Check if token is valid with 5-minute buffer (like OpenClaw)."""
        try:
            with open(self.api_key_file) as f:
                data = json.load(f)
            expires_at = data.get("expires_at", 0)
            now = datetime.now().timestamp()
            # Token is usable if expires in more than 5 minutes
            return (expires_at - now) > REFRESH_THRESHOLD_SECONDS
        except Exception:
            return False
    
    def get_token_info(self) -> dict | None:
        """Get current token info."""
        try:
            with open(self.api_key_file) as f:
                return json.load(f)
        except Exception:
            return None
    
    def ensure_valid_token(self) -> str:
        """Ensure we have a valid token, refreshing proactively if needed.
        
        Returns the token string.
        Raises exception if unable to get valid token.
        """
        if self.is_token_usable():
            info = self.get_token_info()
            if info:
                return info.get("token", "")
        
        # Token expired or expiring soon — refresh
        return self._auth.get_api_key()
    
    def needs_refresh(self) -> tuple[bool, int]:
        """Check if token needs refresh.
        
        Returns (needs_refresh, seconds_until_expiry).
        """
        info = self.get_token_info()
        if not info:
            return True, 0
        
        expires_at = info.get("expires_at", 0)
        now = datetime.now().timestamp()
        seconds_left = int(expires_at - now)
        
        return seconds_left <= REFRESH_THRESHOLD_SECONDS, seconds_left
    
    def refresh(self) -> str:
        """Force refresh the token.
        
        Returns the new token.
        """
        return self._auth.get_api_key()


# Singleton instance
_manager: CopilotTokenManager | None = None


def get_token_manager() -> CopilotTokenManager:
    """Get the singleton token manager."""
    global _manager
    if _manager is None:
        _manager = CopilotTokenManager()
    return _manager


def ensure_copilot_token() -> str:
    """Ensure valid GitHub Copilot token (convenience function)."""
    return get_token_manager().ensure_valid_token()
