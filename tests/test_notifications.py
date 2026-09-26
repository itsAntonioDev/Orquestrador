"""Testes das notificações (mensagem, Slack, e-mail, listener e configuração)."""

from __future__ import annotations

import json
import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest

from orquestrador.bootstrap import build_listeners
from orquestrador.config import Settings
from orquestrador.dispatch import RunOutcome, RunRequest
from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.notifications.base import NotificationError, Notifier
from orquestrador.notifications.email import EmailNotifier, SmtpSettings
from orquestrador.notifications.listener import NotificationListener
from orquestrador.notifications.message import (
    FailedJob,
    RunNotification,
    build_notification,
    build_slack_payload,
    render_html,
    render_text,
)
from orquestrador.notifications.models import NotificationConfig, NotifyWhen
from orquestrador.notifications.slack import SlackNotifier
from orquestrador.projects import Project, ProjectRegistry, ProjectRegistryError, Provider
from tests.conftest import make_pipeline
from tests.test_db_repository import make_request

SLACK_URL = "https://hooks.slack.com/services/T000/B000/segredo"


def failed_outcome(run_id: str = "run1") -> RunOutcome:
    pipeline = make_pipeline(
        {
            "lint": {"continue-on-error": True, "steps": [{"run": "lint"}]},
            "build": {"steps": [{"name": "compilar", "run": "make"}]},
            "deploy": {"needs": "build", "steps": [{"run": "deploy"}]},
        }
    )
    result = PipelineRunner.create_result(pipeline, RunContext(run_id=run_id))
    result.status = Status.FAILURE
    result.duration = 125.0
    result.jobs["lint"].status = Status.FAILURE
    result.jobs["build"].status = Status.FAILURE
    result.jobs["build"].error = "step 'compilar' falhou"
    result.jobs["build"].steps[0].status = Status.FAILURE
    result.jobs["build"].steps[0].error = "comando terminou com código de saída 2"
    result.jobs["deploy"].status = Status.SKIPPED
    return RunOutcome(run_id=run_id, project="api", status="completed", result=result)


def success_outcome(run_id: str = "run1") -> RunOutcome:
    outcome = failed_outcome(run_id)
    assert outcome.result is not None
    outcome.result.status = Status.SUCCESS
    for job in outcome.result.jobs.values():
        job.status = Status.SUCCESS
    return outcome


def html_request() -> RunRequest:
    request = make_request()
    request.trigger.message = "<b>corrige</b> bug & melhora"
    return request


class TestMessage:
    def test_build_notification(self) -> None:
        notification = build_notification(
            failed_outcome(), html_request(), "https://ci.exemplo.com/"
        )
        assert notification.status == "failure"
        assert notification.title == "Falhou: api / teste"
        assert notification.url == "https://ci.exemplo.com/runs/run1"
        assert notification.ref == "main"
        assert notification.commit == "a" * 40
        assert notification.duration == 125.0
        assert notification.failed_jobs == [
            FailedJob(name="build", error="comando terminou com código de saída 2")
        ]

    def test_build_without_request_or_result(self) -> None:
        outcome = RunOutcome(run_id="r", project="p", status="error", reason="checkout falhou")
        notification = build_notification(outcome, None)
        assert (notification.status, notification.title) == ("error", "Erro: p")
        assert notification.url is None and notification.repository is None

    def test_render_text(self) -> None:
        text = render_text(build_notification(failed_outcome(), html_request(), "https://ci"))
        assert text.startswith("Falhou: api / teste\n")
        assert "Commit: aaaaaaaaaa" in text
        assert "Duração: 2m 05s" in text
        assert "  - build: comando terminou com código de saída 2" in text
        assert "Detalhes: https://ci/runs/run1" in text

    def test_render_html_escapes_content(self) -> None:
        body = render_html(build_notification(failed_outcome(), html_request(), "https://ci"))
        assert "<b>corrige</b>" not in body
        assert "&lt;b&gt;corrige&lt;/b&gt; bug &amp; melhora" in body
        assert "href='https://ci/runs/run1'" in body

    def test_slack_payload(self) -> None:
        notification = build_notification(failed_outcome(), html_request(), "https://ci")
        payload = build_slack_payload(notification, channel="#ci", username="bot")
        assert payload["text"] == "Falhou: api / teste"
        assert (payload["channel"], payload["username"]) == ("#ci", "bot")
        types = [block["type"] for block in payload["blocks"]]
        assert types == ["section", "section", "section", "actions"]
        assert payload["blocks"][0]["text"]["text"].startswith(":x: *Falhou: api / teste*")
        assert "&lt;b&gt;corrige" in payload["blocks"][0]["text"]["text"]
        assert payload["blocks"][-1]["elements"][0]["url"] == "https://ci/runs/run1"

    def test_slack_payload_with_reason(self) -> None:
        outcome = RunOutcome(run_id="r", project="p", status="error", reason="Redis <fora>")
        payload = build_slack_payload(build_notification(outcome, None))
        assert "channel" not in payload
        assert payload["blocks"][-1]["type"] == "context"
        assert "Redis &lt;fora&gt;" in payload["blocks"][-1]["elements"][0]["text"]


