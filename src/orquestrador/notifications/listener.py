"""Dispara notificações ao fim das execuções, conforme a configuração de cada projeto."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable

from orquestrador.dispatch.listeners import RunListener
from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.notifications.base import NotificationError, Notifier
from orquestrador.notifications.email import EmailNotifier, SmtpSettings
from orquestrador.notifications.message import build_notification
from orquestrador.notifications.models import NotificationConfig
from orquestrador.notifications.slack import SlackNotifier
from orquestrador.projects import ProjectRegistry

logger = logging.getLogger(__name__)

NotifierFactory = Callable[[NotificationConfig], Iterable[Notifier]]


class NotificationListener(RunListener):
    """Envia notificações de execuções finalizadas (ou que não puderam ser agendadas).

    Falhas de entrega são registradas em log e nunca afetam a execução.
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        *,
        smtp: SmtpSettings | None = None,
        public_url: str | None = None,
        timeout: float = 10.0,
        notifier_factory: NotifierFactory | None = None,
    ) -> None:
        """Cria o listener.

        Args:
            registry: Projetos (com a seção ``notifications``).
            smtp: Servidor SMTP; sem ele, e-mails são ignorados com aviso.
            public_url: URL do dashboard para links.
            timeout: Tempo limite de cada envio.
            notifier_factory: Substitui a criação dos notificadores (testes).
        """
        self.registry = registry
        self.smtp = smtp
        self.public_url = public_url
        self.timeout = timeout
        self.notifier_factory = notifier_factory
        self._requests: dict[str, RunRequest] = {}
        self._lock = threading.Lock()

    def run_started(self, request: RunRequest) -> None:
        with self._lock:
            self._requests[request.run_id] = request

    def run_dispatch_failed(self, request: RunRequest, error: Exception) -> None:
        outcome = RunOutcome(
            run_id=request.run_id,
            project=request.project,
            status="error",
            reason=f"falha ao agendar a execução: {error}",
        )
        self.notify(outcome, request)

    def run_finished(self, outcome: RunOutcome) -> None:
        with self._lock:
            request = self._requests.pop(outcome.run_id, None)
        self.notify(outcome, request)

    def notifiers_for(self, config: NotificationConfig) -> list[Notifier]:
        """Canais configurados para o projeto."""
        if self.notifier_factory is not None:
            return list(self.notifier_factory(config))
        notifiers: list[Notifier] = []
        if config.slack is not None:
            notifiers.append(
                SlackNotifier(
                    config.slack.webhook_url.get_secret_value(),
                    channel=config.slack.channel,
                    username=config.slack.username,
                    timeout=self.timeout,
                )
            )
        if config.email is not None:
            if self.smtp is None:
                logger.warning(
                    "notificação por e-mail configurada, mas ORQ_SMTP_HOST não foi definido"
                )
            else:
                notifiers.append(
                    EmailNotifier(
                        self.smtp, config.email.to, subject_prefix=config.email.subject_prefix
                    )
                )
        return notifiers

    def notify(self, outcome: RunOutcome, request: RunRequest | None) -> int:
        """Envia a notificação se o projeto pedir; retorna quantos canais a receberam."""
        project = self.registry.get(outcome.project)
        if project is None or project.notifications is None:
            return 0
        if not project.notifications.should_notify(outcome.final_status):
            return 0
        notification = build_notification(outcome, request, self.public_url)
        delivered = 0
        for notifier in self.notifiers_for(project.notifications):
            try:
                notifier.send(notification)
                delivered += 1
            except NotificationError as exc:
                logger.warning(
                    "[%s] notificação via %s falhou: %s", outcome.run_id, notifier.name, exc
                )
            except Exception:
                logger.exception(
                    "[%s] erro inesperado na notificação via %s", outcome.run_id, notifier.name
                )
        return delivered
