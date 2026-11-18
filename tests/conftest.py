import sys
import types
from pathlib import Path

# Register the integration as a package without executing its
# __init__.py (which needs a real Home Assistant install).
_pkg_dir = Path(__file__).parent.parent / "custom_components" / "meta_home_link"
_pkg = types.ModuleType("meta_home_link")
_pkg.__path__ = [str(_pkg_dir)]
sys.modules.setdefault("meta_home_link", _pkg)
