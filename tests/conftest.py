import sys
import types
from pathlib import Path

# Register the integration as a package without executing its
# __init__.py (which needs a real Home Assistant install).
_pkg_dir = Path(__file__).parent.parent / "custom_components" / "meta_home_link"
_pkg = types.ModuleType("meta_home_link")
_pkg.__path__ = [str(_pkg_dir)]
sys.modules.setdefault("meta_home_link", _pkg)

# Same trick for the add-on package (muse-gadget/app/muse_gadget).
_addon_dir = Path(__file__).parent.parent / "muse-gadget" / "app" / "muse_gadget"
_addon_pkg = types.ModuleType("muse_gadget")
_addon_pkg.__path__ = [str(_addon_dir)]
_addon_pkg.__version__ = "1.1.0"  # stub does not execute __init__.py
sys.modules.setdefault("muse_gadget", _addon_pkg)
