"""Constants for the Meta Home Link (Muse gadget) integration."""

DOMAIN = "meta_home_link"
VERSION = "1.0.0"

CONF_NAME = "name"
CONF_SDK_TOKEN = "sdk_token"
CONF_PAIRING = "pairing"
CONF_NODE_ID = "node_id"
CONF_DEVICE_ID = "device_id"
CONF_MAC = "mac"
CONF_EXPOSED_ENTITIES = "exposed_entities"
CONF_FRIENDLY_NAMES = "friendly_names"

DEFAULT_NAME = "Home Assistant"
DEFAULT_NOISE_HOST = "hatch.metaaivm.com"
DEFAULT_API_ROOT = "https://api.muse.ai"

# Domains the user may expose to Muse, grouped by how Muse can use them.
CONTROL_DOMAINS = (
    "light",
    "switch",
    "fan",
    "cover",
    "climate",
    "lock",
    "media_player",
    "vacuum",
    "humidifier",
    "button",
    "input_button",
    "scene",
    "script",
)
READ_ONLY_DOMAINS = ("sensor", "binary_sensor")
SUPPORTED_DOMAINS = CONTROL_DOMAINS + READ_ONLY_DOMAINS

PLATFORMS = ["sensor", "binary_sensor"]

DATA_HUB = "hub"
