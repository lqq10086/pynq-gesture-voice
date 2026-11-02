"""Deterministic tile shapes and cached 2.5D scene creation."""
from pathlib import Path


def create_app(width, height, root=None, font_path=None, work_path=None):
    """Create the original drawing app with packaged assets and isolated storage.

    ``root`` selects storage only; assets always come from this package. A
    supplied work path keeps its parent and uses ``living_work.json`` so the
    space-building app cannot share the drawing document by accident.
    """
    from .updated_town import UpdatedTownApp

    package = Path(__file__).resolve().parent
    storage = Path(root) if root is not None else package.parent
    work = (Path(work_path).with_name('living_work.json') if work_path is not None
            else storage / 'works' / 'living_work.json')
    font = Path(font_path) if font_path is not None else package / 'assets' / 'ui_font.ttf'
    return UpdatedTownApp(width, height, root=package, font_path=font, work_path=work)
