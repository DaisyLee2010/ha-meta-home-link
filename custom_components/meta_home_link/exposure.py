"""Exposed-device model: what Muse may see and control.

Pure Python (no Home Assistant imports) so the picker / friendly-name
logic is unit-testable alone. ``hub.py`` feeds it live HA states and
executes the service calls it plans.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import READ_ONLY_DOMAINS, SUPPORTED_DOMAINS


@dataclass(frozen=True)
class ExposedDevice:
    entity_id: str
    friendly_name: str  # name Muse hears; user alias or HA friendly name
    domain: str

    @property
    def read_only(self) -> bool:
        return self.domain in READ_ONLY_DOMAINS


def _default_name(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1].replace("_", " ").strip().title()


def normalise_exposure(
    exposed_entities: list[str] | None,
    friendly_names: dict[str, str] | None,
    ha_names: dict[str, str] | None = None,
) -> list[ExposedDevice]:
    """Build the exposed list from picker + alias config.

    - Entities outside SUPPORTED_DOMAINS are dropped (picker filters too).
    - A blank alias falls back to the HA friendly name, then a
      humanised entity id. Aliases are what Muse matches on.
    - Duplicate friendly names (case-insensitive) get the entity's
      domain appended so Muse can still tell them apart.
    """
    ha_names = ha_names or {}
    aliases = friendly_names or {}
    devices: list[ExposedDevice] = []
    seen: dict[str, int] = {}
    for entity_id in exposed_entities or []:
        if "." not in entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in SUPPORTED_DOMAINS:
            continue
        name = (aliases.get(entity_id) or "").strip()
        if not name:
            name = (ha_names.get(entity_id) or "").strip() or _default_name(entity_id)
        key = name.casefold()
        if key in seen:
            seen[key] += 1
            name = f"{name} ({domain} {seen[key]})"
        else:
            seen[key] = 1
        devices.append(ExposedDevice(entity_id, name, domain))
    return devices


class ExposureRegistry:
    """Resolves the names Muse uses to exposed Home Assistant entities."""

    def __init__(self, devices: list[ExposedDevice]) -> None:
        self.devices = devices
        self._by_name = {d.friendly_name.casefold(): d for d in devices}
        self._by_entity = {d.entity_id: d for d in devices}

    def resolve(self, device: str | None) -> ExposedDevice | None:
        if not device:
            return None
        text = str(device).strip()
        return (
            self._by_entity.get(text)
            or self._by_name.get(text.casefold())
            or next(
                (d for d in self.devices if d.friendly_name.casefold() in text.casefold()),
                None,
            )
        )

    # ------------------------------------------------------------ guide
    def guide(self, instance_name: str, states: dict[str, dict]) -> str:
        lines = [
            f"# {instance_name} (Home Assistant via Meta Home Link)",
            "",
            "You control a real home through Home Assistant. Only the "
            "devices listed below are exposed to you; refer to each by "
            "its friendly name exactly as written. Keep spoken replies "
            "short and confirm what you did (\"Kitchen Light is on\").",
            "",
            "## Exposed devices",
            "",
        ]
        if not self.devices:
            lines.append("No devices are exposed yet. The owner can add "
                         "some in Home Assistant: Settings > Devices & "
                         "services > Meta Home Link > Configure.")
        for d in self.devices:
            state = states.get(d.entity_id, {})
            current = state.get("state", "unknown")
            kind = "read-only" if d.read_only else d.domain
            lines.append(f"- {d.friendly_name} ({kind}, currently {current})")
        lines += [
            "",
            "## How to control them",
            "",
            "- home.list_devices lists every exposed device and its state.",
            "- home.turn_on / home.turn_off / home.toggle take `device` "
            "(a friendly name above).",
            "- Lights also take home.set_brightness (`brightness_pct` 0-100).",
            "- Climate takes home.set_temperature (`temperature`, "
            "optional `hvac_mode`). Covers take home.set_cover_position "
            "(`position` 0-100). Fans take home.set_fan_percentage.",
            "- Locks: home.lock / home.unlock. Media: home.media_control "
            "(`action`: play, pause, stop, next, previous) and "
            "home.set_volume (`volume_pct` 0-100).",
            "- Scenes, scripts, buttons: home.activate / home.press. "
            "Sensors are read-only: use home.get_state.",
            "- Never guess a device that is not listed; ask the owner "
            "to expose it in Home Assistant instead.",
        ]
        return "\n".join(lines)

    def list_payload(self, states: dict[str, dict]) -> list[dict]:
        out = []
        for d in self.devices:
            state = states.get(d.entity_id, {})
            item = {
                "name": d.friendly_name,
                "entity_id": d.entity_id,
                "domain": d.domain,
                "state": state.get("state", "unknown"),
                "read_only": d.read_only,
            }
            if state.get("attributes"):
                item["attributes"] = state["attributes"]
            out.append(item)
        return out


# -------------------------------------------------------- service planning
class PlanError(ValueError):
    """A command Muse asked for cannot be carried out."""


def plan_service(command: str, device: ExposedDevice, params: dict) -> tuple[str, str, dict]:
    """Map a home.* command to a (domain, service, service_data) call.

    Pure function: hub.py executes the plan against Home Assistant.
    Raises PlanError with a Muse-speakable message on bad input.
    """
    domain = device.domain
    eid = device.entity_id
    if device.read_only and command not in ("home.get_state",):
        raise PlanError(f"{device.friendly_name} is read-only; I can only report its state.")
    simple = {
        "home.turn_on": (domain, "turn_on", {}),
        "home.turn_off": (domain, "turn_off", {}),
        "home.toggle": (domain, "toggle", {}),
        "home.lock": ("lock", "lock", {}),
        "home.unlock": ("lock", "unlock", {}),
        "home.press": (domain, "press", {}),
        "home.activate": (domain, "turn_on", {}),
    }
    if command in simple:
        svc_domain, service, data = simple[command]
        if command in ("home.turn_on", "home.turn_off", "home.toggle") and domain not in (
            "light", "switch", "fan", "media_player", "humidifier", "vacuum", "scene", "script",
        ):
            raise PlanError(f"{device.friendly_name} ({domain}) does not support {command[5:]}.")
        if command in ("home.lock", "home.unlock") and domain != "lock":
            raise PlanError(f"{device.friendly_name} is not a lock.")
        if command == "home.activate" and domain not in ("scene", "script"):
            raise PlanError(f"{device.friendly_name} is not a scene or script; use home.turn_on.")
        if command == "home.press" and domain not in ("button", "input_button"):
            raise PlanError(f"{device.friendly_name} is not a button.")
        return svc_domain, service, {"entity_id": eid, **data}
    if command == "home.set_brightness":
        if domain != "light":
            raise PlanError(f"{device.friendly_name} is not a light.")
        pct = _number(params, "brightness_pct")
        return "light", "turn_on", {"entity_id": eid, "brightness_pct": pct}
    if command == "home.set_temperature":
        if domain != "climate":
            raise PlanError(f"{device.friendly_name} is not a climate device.")
        data = {"entity_id": eid, "temperature": _number(params, "temperature")}
        if params.get("hvac_mode"):
            data["hvac_mode"] = str(params["hvac_mode"])
        return "climate", "set_temperature", data
    if command == "home.set_cover_position":
        if domain != "cover":
            raise PlanError(f"{device.friendly_name} is not a cover.")
        return "cover", "set_cover_position", {"entity_id": eid, "position": int(_number(params, "position"))}
    if command == "home.set_fan_percentage":
        if domain != "fan":
            raise PlanError(f"{device.friendly_name} is not a fan.")
        return "fan", "set_percentage", {"entity_id": eid, "percentage": int(_number(params, "percentage"))}
    if command == "home.set_volume":
        if domain != "media_player":
            raise PlanError(f"{device.friendly_name} is not a media player.")
        return "media_player", "volume_set", {
            "entity_id": eid, "volume_level": _number(params, "volume_pct") / 100.0}
    if command == "home.media_control":
        if domain != "media_player":
            raise PlanError(f"{device.friendly_name} is not a media player.")
        actions = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
                   "next": "media_next_track", "previous": "media_previous_track"}
        action = str(params.get("action", "")).strip().lower()
        if action not in actions:
            raise PlanError("Say play, pause, stop, next or previous for media control.")
        return "media_player", actions[action], {"entity_id": eid}
    raise PlanError(f"Unknown command: {command}")


def _number(params: dict, key: str) -> float:
    try:
        return float(params[key])
    except (KeyError, TypeError, ValueError) as err:
        raise PlanError(f"Missing or invalid {key}.") from err


# ---------------------------------------------------------- command specs
def _spec(description: str, required: dict | None = None, optional: dict | None = None) -> dict:
    return {
        "description": description,
        "required": required or {},
        "optional": optional or {},
    }


def _param(type_: str, description: str) -> dict:
    return {"type": type_, "description": description}


_DEVICE = _param("string", "Friendly name of an exposed device, as listed by home.list_devices.")


def command_specs() -> dict[str, dict]:
    """The ``commands_v2`` map registered with Muse at link.register."""
    return {
        "device.guide": _spec("This home's usage guide (Markdown): exposed devices, controls and limits."),
        "device.health": _spec("Report Home Assistant version, link uptime and exposed-device count."),
        "home.list_devices": _spec("List every device exposed to Muse, with its current state."),
        "home.get_state": _spec("Get one exposed device's current state.", {"device": _DEVICE}),
        "home.turn_on": _spec("Turn on an exposed light, switch, fan, media player or humidifier.", {"device": _DEVICE}),
        "home.turn_off": _spec("Turn off an exposed light, switch, fan, media player or humidifier.", {"device": _DEVICE}),
        "home.toggle": _spec("Toggle an exposed light, switch or fan.", {"device": _DEVICE}),
        "home.set_brightness": _spec(
            "Set a light's brightness.", {"device": _DEVICE, "brightness_pct": _param("number", "Brightness 0-100.")}),
        "home.set_temperature": _spec(
            "Set a climate device's target temperature.",
            {"device": _DEVICE, "temperature": _param("number", "Target temperature in the device's units.")},
            {"hvac_mode": _param("string", "Optional HVAC mode, e.g. heat, cool, auto, off.")}),
        "home.set_cover_position": _spec(
            "Set a cover's position.", {"device": _DEVICE, "position": _param("number", "Position 0 (closed) to 100 (open).")}),
        "home.set_fan_percentage": _spec(
            "Set a fan's speed.", {"device": _DEVICE, "percentage": _param("number", "Speed 0-100.")}),
        "home.set_volume": _spec(
            "Set a media player's volume.", {"device": _DEVICE, "volume_pct": _param("number", "Volume 0-100.")}),
        "home.media_control": _spec(
            "Control a media player.",
            {"device": _DEVICE, "action": _param("string", "One of: play, pause, stop, next, previous.")}),
        "home.lock": _spec("Lock an exposed lock.", {"device": _DEVICE}),
        "home.unlock": _spec("Unlock an exposed lock. Confirm with the owner first if unsure.", {"device": _DEVICE}),
        "home.press": _spec("Press an exposed button.", {"device": _DEVICE}),
        "home.activate": _spec("Activate an exposed scene or run an exposed script.", {"device": _DEVICE}),
    }
