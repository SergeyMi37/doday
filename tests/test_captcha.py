"""Google reCAPTCHA на форме регистрации.

Капча включается ключами в .env. Общая фикстура в conftest их гасит, поэтому
тесты ниже выставляют ключи сами.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.auth import captcha
from app.config import get_settings
from app.main import _CSP
from tests.conftest import signup_form_token


@pytest.fixture
def captcha_on() -> Iterator[None]:
    settings = get_settings()
    settings.recaptcha_site_key = "6Ltest-site-key"
    settings.recaptcha_secret_key = "6Ltest-secret-key"
    yield
    # Вернёт общая фикстура _no_captcha из conftest.


class _Response:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _fake_google(monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]) -> list[str]:
    """Подменить поход к Google и запомнить, куда он шёл."""
    calls: list[str] = []

    async def post(self: object, url: str, data: dict[str, str]) -> _Response:
        calls.append(url)
        return _Response(status, body)

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    return calls


# ── виджет на странице ────────────────────────────────────────────────────


async def test_widget_shown_when_keys_set(client: AsyncClient, captcha_on: None) -> None:
    html = (await client.get("/auth/register")).text
    assert 'class="g-recaptcha"' in html
    assert 'data-sitekey="6Ltest-site-key"' in html
    # recaptcha.net, а не google.com: у части пользователей google.com режут.
    assert "https://www.recaptcha.net/recaptcha/api.js?hl=ru" in html


async def test_no_widget_without_keys(client: AsyncClient) -> None:
    # Проверяем именно виджет: строка «g-recaptcha» есть и в коде проверки
    # перед отправкой, она на странице всегда.
    assert "data-sitekey" not in (await client.get("/auth/register")).text


async def test_no_widget_when_only_site_key_set(client: AsyncClient) -> None:
    """Без секрета сервер ничего не проверяет — незачем заставлять решать."""
    get_settings().recaptcha_site_key = "6Ltest-site-key"
    assert "data-sitekey" not in (await client.get("/auth/register")).text


def test_csp_allows_the_widget() -> None:
    """Без этого браузер молча не загрузит виджет, и регистрация встанет."""
    directives = dict(part.split(" ", 1) for part in _CSP.split("; "))
    assert "https://www.recaptcha.net" in directives["script-src"]
    assert "https://www.recaptcha.net" in directives["frame-src"]
    assert "https://www.gstatic.com" in directives["script-src"]


# ── серверная проверка ────────────────────────────────────────────────────


async def test_registration_without_token_rejected(client: AsyncClient, captcha_on: None) -> None:
    r = await client.post(
        "/auth/register",
        data={
            "email": "human-captcha@gmail.com",
            "password": "StrongPass12345",
            "agree_privacy": "on",
            "form_ts": signup_form_token(),
        },
    )
    assert r.status_code == 400
    assert "капча" in r.text


async def test_verify_goes_to_recaptcha_net(
    monkeypatch: pytest.MonkeyPatch, captcha_on: None
) -> None:
    calls = _fake_google(monkeypatch, 200, {"success": True})
    assert await captcha.verify("token", "45.132.20.1") is True
    assert calls == ["https://www.recaptcha.net/recaptcha/api/siteverify"]


async def test_failed_check_rejected(monkeypatch: pytest.MonkeyPatch, captcha_on: None) -> None:
    _fake_google(monkeypatch, 200, {"success": False, "error-codes": ["invalid-input-response"]})
    assert await captcha.verify("forged", None) is False


async def test_google_outage_does_not_block_signup(
    monkeypatch: pytest.MonkeyPatch, captcha_on: None
) -> None:
    """Сбой у Google не должен закрывать регистрацию: ботов подстрахуют
    остальные проверки."""
    _fake_google(monkeypatch, 503, {})
    assert await captcha.verify("token", None) is True


async def test_disabled_captcha_always_passes() -> None:
    assert await captcha.verify("", None) is True


# ── введённое не пропадает ────────────────────────────────────────────────


async def test_email_and_consent_survive_an_error(client: AsyncClient, captcha_on: None) -> None:
    """Ошибка не должна стоить человеку набранной почты и галочки."""
    r = await client.post(
        "/auth/register",
        data={
            "email": "vasya.petrov@gmail.com",
            "password": "StrongPass12345",
            "agree_privacy": "on",
            "form_ts": signup_form_token(),
        },
    )
    assert r.status_code == 400
    assert "vasya.petrov@gmail.com" in r.text  # подставлено обратно в поле
    assert "checked" in r.text  # галочка согласия осталась


async def test_password_is_never_echoed_back(client: AsyncClient, captcha_on: None) -> None:
    """Пароль обратно не возвращаем: он осел бы в кеше браузера и скриншотах."""
    r = await client.post(
        "/auth/register",
        data={
            "email": "vasya.petrov@gmail.com",
            "password": "SuperSecret12345",
            "agree_privacy": "on",
            "form_ts": signup_form_token(),
        },
    )
    assert "SuperSecret12345" not in r.text


async def test_unchecked_consent_keeps_email(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/register",
        data={
            "email": "masha@yandex.ru",
            "password": "StrongPass12345",
            "form_ts": signup_form_token(),
        },
    )
    assert r.status_code == 400
    assert "masha@yandex.ru" in r.text
    assert "согласие" in r.text.lower()


async def test_form_guards_captcha_before_submit(client: AsyncClient, captcha_on: None) -> None:
    """Нерешённая капча не должна приводить к перезагрузке страницы."""
    html = (await client.get("/auth/register")).text
    assert "grecaptcha.getResponse()" in html
    assert "Поставь галочку «Я не робот»" in html
