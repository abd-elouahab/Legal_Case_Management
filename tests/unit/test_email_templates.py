"""Unit tests for ``services/email_templates.py`` and the shipped templates.

Two halves, and the second is the one that matters most.

The **renderer** is a near-twin of the prompt library, so its tests are the same
shape: version discovery from filenames, a pinned version, a strict failure on a
missing variable, and a translated failure on a missing file.

The **escaping** is where the two genuinely differ, and it is the security
property this feature has to hold: an administrator's announcement reaches
``{message}``, which reaches both bodies, so the HTML part must escape and the
plain-text part must not. Getting that backwards in either direction is a defect —
entities in somebody's terminal, or unescaped input in their mail client — and
neither is visible without a test that renders both parts from the same hostile
input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.email import TEMPLATE_NOTIFICATION, TEMPLATE_SECURITY, build_email_context
from services.email_templates import (
    DEFAULT_EMAIL_TEMPLATE_ROOT,
    EmailTemplateNotFoundError,
    EmailTemplateRenderError,
    JinjaEmailTemplateRenderer,
    available_email_template_renderers,
    get_email_template_renderer,
    reset_email_template_cache,
)


@pytest.fixture
def renderer() -> JinjaEmailTemplateRenderer:
    return JinjaEmailTemplateRenderer()


def _context(**overrides: Any) -> dict[str, Any]:
    """The context a real delivery renders with, so the tests exercise the real
    variable set rather than a hand-written approximation of it."""
    base = build_email_context(
        rule_key="case.assigned",
        category="case",
        priority="high",
        context={"case_number": "CASE-2026-0001"},
        recipient_name="Amina Benali",
        language="fr",
        base_url="https://legal.example",
        target_type="case",
        target_id=None,
        platform_name="Legal Platform",
    ).as_mapping()
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


class TestDiscovery:
    def test_the_shipped_templates_are_found(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        assert renderer.versions(TEMPLATE_NOTIFICATION) == [1]
        assert renderer.versions(TEMPLATE_SECURITY) == [1]

    def test_an_unknown_template_has_no_versions(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        assert renderer.versions("whatsapp-someday") == []

    def test_a_version_counts_only_when_all_three_parts_exist(
        self, tmp_path: Path
    ) -> None:
        """A v2 with a new HTML body and last year's plain text is a
        half-finished edit, and offering it would let `version=None` select a
        message whose two halves say different things."""
        (tmp_path / "partial.v1.subject.j2").write_text("s", encoding="utf-8")
        (tmp_path / "partial.v1.html.j2").write_text("<p>h</p>", encoding="utf-8")
        assert JinjaEmailTemplateRenderer(tmp_path).versions("partial") == []

        (tmp_path / "partial.v1.text.j2").write_text("t", encoding="utf-8")
        assert JinjaEmailTemplateRenderer(tmp_path).versions("partial") == [1]

    def test_the_root_is_resolved_from_the_module_rather_than_the_cwd(self) -> None:
        """The API runs from `apps/api`, the tests from the repo root, and a
        worker thread from wherever the process was started."""
        assert DEFAULT_EMAIL_TEMPLATE_ROOT.is_dir()
        assert DEFAULT_EMAIL_TEMPLATE_ROOT.name == "emails"

    def test_availability_is_probed(self, renderer: JinjaEmailTemplateRenderer) -> None:
        assert renderer.is_available() is True
        assert JinjaEmailTemplateRenderer(Path("nowhere-at-all")).is_available() is False


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRendering:
    def test_all_three_parts_are_produced(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        rendered = renderer.render(TEMPLATE_NOTIFICATION, version=1, context=_context())
        assert rendered.subject == "Dossier attribué"
        assert rendered.html.startswith("<html")
        assert "Bonjour Amina Benali," in rendered.text

    def test_the_subject_is_a_single_line(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """A `.j2` file ends with a newline, and a `Subject` header containing one
        is a header-injection attempt as far as every mail library is
        concerned."""
        rendered = renderer.render(TEMPLATE_NOTIFICATION, version=1, context=_context())
        assert "\n" not in rendered.subject
        assert "\r" not in rendered.subject

    def test_the_template_identity_travels_with_the_text(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        rendered = renderer.render(TEMPLATE_NOTIFICATION, version=1, context=_context())
        assert (rendered.name, rendered.version) == (TEMPLATE_NOTIFICATION, 1)

    def test_a_missing_variable_fails_loudly(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """`StrictUndefined`: a template that silently lost its `action_url` would
        send a correct-looking email with no way to act on it, and nobody would
        find out from the outside."""
        with pytest.raises(EmailTemplateRenderError):
            renderer.render(TEMPLATE_NOTIFICATION, version=1, context={"title": "x"})

    def test_a_missing_template_is_translated(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        with pytest.raises(EmailTemplateNotFoundError):
            renderer.render("whatsapp-someday", context=_context())

    def test_a_missing_version_is_translated(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        with pytest.raises(EmailTemplateNotFoundError):
            renderer.render(TEMPLATE_NOTIFICATION, version=99, context=_context())


# --------------------------------------------------------------------------- #
# Escaping — the reason there are two Jinja environments
# --------------------------------------------------------------------------- #


class TestEscaping:
    HOSTILE = "<script>alert('x')</script> & \"quoted\" 'apostrophe'"

    def test_the_html_part_escapes(self, renderer: JinjaEmailTemplateRenderer) -> None:
        """An administrator's announcement reaches `{message}`, which reaches
        this."""
        rendered = renderer.render(
            TEMPLATE_NOTIFICATION, version=1, context=_context(message=self.HOSTILE)
        )
        assert "<script>" not in rendered.html
        assert "&lt;script&gt;" in rendered.html

    def test_the_plain_text_part_does_not(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """Escaping here would rewrite the apostrophes of a French sentence into
        entities in somebody's terminal or screen reader."""
        rendered = renderer.render(
            TEMPLATE_NOTIFICATION, version=1, context=_context(message="l'audience & vous")
        )
        assert "l'audience & vous" in rendered.text
        assert "&#39;" not in rendered.text
        assert "&amp;" not in rendered.text

    def test_the_subject_is_not_escaped(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """A `Subject` header is not markup: `&amp;` in an inbox list is a bug."""
        rendered = renderer.render(
            TEMPLATE_NOTIFICATION, version=1, context=_context(title="Dossier & audience")
        )
        assert rendered.subject == "Dossier & audience"


# --------------------------------------------------------------------------- #
# What the shipped templates actually say
# --------------------------------------------------------------------------- #


class TestShippedTemplates:
    def test_a_notification_offers_its_link(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        rendered = renderer.render(
            TEMPLATE_NOTIFICATION,
            version=1,
            context=_context(action_url="https://legal.example/cases/abc"),
        )
        assert "https://legal.example/cases/abc" in rendered.html
        assert "https://legal.example/cases/abc" in rendered.text
        assert "Ouvrir dans la plateforme" in rendered.html

    def test_a_notification_without_a_link_offers_none(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """Linkless but correct, rather than a button pointing at nothing."""
        rendered = renderer.render(
            TEMPLATE_NOTIFICATION, version=1, context=_context(action_url=None)
        )
        assert "Ouvrir dans la plateforme" not in rendered.html
        assert "href" not in rendered.html

    def test_a_security_email_never_offers_a_link(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """A message about somebody's password that asks them to click something
        is the exact shape of a phishing email."""
        rendered = renderer.render(
            TEMPLATE_SECURITY,
            version=1,
            context=_context(action_url="https://legal.example/settings"),
        )
        assert "https://legal.example/settings" not in rendered.html
        assert "https://legal.example/settings" not in rendered.text
        assert "<a " not in rendered.html

    def test_a_security_email_says_what_to_do_if_unexpected(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        rendered = renderer.render(TEMPLATE_SECURITY, version=1, context=_context())
        assert "contactez immédiatement" in rendered.html
        assert "contactez immédiatement" in rendered.text

    def test_arabic_renders_right_to_left(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """`ui-context.md` requires RTL for Arabic, and an email is the one surface
        where the application's own stylesheet is not available."""
        arabic = build_email_context(
            rule_key="case.assigned",
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001"},
            recipient_name="أمينة",
            language="ar",
            base_url="https://legal.example",
            target_type="case",
            target_id=None,
            platform_name="منصة",
        ).as_mapping()

        rendered = renderer.render(TEMPLATE_NOTIFICATION, version=1, context=arabic)
        assert 'dir="rtl"' in rendered.html
        assert 'lang="ar"' in rendered.html
        assert "text-align:right" in rendered.html
        assert "تم إسناد ملف إليك" in rendered.text

    def test_no_template_loads_anything_from_the_network(
        self, renderer: JinjaEmailTemplateRenderer
    ) -> None:
        """A mail client blocks remote resources, and a template that needed one
        would render as a broken box for most readers."""
        for name in (TEMPLATE_NOTIFICATION, TEMPLATE_SECURITY):
            html = renderer.render(name, version=1, context=_context()).html
            assert "<img" not in html
            assert "<link" not in html
            assert "<script" not in html


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_renderer_is_resolvable_and_shared(self) -> None:
        reset_email_template_cache()
        assert available_email_template_renderers() == ["jinja-files"]
        assert get_email_template_renderer() is get_email_template_renderer()

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        reset_email_template_cache()
        assert get_email_template_renderer("mjml-someday").name == "jinja-files"