def notification() -> RunNotification:
    return build_notification(failed_outcome(), make_request(), "https://ci")


class TestSlack:
    def test_send(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, text="ok")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        SlackNotifier(SLACK_URL, channel="#ci", client=client).send(notification())

        assert str(captured[0].url) == SLACK_URL
        body = json.loads(captured[0].content)
        assert body["text"] == "Falhou: api / teste"
        assert body["channel"] == "#ci"

    def test_http_error(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="erro"))
        )
        with pytest.raises(NotificationError, match="respondeu 500"):
            SlackNotifier(SLACK_URL, client=client).send(notification())

    def test_network_error_does_not_leak_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"falha conectando em {SLACK_URL}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(NotificationError) as info:
            SlackNotifier(SLACK_URL, client=client).send(notification())
        assert "segredo" not in str(info.value)
        assert "ConnectError" in str(info.value)

    def test_without_injected_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict[str, Any]] = []

        def fake_post(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
            sent.append(kwargs["json"])
            return httpx.Response(204, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        SlackNotifier(SLACK_URL).send(notification())
        assert sent and sent[0]["text"] == "Falhou: api / teste"


class FakeSMTP:
    """Cliente SMTP falso compatível com ``with smtplib.SMTP(...)``."""

    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, **options: Any) -> None:
        self.host = host
        self.port = port
        self.options = options
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        self.fail_login = False
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"credenciais invalidas")
        self.logged_in = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)


def smtp_factory(fail_login: bool = False) -> tuple[Callable[..., FakeSMTP], list[FakeSMTP]]:
    created: list[FakeSMTP] = []

    def factory(host: str, port: int, **options: Any) -> FakeSMTP:
        client = FakeSMTP(host, port, **options)
        client.fail_login = fail_login
        created.append(client)
        return client

    return factory, created


