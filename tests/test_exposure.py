import pytest

from meta_home_link.exposure import (
    ExposureRegistry,
    PlanError,
    normalise_exposure,
    plan_service,
)

HA_NAMES = {
    "light.kitchen": "Kitchen ceiling",
    "light.bedroom": "Bedroom lamp",
    "sensor.temp": "Hall temperature",
}


def registry():
    devices = normalise_exposure(
        ["light.kitchen", "light.bedroom", "sensor.temp", "camera.porch", "bogus"],
        {"light.kitchen": "Kitchen Light"},
        HA_NAMES,
    )
    return ExposureRegistry(devices)


def test_picker_drops_unsupported_and_applies_alias():
    reg = registry()
    assert [d.entity_id for d in reg.devices] == [
        "light.kitchen", "light.bedroom", "sensor.temp"]
    assert reg.devices[0].friendly_name == "Kitchen Light"  # user alias wins
    assert reg.devices[1].friendly_name == "Bedroom lamp"  # HA name fallback
    assert reg.devices[2].read_only is True


def test_blank_alias_falls_back_to_humanised_id():
    devices = normalise_exposure(["switch.water_heater"], {"switch.water_heater": "  "}, {})
    assert devices[0].friendly_name == "Water Heater"


def test_duplicate_names_are_disambiguated():
    devices = normalise_exposure(
        ["light.a", "switch.a"], {}, {"light.a": "Lamp", "switch.a": "Lamp"})
    assert devices[0].friendly_name == "Lamp"
    assert devices[1].friendly_name == "Lamp (switch 2)"


def test_resolve_by_alias_entity_and_case():
    reg = registry()
    assert reg.resolve("kitchen light").entity_id == "light.kitchen"
    assert reg.resolve("light.bedroom").entity_id == "light.bedroom"
    assert reg.resolve("Bedroom Lamp").entity_id == "light.bedroom"
    assert reg.resolve("garage door") is None
    assert reg.resolve("") is None


def test_guide_lists_friendly_names_and_states():
    reg = registry()
    guide = reg.guide("My Home", {"light.kitchen": {"state": "on", "attributes": {}}})
    assert "Kitchen Light (light, currently on)" in guide
    assert "Hall temperature" in guide
    assert "home.turn_on" in guide


def test_list_payload():
    reg = registry()
    payload = reg.list_payload({"sensor.temp": {"state": "21.5", "attributes": {"unit_of_measurement": "°C"}}})
    assert payload[2]["state"] == "21.5"
    assert payload[0]["state"] == "unknown"


# ------------------------------------------------------------- planning
def dev(entity_id, name="Device"):
    domain = entity_id.split(".")[0]
    from meta_home_link.exposure import ExposedDevice
    return ExposedDevice(entity_id, name, domain)


def test_plan_light_and_brightness():
    assert plan_service("home.turn_on", dev("light.k"), {}) == (
        "light", "turn_on", {"entity_id": "light.k"})
    assert plan_service("home.set_brightness", dev("light.k"), {"brightness_pct": 40}) == (
        "light", "turn_on", {"entity_id": "light.k", "brightness_pct": 40.0})
    with pytest.raises(PlanError):
        plan_service("home.set_brightness", dev("switch.k"), {"brightness_pct": 40})


def test_plan_climate_cover_fan_volume_media():
    assert plan_service("home.set_temperature", dev("climate.h"), {"temperature": 21, "hvac_mode": "heat"})[2] == {
        "entity_id": "climate.h", "temperature": 21.0, "hvac_mode": "heat"}
    assert plan_service("home.set_cover_position", dev("cover.b"), {"position": 75})[1:] == (
        "set_cover_position", {"entity_id": "cover.b", "position": 75})
    assert plan_service("home.set_volume", dev("media_player.tv"), {"volume_pct": 30})[2]["volume_level"] == 0.3
    assert plan_service("home.media_control", dev("media_player.tv"), {"action": "next"})[1] == "media_next_track"
    with pytest.raises(PlanError):
        plan_service("home.media_control", dev("media_player.tv"), {"action": "explode"})


def test_plan_lock_scene_button_and_read_only():
    assert plan_service("home.lock", dev("lock.front"), {})[:2] == ("lock", "lock")
    assert plan_service("home.activate", dev("scene.movie"), {})[:2] == ("scene", "turn_on")
    assert plan_service("home.press", dev("button.doorbell"), {})[:2] == ("button", "press")
    with pytest.raises(PlanError):
        plan_service("home.turn_on", dev("sensor.temp"), {})
    with pytest.raises(PlanError):
        plan_service("home.unlock", dev("light.k"), {})


def test_command_specs_cover_guide_and_home():
    from meta_home_link.exposure import command_specs
    specs = command_specs()
    assert "device.guide" in specs and "home.list_devices" in specs
    assert specs["home.set_brightness"]["required"]["device"]["type"] == "string"
