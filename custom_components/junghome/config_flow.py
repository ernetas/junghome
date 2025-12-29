from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN  # Define DOMAIN in const.py (e.g., DOMAIN = "junghome")
from homeassistant.core import HomeAssistant

# Example validation schema
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Required("token"): str,
    }
)

class JungHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jung Home."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Validate user input here
            host = user_input["host"]
            token = user_input["token"]

            if not self._validate_host(host):
                errors["host"] = "invalid_host"

            if not self._validate_token(token):
                errors["token"] = "invalid_token"

            if not errors:
                # Save configuration if validation passes
                return self.async_create_entry(title="Jung Home", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    def _validate_host(self, host):
        """Validate the host (e.g., check format or connectivity)."""
        # Example: Ensure the host is a valid IP address or hostname
        return isinstance(host, str) and len(host) > 0

    def _validate_token(self, token):
        """Validate the API token."""
        # Example: Ensure the token has a minimum length
        return isinstance(token, str) and len(token) == 95
