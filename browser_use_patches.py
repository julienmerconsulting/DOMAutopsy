"""Correctifs runtime etroitement bornes pour Browser Use.

Ces correctifs ne changent jamais les fichiers de ``site-packages``. Ils
sont gardes par version et echouent fermes si l'API privee visee change.
"""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any


SUPPORTED_BROWSER_USE_VERSION = "0.13.8"
_TRANSPARENT_CONTROLS_PATCH_MARKER = "_domautopsy_transparent_controls_patch"


def is_strict_transparent_form_control(node: Any) -> bool:
    """Vrai pour un checkbox/radio transparent mais actionnable.

    Browser Use 0.13.8 classe ``opacity:0`` comme invisible avant la
    serialisation. Certains composants accessibles (TodoMVC notamment)
    conservent pourtant un input natif dimensionne, focusable et nomme sous
    leur rendu visuel. La regle reste volontairement fermee pour ne pas faire
    remonter les honeypots et autres champs caches.
    """

    tag_name = str(getattr(node, "tag_name", None) or getattr(node, "node_name", "")).lower()
    attributes = getattr(node, "attributes", None)
    if tag_name != "input" or not isinstance(attributes, dict):
        return False

    input_type = str(attributes.get("type") or "").lower()
    if input_type not in {"checkbox", "radio"}:
        return False
    if "disabled" in attributes or "hidden" in attributes or "inert" in attributes:
        return False
    if str(attributes.get("aria-disabled") or "").lower() == "true":
        return False
    if str(attributes.get("aria-hidden") or "").lower() == "true":
        return False
    if str(attributes.get("tabindex") or "").strip() == "-1":
        return False

    snapshot = getattr(node, "snapshot_node", None)
    bounds = getattr(snapshot, "bounds", None) if snapshot is not None else None
    if bounds is None:
        return False
    if float(getattr(bounds, "width", 0) or 0) <= 0 or float(getattr(bounds, "height", 0) or 0) <= 0:
        return False

    styles = getattr(snapshot, "computed_styles", None)
    if not isinstance(styles, dict):
        return False
    if str(styles.get("display") or "").lower() == "none":
        return False
    if str(styles.get("visibility") or "").lower() in {"hidden", "collapse"}:
        return False
    if str(styles.get("pointer-events") or "").lower() == "none":
        return False
    try:
        if float(styles.get("opacity", "1")) > 0:
            return False
    except (TypeError, ValueError):
        return False

    # Un aria-label non vide est une preuve directe. Pour aria-labelledby,
    # <label for=...> et les labels englobants, seul le nom AX resolu par
    # Chromium constitue une preuve : la simple presence d'un IDREF peut
    # pointer vers un element absent et ne doit pas ouvrir la politique.
    aria_label = attributes.get("aria-label")
    has_explicit_name = isinstance(aria_label, str) and bool(aria_label.strip())
    ax_node = getattr(node, "ax_node", None)
    has_ax_name = bool(
        ax_node
        and not bool(getattr(ax_node, "ignored", False))
        and isinstance(getattr(ax_node, "name", None), str)
        and ax_node.name.strip()
    )
    return has_explicit_name or has_ax_name


def patch_transparent_form_control_visibility(dom_service_cls: type, installed_version: str) -> bool:
    """Patche la visibilite BU 0.13.8 sans recopier son serializer.

    Pour une cible strictement admissible, seule l'opacite vue par la methode
    upstream est neutralisee le temps de son appel synchrone. Tous ses autres
    controles (display, visibility, bounds, viewport et frames) restent donc
    la source de verite.

    Retourne ``False`` sur une autre version. Une forme inattendue de l'API
    leve une erreur afin que l'appelant puisse signaler que le patch n'est pas
    applique plutot que de continuer silencieusement.
    """

    if installed_version != SUPPORTED_BROWSER_USE_VERSION:
        return False
    if getattr(dom_service_cls, _TRANSPARENT_CONTROLS_PATCH_MARKER, None) == installed_version:
        return True

    method_name = "is_element_visible_according_to_all_parents"
    descriptor = inspect.getattr_static(dom_service_cls, method_name)
    if not isinstance(descriptor, classmethod):
        raise TypeError(f"{dom_service_cls.__name__}.{method_name} n'est plus un classmethod")
    original = descriptor.__func__

    @wraps(original)
    def _patched(cls, node, html_frames, viewport_threshold=1000):
        if not is_strict_transparent_form_control(node):
            return original(cls, node, html_frames, viewport_threshold)

        snapshot = node.snapshot_node
        original_styles = snapshot.computed_styles
        visible_styles = dict(original_styles)
        visible_styles["opacity"] = "1"
        snapshot.computed_styles = visible_styles
        try:
            return original(cls, node, html_frames, viewport_threshold)
        finally:
            snapshot.computed_styles = original_styles

    setattr(dom_service_cls, method_name, classmethod(_patched))
    setattr(dom_service_cls, _TRANSPARENT_CONTROLS_PATCH_MARKER, installed_version)
    return True
