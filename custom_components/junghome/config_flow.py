"""Config flow for the Junghome integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN

# Expected token length used for simple validation
TOKEN_EXPECTED_LENGTH = 95
TOKEN_FIELD = "token"

# Example validation schema
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Required(TOKEN_FIELD): str,
    }
)

class JungHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jung Home."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate user input here
            host = user_input["host"]
            token_value = user_input[TOKEN_FIELD]

            if not self._validate_host(host):
                errors["host"] = "invalid_host"

            if not self._validate_token(token_value):
                errors[TOKEN_FIELD] = "invalid_token"

            if not errors:
                # Save configuration if validation passes
                return self.async_create_entry(
                    title="Jung Home",
                    data={"host": host, TOKEN_FIELD: token_value},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    def _validate_host(self, host: str) -> bool:
        """Validate the host (e.g., check format or connectivity)."""
        # Example: Ensure the host is a valid IP address or hostname
        return isinstance(host, str) and len(host) > 0

    def _validate_token(self, token: str) -> bool:
        """Validate the API token."""
        # Example: Check token is a string and matches expected length.
        return isinstance(token, str) and len(token) == TOKEN_EXPECTED_LENGTH
