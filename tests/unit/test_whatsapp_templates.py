"""Unit tests for ``services/whatsapp_templates.py`` and the shipped descriptors.

Two halves, and the second is the one that would otherwise go untested anywhere.

The **renderer** is exercised against descriptors written in a temporary
directory: discovery, version pinning, one-parameter-per-line, blank-line
dropping, and the two failure modes that are deployment faults rather than
delivery ones.

The **shipped descriptors** in ``apps/api/whatsapp/`` are then rendered with a
real context, and what those tests assert is the contract with the templates
approved in the WhatsApp Business account: how many parameters each one produces,
in what order, and — for ``security`` — that it produces **no link**. Nothing else
in this repository can check that contract, because the other half of it lives in
a console.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.events import DomainEventType
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_FRENCH
from core.notifications import EVENT_RULES, RULE_CASE_ASSIGNED
from core.whatsapp import (
    TEMPLATE_NOTIFICATION,
    TEMPLATE_SECURITY,
    build_whatsapp_context,
)
from services.whatsapp_templates import (
    DEFAULT_WHATSAPP_TEMPLATE_ROOT,
    MAX_PARAMETERS,
    JinjaWhatsAppTemplateRenderer,
    WhatsAppTemplateNotFoundError,
    WhatsAppTemplateRenderError,
    available_whatsapp_template_renderers,
    get_whatsapp_template_renderer,
)


def _write(root: Path, name: str, version: int, body: str) -> None:
    (root / f"{name}.v{version}.params.j2").write_text(body, encoding="utf-8")


@pytest.fixture
def renderer(tmp_path: Path) -> JinjaWhatsAppTemplateRenderer:
    return JinjaWhatsAppTemplateRenderer(root=tmp_path)


@pytest.fixture
def shipped() -> JinjaWhatsAppTemplateRenderer:
    """The descriptors that actually ship, read from ``apps/api/whatsapp``."""
    return JinjaWhatsAppTemplateRenderer(root=DEFAULT_WHATSAPP_TEMPLATE_ROOT)


def _context(*, language: str = LANGUAGE_FRENCH, base_url: str | None = None) -> dict[str, Any]:
    return build_whatsapp_context(
        rule_key=RULE_CASE_ASSIGNED.key,
        category="case",
        priority="high",
        context={"case_number": "CASE-2026-0001"},
        recipient_name="Amina",
        language=language,
        base_url=base_url,
        target_type="case",
        target_id=None,
        platform_name="Legal Case Management Platform",
    ).as_mapping()


# --------------------------------------------------------------------------- #
# The renderer
# --------------------------------------------------------------------------- #


class TestRendering:
    def test_each_line_becomes_one_parameter(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """Which is the whole file format: a descriptor says, in order, which
        values fill the approved template's slots."""
        _write(tmp_path, "sample", 1, "{{ title }}\n{{ message }}\n")
        rendered = renderer.render("sample", context=_context())

        assert rendered.parameters == ("Dossier attribué", _context()["message"])

    def test_blank_lines_are_dropped(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """So a descriptor can use control flow without its whitespace becoming an
        empty slot — and an empty parameter is worth avoiding specifically,
        because the Cloud API accepts it and the reader sees a sentence with a hole
        in it."""
        _write(tmp_path, "sample", 1, "{{ title }}\n\n\n{{ message }}\n\n")
        assert len(renderer.render("sample", context=_context()).parameters) == 2

    def test_whitespace_inside_a_parameter_is_collapsed(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """A real Cloud API constraint rather than a defensive habit."""
        _write(tmp_path, "sample", 1, "a     b\n")
        assert renderer.render("sample", context=_context()).parameters == ("a b",)

    def test_control_flow_selects_a_parameter(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "sample",
            1,
            "{% if action_url %}\n{{ action_url }}\n{% else %}\nno link\n{% endif %}\n",
        )
        assert renderer.render("sample", context=_context()).parameters == ("no link",)
        assert renderer.render(
            "sample", context=_context(base_url="https://legal.example")
        ).parameters == ("https://legal.example/notifications",)

    def test_the_highest_version_is_taken_by_default(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        _write(tmp_path, "sample", 1, "one\n")
        _write(tmp_path, "sample", 2, "two\n")

        assert renderer.versions("sample") == [1, 2]
        assert renderer.render("sample", context=_context()).version == 2

    def test_a_version_can_be_pinned(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """Pinned per rule rather than globally, so a new version of the security
        descriptor can ship without re-rendering every case assignment."""
        _write(tmp_path, "sample", 1, "one\n")
        _write(tmp_path, "sample", 2, "two\n")

        assert renderer.render("sample", version=1, context=_context()).parameters == (
            "one",
        )

    def test_the_descriptor_identity_travels_with_the_parameters(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """Configuration is *current* and a delivery is *historical*."""
        _write(tmp_path, "sample", 3, "one\n")
        rendered = renderer.render("sample", context=_context())
        assert (rendered.name, rendered.version) == ("sample", 3)


class TestFailures:
    def test_a_missing_descriptor_is_reported(
        self, renderer: JinjaWhatsAppTemplateRenderer
    ) -> None:
        with pytest.raises(WhatsAppTemplateNotFoundError):
            renderer.render("nothing-here", context=_context())

    def test_a_missing_version_is_reported(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        _write(tmp_path, "sample", 1, "one\n")
        with pytest.raises(WhatsAppTemplateNotFoundError):
            renderer.render("sample", version=9, context=_context())

    def test_an_undefined_variable_fails_loudly(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """`StrictUndefined`: a descriptor that silently lost a variable would send
        a parameter list of the wrong length, which the Cloud API answers with
        `132000` one message at a time."""
        _write(tmp_path, "sample", 1, "{{ title }}\n{{ nonexistent }}\n")
        with pytest.raises(WhatsAppTemplateRenderError):
            renderer.render("sample", context=_context())

    def test_a_descriptor_that_renders_to_nothing_is_refused(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """Caught here so it is recorded as the deployment fault it is, rather
        than as a per-message provider refusal."""
        _write(tmp_path, "sample", 1, "\n\n   \n")
        with pytest.raises(WhatsAppTemplateRenderError):
            renderer.render("sample", context=_context())

    def test_too_many_parameters_is_refused(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """A bound only ever reached by a bug — a runaway loop rather than a
        design."""
        _write(tmp_path, "sample", 1, "x\n" * (MAX_PARAMETERS + 1))
        with pytest.raises(WhatsAppTemplateRenderError):
            renderer.render("sample", context=_context())

    def test_the_libraries_message_does_not_reach_the_error(
        self, renderer: JinjaWhatsAppTemplateRenderer, tmp_path: Path
    ) -> None:
        """Jinja's own exception text quotes the values it was substituting, which
        here include a case number and a recipient's name."""
        _write(tmp_path, "sample", 1, "{{ nonexistent }}\n")
        with pytest.raises(WhatsAppTemplateRenderError) as raised:
            renderer.render("sample", context=_context())

        assert "CASE-2026-0001" not in str(raised.value)
        assert "Amina" not in str(raised.value)


class TestAvailability:
    def test_a_missing_directory_reports_unavailable(self, tmp_path: Path) -> None:
        """So a deployment missing its descriptors says so on the monitoring
        endpoint instead of surfacing as a failed delivery per notification."""
        assert not JinjaWhatsAppTemplateRenderer(root=tmp_path / "absent").is_available()

    def test_the_shipped_directory_is_available(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        assert shipped.is_available()

    def test_resolution_falls_back_rather_than_failing(self) -> None:
        assert available_whatsapp_template_renderers() == ["jinja-files"]
        assert isinstance(
            get_whatsapp_template_renderer("invented"), JinjaWhatsAppTemplateRenderer
        )


# --------------------------------------------------------------------------- #
# The descriptors that actually ship
# --------------------------------------------------------------------------- #


class TestShippedDescriptors:
    """The contract with the templates approved in the WhatsApp Business account.

    A descriptor's parameter **count and order** are that contract. Nothing else
    in this repository can check the other half of it — that half is a console —
    so these tests are what stands between a descriptor edit and error `132000`
    on every message.
    """

    def test_both_templates_exist(self, shipped: JinjaWhatsAppTemplateRenderer) -> None:
        assert shipped.versions(TEMPLATE_NOTIFICATION) == [1]
        assert shipped.versions(TEMPLATE_SECURITY) == [1]

    def test_the_notification_template_has_four_slots(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        """greeting, title, message, and where to read it — the body documented at
        the top of ``notification.v1.params.j2``."""
        rendered = shipped.render(
            TEMPLATE_NOTIFICATION, context=_context(base_url="https://legal.example")
        )
        assert len(rendered.parameters) == 4
        assert rendered.parameters[0].startswith("Bonjour")
        assert rendered.parameters[1] == "Dossier attribué"
        assert "CASE-2026-0001" in rendered.parameters[2]
        assert "https://legal.example" in rendered.parameters[3]

    def test_it_keeps_four_slots_without_a_link(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        """The count must not change with the deployment's configuration: a
        deployment with no base URL would otherwise send three parameters into a
        template approved for four."""
        rendered = shipped.render(TEMPLATE_NOTIFICATION, context=_context())
        assert len(rendered.parameters) == 4
        assert "http" not in rendered.parameters[3]

    def test_the_security_template_offers_no_link(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        """A link in a WhatsApp message about a password is precisely the shape of
        a phishing message, and the sender is a number the reader may not
        recognise."""
        rendered = shipped.render(
            TEMPLATE_SECURITY,
            context=build_whatsapp_context(
                rule_key=EVENT_RULES[DomainEventType.USER_PASSWORD_RESET].key,
                category="user",
                priority="critical",
                context=None,
                recipient_name="Amina",
                language=LANGUAGE_FRENCH,
                base_url="https://legal.example",
                target_type="account",
                target_id=None,
                platform_name="Legal",
            ).as_mapping(),
        )
        assert len(rendered.parameters) == 4
        assert not any("http" in parameter for parameter in rendered.parameters)
        assert "administrateur" in rendered.parameters[3]

    def test_they_render_in_arabic(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        """The wording is the notification's own, so an Arabic reader's message is
        Arabic for the same reason their feed is."""
        rendered = shipped.render(
            TEMPLATE_NOTIFICATION, context=_context(language=LANGUAGE_ARABIC)
        )
        assert rendered.parameters[0].startswith("مرحبًا")
        assert rendered.parameters[1] == "تم إسناد ملف إليك"

    def test_no_parameter_contains_a_line_break(
        self, shipped: JinjaWhatsAppTemplateRenderer
    ) -> None:
        """Which the Cloud API refuses outright."""
        for name in (TEMPLATE_NOTIFICATION, TEMPLATE_SECURITY):
            for parameter in shipped.render(name, context=_context()).parameters:
                assert "\n" not in parameter
                assert "\t" not in parameter
                assert "    " not in parameter
