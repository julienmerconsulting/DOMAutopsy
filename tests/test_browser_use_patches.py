"""Contrats fermes des correctifs runtime Browser Use."""

from types import SimpleNamespace

import pytest

from browser_use_patches import (
    SUPPORTED_BROWSER_USE_VERSION,
    is_strict_transparent_form_control,
    patch_transparent_form_control_visibility,
)


def _node(
    *,
    input_type="checkbox",
    attributes=None,
    width=40,
    height=40,
    display="block",
    visibility="visible",
    opacity="0",
    pointer_events="auto",
    ax_name=None,
):
    attrs = {"type": input_type, "aria-label": "Toggle Todo"}
    if attributes is not None:
        attrs = attributes
    return SimpleNamespace(
        tag_name="input",
        node_name="INPUT",
        attributes=attrs,
        snapshot_node=SimpleNamespace(
            bounds=SimpleNamespace(width=width, height=height),
            computed_styles={
                "display": display,
                "visibility": visibility,
                "opacity": opacity,
                "pointer-events": pointer_events,
            },
        ),
        ax_node=(
            SimpleNamespace(name=ax_name, ignored=False)
            if ax_name is not None
            else None
        ),
    )


def _fake_dom_service():
    class FakeDomService:
        @classmethod
        def is_element_visible_according_to_all_parents(
            cls, node, _html_frames, _viewport_threshold=1000
        ):
            snapshot = node.snapshot_node
            styles = snapshot.computed_styles
            if styles.get("display") == "none":
                return False
            if styles.get("visibility") in {"hidden", "collapse"}:
                return False
            if float(styles.get("opacity", "1")) <= 0:
                return False
            return bool(snapshot.bounds.width > 0 and snapshot.bounds.height > 0)

    return FakeDomService


def test_todomvc_like_checkboxes_enter_selector_map_after_patch():
    service = _fake_dom_service()
    controls = [
        _node(attributes={"type": "checkbox", "aria-label": f"Toggle Todo {index}"})
        for index in range(1, 5)
    ]

    assert not any(
        service.is_element_visible_according_to_all_parents(control, [], None)
        for control in controls
    )
    assert patch_transparent_form_control_visibility(
        service, SUPPORTED_BROWSER_USE_VERSION
    )

    selector_map = {
        index: control
        for index, control in enumerate(controls, start=1)
        if service.is_element_visible_according_to_all_parents(control, [], None)
    }
    assert len(selector_map) == 4
    assert [node.attributes["aria-label"] for node in selector_map.values()] == [
        "Toggle Todo 1",
        "Toggle Todo 2",
        "Toggle Todo 3",
        "Toggle Todo 4",
    ]


def test_patch_neutralizes_only_opacity_and_restores_snapshot_styles():
    service = _fake_dom_service()
    control = _node()
    original_styles = control.snapshot_node.computed_styles
    assert patch_transparent_form_control_visibility(
        service, SUPPORTED_BROWSER_USE_VERSION
    )

    assert service.is_element_visible_according_to_all_parents(control, [], None) is True
    assert control.snapshot_node.computed_styles is original_styles
    assert control.snapshot_node.computed_styles["opacity"] == "0"


def test_ax_name_proves_associated_label_without_aria_attribute():
    control = _node(attributes={"type": "radio"}, ax_name="Choix principal")
    assert is_strict_transparent_form_control(control) is True


@pytest.mark.parametrize(
    "control",
    [
        _node(input_type="text"),
        _node(attributes={"type": "checkbox", "disabled": ""}),
        _node(attributes={"type": "checkbox", "hidden": ""}),
        _node(attributes={"type": "checkbox", "inert": ""}),
        _node(attributes={"type": "checkbox", "aria-disabled": "true"}),
        _node(attributes={"type": "checkbox", "aria-hidden": "true", "aria-label": "Toggle"}),
        _node(attributes={"type": "checkbox", "tabindex": "-1", "aria-label": "Toggle"}),
        _node(width=0),
        _node(height=0),
        _node(display="none"),
        _node(visibility="hidden"),
        _node(pointer_events="none"),
        _node(opacity="1"),
        _node(attributes={"type": "checkbox"}),
        _node(attributes={"type": "checkbox", "aria-labelledby": "missing-label"}),
    ],
    ids=[
        "text-input",
        "disabled",
        "hidden-attribute",
        "inert",
        "aria-disabled",
        "aria-hidden",
        "not-focusable",
        "zero-width",
        "zero-height",
        "display-none",
        "visibility-hidden",
        "pointer-events-none",
        "already-visible",
        "no-accessible-name",
        "unresolved-aria-labelledby",
    ],
)
def test_strict_policy_rejects_non_actionable_or_unlabelled_controls(control):
    assert is_strict_transparent_form_control(control) is False


def test_version_guard_fails_closed_and_leaves_upstream_method_untouched():
    service = _fake_dom_service()
    original_descriptor = service.__dict__["is_element_visible_according_to_all_parents"]

    assert patch_transparent_form_control_visibility(service, "0.13.9") is False
    assert service.__dict__["is_element_visible_according_to_all_parents"] is original_descriptor
    assert service.is_element_visible_according_to_all_parents(_node(), [], None) is False


def test_patch_is_idempotent():
    service = _fake_dom_service()
    assert patch_transparent_form_control_visibility(
        service, SUPPORTED_BROWSER_USE_VERSION
    )
    first_descriptor = service.__dict__["is_element_visible_according_to_all_parents"]

    assert patch_transparent_form_control_visibility(
        service, SUPPORTED_BROWSER_USE_VERSION
    )
    assert service.__dict__["is_element_visible_according_to_all_parents"] is first_descriptor


def test_patch_fails_loudly_if_upstream_method_is_no_longer_a_classmethod():
    class ChangedDomService:
        @staticmethod
        def is_element_visible_according_to_all_parents(*_args):
            return False

    with pytest.raises(TypeError, match="n'est plus un classmethod"):
        patch_transparent_form_control_visibility(
            ChangedDomService, SUPPORTED_BROWSER_USE_VERSION
        )
