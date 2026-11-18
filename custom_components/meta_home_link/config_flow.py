"""Config flow: SDK token + pairing credentials, then the device picker.

Step order matters to the user, so it mirrors the task:
1. ``user`` - name this link + Gadget SDK token (mgst_...).
2. ``credentials`` - paste the device pairing credentials the Muse
   app issued during BLE pairing (see README for how to get them).
3. ``devices`` - pick exactly which Home Assistant entities Muse
   may see. Nothing is exposed until it is picked here.
4. ``friendly_names`` - one optional text field per picked entity;
   the name typed here is the name Muse hears and matches on.

The options flow repeats steps 3-4, so exposure and friendly names
can be changed any time without re-pairing.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .config_helpers import merge_friendly_names, parse_pairing_credentials, valid_sdk_token
from .const import (
    CONF_EXPOSED_ENTITIES,
    CONF_FRIENDLY_NAMES,
    CONF_MAC,
    CONF_NAME,
    CONF_PAIRING,
    CONF_SDK_TOKEN,
    DEFAULT_NAME,
    DOMAIN,
    SUPPORTED_DOMAINS,
)
from .muse_link import generate_identity


def _entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=list(SUPPORTED_DOMAINS), multiple=True)
    )


def _friendly_names_schema(flow, exposed: list[str]) -> vol.Schema:
    fields = {}
    for entity_id in exposed:
        state = flow.hass.states.get(entity_id)
        ha_name = state.attributes.get("friendly_name", "") if state else ""
        current = (flow._friendly_names or {}).get(entity_id, ha_name)
        fields[vol.Optional(entity_id, default=current)] = selector.TextSelector(
            selector.TextSelectorConfig()
        )
    return vol.Schema(fields)


def _ha_names(flow, exposed: list[str]) -> dict[str, str]:
    out = {}
    for entity_id in exposed:
        state = flow.hass.states.get(entity_id)
        if state is not None:
            out[entity_id] = state.attributes.get("friendly_name", "")
    return out


class MetaHomeLinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name = DEFAULT_NAME
        self._sdk_token = ""
        self._pairing: dict | None = None
        self._exposed: list[str] = []
        self._friendly_names: dict[str, str] = {}

    # ------------------------------------------------------------- steps
    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = (user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            self._sdk_token = (user_input.get(CONF_SDK_TOKEN) or "").strip()
            if self._sdk_token and not valid_sdk_token(self._sdk_token):
                errors[CONF_SDK_TOKEN] = "sdk_token_invalid"
            if not errors:
                # No unique id: each entry creates its own gadget
                # identity at create time, so multiple links can coexist.
                return await self.async_step_credentials()
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=self._name): selector.TextSelector(),
            vol.Optional(CONF_SDK_TOKEN, default=self._sdk_token): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_credentials(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pairing, error = parse_pairing_credentials(user_input.get(CONF_PAIRING))
            if error is not None:
                errors[CONF_PAIRING] = error
            else:
                self._pairing = pairing
                return await self.async_step_devices()
        schema = vol.Schema({
            vol.Required(CONF_PAIRING): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        })
        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)

    async def async_step_devices(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._exposed = list(user_input.get(CONF_EXPOSED_ENTITIES) or [])
            return await self.async_step_friendly_names()
        schema = vol.Schema({
            vol.Optional(CONF_EXPOSED_ENTITIES, default=self._exposed): _entity_selector(),
        })
        return self.async_show_form(step_id="devices", data_schema=schema)

    async def async_step_friendly_names(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._friendly_names = merge_friendly_names(
                self._exposed, user_input, _ha_names(self, self._exposed)
            )
            return self._create_entry()
        if not self._exposed:
            return self._create_entry()
        return self.async_show_form(
            step_id="friendly_names", data_schema=_friendly_names_schema(self, self._exposed)
        )

    def _create_entry(self) -> FlowResult:
        identity = generate_identity()
        data = {
            CONF_NAME: self._name,
            CONF_SDK_TOKEN: self._sdk_token,
            CONF_PAIRING: self._pairing or {},
            CONF_MAC: identity.mac,
            CONF_EXPOSED_ENTITIES: self._exposed,
            CONF_FRIENDLY_NAMES: self._friendly_names,
        }
        return self.async_create_entry(title=self._name, data=data)

    # ----------------------------------------------------------- options
    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return MetaHomeLinkOptionsFlow(entry)


class MetaHomeLinkOptionsFlow(config_entries.OptionsFlow):
    """Re-pick exposed devices and friendly names without re-pairing."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        merged = {**entry.data, **entry.options}
        self._exposed: list[str] = list(merged.get(CONF_EXPOSED_ENTITIES) or [])
        self._friendly_names: dict[str, str] = dict(merged.get(CONF_FRIENDLY_NAMES) or {})

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._exposed = list(user_input.get(CONF_EXPOSED_ENTITIES) or [])
            return await self.async_step_friendly_names()
        schema = vol.Schema({
            vol.Optional(CONF_EXPOSED_ENTITIES, default=self._exposed): _entity_selector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_friendly_names(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            friendly = merge_friendly_names(self._exposed, user_input, _ha_names(self, self._exposed))
            return self.async_create_entry(data={
                CONF_EXPOSED_ENTITIES: self._exposed,
                CONF_FRIENDLY_NAMES: friendly,
            })
        if not self._exposed:
            return self.async_create_entry(data={
                CONF_EXPOSED_ENTITIES: [], CONF_FRIENDLY_NAMES: {},
            })
        return self.async_show_form(
            step_id="friendly_names", data_schema=_friendly_names_schema(self, self._exposed)
        )