class TestEmail:
    SETTINGS = SmtpSettings(
        host="smtp.exemplo.com", port=587, username="ci", password="senha", sender="ci@exemplo.com"
    )

    def test_send_with_starttls_and_login(self) -> None:
        factory, created = smtp_factory()
        notifier = EmailNotifier(
            self.SETTINGS, ["a@exemplo.com", "b@exemplo.com"], smtp_factory=factory
        )

        notifier.send(build_notification(failed_outcome(), html_request(), "https://ci"))

        client = created[0]
        assert (client.host, client.port, client.options["timeout"]) == (
            "smtp.exemplo.com",
            587,
            10.0,
        )
        assert client.started_tls
        assert client.logged_in == ("ci", "senha")
        message = client.messages[0]
        assert message["To"] == "a@exemplo.com, b@exemplo.com"
        assert message["From"] == "ci@exemplo.com"
        assert message["Subject"] == "[CI] Falhou: api / teste (main) #run1"
        html_part = message.get_body(preferencelist=("html",))
        assert html_part is not None
        assert "&lt;b&gt;corrige" in html_part.get_content()
        text_part = message.get_body(preferencelist=("plain",))
        assert text_part is not None and "Jobs com falha" in text_part.get_content()

    def test_ssl_without_authentication(self) -> None:
        factory, created = smtp_factory()
        settings = SmtpSettings(host="smtp", port=465, use_ssl=True, sender="ci@x.com")
        EmailNotifier(settings, ["a@x.com"], subject_prefix="[Deploy]", smtp_factory=factory).send(
            notification()
        )
        client = created[0]
        assert not client.started_tls
        assert client.logged_in is None
        assert "context" in client.options
        assert client.messages[0]["Subject"].startswith("[Deploy] Falhou")

    def test_smtp_errors(self) -> None:
        factory, _ = smtp_factory(fail_login=True)
        with pytest.raises(NotificationError, match="SMTPAuthenticationError"):
            EmailNotifier(self.SETTINGS, ["a@x.com"], smtp_factory=factory).send(notification())

    def test_connection_errors(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> FakeSMTP:
            raise ConnectionRefusedError("recusado")

        with pytest.raises(NotificationError, match="ConnectionRefusedError"):
            EmailNotifier(self.SETTINGS, ["a@x.com"], smtp_factory=refuse).send(notification())

    def test_default_factory_is_smtplib(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeSMTP.instances.clear()
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        EmailNotifier(self.SETTINGS, ["a@x.com"]).send(notification())
        assert FakeSMTP.instances and FakeSMTP.instances[0].messages


class RecordingNotifier(Notifier):
    name = "gravador"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[RunNotification] = []
        self.error = error

    def send(self, notification: RunNotification) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(notification)


def registry_with(config: NotificationConfig | None) -> ProjectRegistry:
    return ProjectRegistry(
        [
            Project(
                name="api",
                provider=Provider.GITHUB,
                repository="acme/api",
                secret="s",
                notifications=config,
            )
        ]
    )


def listener_with(
    config: NotificationConfig | None, notifiers: list[Notifier]
) -> NotificationListener:
    return NotificationListener(
        registry_with(config), public_url="https://ci", notifier_factory=lambda cfg: notifiers
    )


class TestListener:
    def test_default_when_is_failure_and_error(self) -> None:
        recorder = RecordingNotifier()
        listener = listener_with(NotificationConfig(), [recorder])
        request = make_request()

        listener.run_started(request)
        listener.run_finished(failed_outcome())
        listener.run_finished(success_outcome("run2"))

        assert len(recorder.sent) == 1
        assert recorder.sent[0].commit == request.trigger.commit
        assert recorder.sent[0].url == "https://ci/runs/run1"
        assert listener._requests == {}

    def test_custom_when(self) -> None:
        recorder = RecordingNotifier()
        listener = listener_with(NotificationConfig(when=[NotifyWhen.SUCCESS]), [recorder])
        listener.run_finished(failed_outcome())
        listener.run_finished(success_outcome())
        assert [item.status for item in recorder.sent] == ["success"]

    def test_dispatch_failure_is_notified(self) -> None:
        recorder = RecordingNotifier()
        listener = listener_with(NotificationConfig(), [recorder])
        listener.run_dispatch_failed(make_request(), ConnectionError("Redis indisponível"))
        assert recorder.sent[0].status == "error"
        assert recorder.sent[0].reason == "falha ao agendar a execução: Redis indisponível"

    def test_projects_without_notifications_are_ignored(self) -> None:
        recorder = RecordingNotifier()
        listener = listener_with(None, [recorder])
        assert listener.notify(failed_outcome(), None) == 0
        unknown = RunOutcome(run_id="x", project="desconhecido", status="error")
        assert listener.notify(unknown, None) == 0
        assert recorder.sent == []

    def test_notifier_errors_are_isolated(self, caplog: pytest.LogCaptureFixture) -> None:
        ok = RecordingNotifier()
        listener = listener_with(
            NotificationConfig(),
            [
                RecordingNotifier(NotificationError("Slack fora")),
                RecordingNotifier(RuntimeError("bug")),
                ok,
            ],
        )
        with caplog.at_level(logging.WARNING):
            assert listener.notify(failed_outcome(), None) == 1
        assert len(ok.sent) == 1
        assert "Slack fora" in caplog.text
        assert "erro inesperado" in caplog.text

    def test_default_notifiers_from_config(self, caplog: pytest.LogCaptureFixture) -> None:
        config = NotificationConfig(
            slack={"webhook_url": SLACK_URL, "channel": "#ci"},  # type: ignore[arg-type]
            email={"to": ["time@exemplo.com"]},  # type: ignore[arg-type]
        )
        without_smtp = NotificationListener(registry_with(config))
        with caplog.at_level(logging.WARNING):
            notifiers = without_smtp.notifiers_for(config)
        assert [type(item) for item in notifiers] == [SlackNotifier]
        assert "ORQ_SMTP_HOST" in caplog.text

        with_smtp = NotificationListener(registry_with(config), smtp=SmtpSettings(host="smtp"))
        assert [type(item) for item in with_smtp.notifiers_for(config)] == [
            SlackNotifier,
            EmailNotifier,
        ]


class TestConfiguration:
    def test_project_notifications_from_yaml(self, write_file: Callable[..., Path]) -> None:
        path = write_file(
            """
            projects:
              - name: api
                provider: github
                repository: acme/api
                secret: s
                notifications:
                  when: [failure, error, success]
                  slack:
                    webhook_url: ${SLACK_URL}
                    channel: "#deploys"
                  email:
                    to: [time@exemplo.com]
                    subject_prefix: "[API]"
            """,
            name="projects.yml",
        )
        registry = ProjectRegistry.from_file(path, environ={"SLACK_URL": SLACK_URL})
        project = registry.get("api")
        assert project is not None and project.notifications is not None
        config = project.notifications
        assert config.should_notify("success") and not config.should_notify("cancelled")
        assert config.slack is not None
        assert config.slack.webhook_url.get_secret_value() == SLACK_URL
        assert SLACK_URL not in repr(project)
        assert config.email is not None and config.email.subject_prefix == "[API]"

    @pytest.mark.parametrize(
        ("notifications", "fragment"),
        [
            ({"slack": {"webhook_url": "http://inseguro"}}, "https"),
            ({"email": {"to": ["invalido"]}}, "e-mail inválido"),
            ({"email": {"to": []}}, "to"),
            ({"when": ["talvez"]}, "when"),
            ({"telegram": {}}, "telegram"),
        ],
    )
    def test_invalid_configuration(self, notifications: dict[str, Any], fragment: str) -> None:
        data = {
            "projects": [
                {
                    "name": "api",
                    "provider": "github",
                    "repository": "acme/api",
                    "secret": "s",
                    "notifications": notifications,
                }
            ]
        }
        with pytest.raises(ProjectRegistryError, match=fragment):
            ProjectRegistry.from_data(data, environ={})

    def test_smtp_settings(self) -> None:
        assert Settings().smtp_settings is None
        smtp = Settings(smtp_host="smtp.x", smtp_password="p", smtp_ssl=True).smtp_settings
        assert smtp is not None
        assert (smtp.host, smtp.password, smtp.use_ssl, smtp.port) == ("smtp.x", "p", True, 587)

    def test_bootstrap_adds_notification_listener(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path)
        assert build_listeners(settings, None, registry_with(None)) == []
        listeners = build_listeners(settings, None, registry_with(NotificationConfig()))
        assert [type(item) for item in listeners] == [NotificationListener]
