"""Motor de execução: percorre jobs e steps aplicando condições, timeouts e dependências."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from orquestrador.execution.cancellation import AnyCancelSignal, CancelSignal
from orquestrador.execution.context import RunContext
from orquestrador.execution.events import CompositeObserver, RunObserver
from orquestrador.execution.masking import SecretMasker
from orquestrador.execution.results import (
    JobResult,
    LogLine,
    PipelineResult,
    Status,
    StepResult,
    utcnow,
)
from orquestrador.executors.base import (
    CommandRequest,
    CommandResult,
    Executor,
    JobSession,
    StreamName,
)
from orquestrador.pipeline.conditions import ConditionContext, ConditionError, evaluate_condition
from orquestrador.pipeline.expressions import ExpressionError, interpolate
from orquestrador.pipeline.models import Job, Pipeline, Step

logger = logging.getLogger(__name__)

#: Motivo registrado em steps interrompidos pelo ``fail-fast`` de um grupo paralelo.
FAIL_FAST_REASON = "cancelado: outro step do grupo paralelo falhou (fail-fast)"

StepBlock = list[tuple[int, Step]]


def step_blocks(job: Job) -> list[StepBlock]:
    """Divide os steps em blocos: steps consecutivos do mesmo grupo paralelo ficam juntos.

    Args:
        job: Job cujos steps serão agrupados.

    Returns:
        Lista de blocos ``[(índice, step), ...]`` na ordem de declaração.
    """
    blocks: list[StepBlock] = []
    for index, step in enumerate(job.steps):
        if step.group is not None and blocks and blocks[-1][0][1].group == step.group:
            blocks[-1].append((index, step))
        else:
            blocks.append([(index, step)])
    return blocks


class PipelineRunner:
    """Executa um ``Pipeline`` usando um ``Executor``.

    Regras principais:

    * Jobs são agrupados em estágios pelo grafo ``needs``; estágios rodam em
      ordem e os jobs de um estágio podem rodar em paralelo.
    * Steps rodam em sequência, exceto os de um bloco ``parallel``, que rodam
      simultaneamente na mesma sessão do job.
    * Um step que falha interrompe o job: os próximos steps são ignorados, a
      menos que usem ``if: always()`` ou ``if: failure()``.
    * Um job cuja dependência falhou é ignorado (salvo ``if: always()``).
    * ``continue-on-error`` marca o step/job como falho, mas não propaga a falha.
    * ``${{ secrets.X }}`` é resolvido na hora de executar e mascarado na saída.
    """

    def __init__(
        self,
        executor: Executor,
        observers: Iterable[RunObserver] = (),
        *,
        max_parallel_jobs: int = 1,
    ) -> None:
        """Cria o runner.

        Args:
            executor: Executor usado para abrir as sessões dos jobs.
            observers: Observadores que recebem os eventos da execução.
            max_parallel_jobs: Máximo de jobs simultâneos por estágio.

        Raises:
            ValueError: Se ``max_parallel_jobs`` for menor que 1.
        """
        if max_parallel_jobs < 1:
            raise ValueError("max_parallel_jobs deve ser >= 1")
        self.executor = executor
        self.observer = CompositeObserver(observers)
        self.max_parallel_jobs = max_parallel_jobs

    @staticmethod
    def create_result(pipeline: Pipeline, context: RunContext) -> PipelineResult:
        """Cria um resultado com todos os jobs e steps em ``pending``.

        Útil para registrar a execução (ex.: no banco) antes de ela começar.
        """
        return PipelineResult(
            run_id=context.run_id,
            pipeline=pipeline.name,
            jobs={
                job.id: JobResult(
                    job_id=job.id,
                    name=job.name,
                    allowed_failure=job.continue_on_error,
                    steps=[
                        StepResult(
                            index=i,
                            name=step.name,
                            allowed_failure=step.continue_on_error,
                            group=step.group,
                        )
                        for i, step in enumerate(job.steps)
                    ],
                )
                for job in pipeline.jobs.values()
            },
        )

    def run(
        self,
        pipeline: Pipeline,
        context: RunContext | None = None,
        result: PipelineResult | None = None,
    ) -> PipelineResult:
        """Executa o pipeline até o fim.

        Args:
            pipeline: Pipeline validado.
            context: Contexto da execução (padrão: execução manual no diretório atual).
            result: Resultado pré-criado com ``create_result`` (opcional).

        Returns:
            O resultado final, com status de cada job e step.
        """
        context = context or RunContext()
        result = result or self.create_result(pipeline, context)
        result.status = Status.RUNNING
        result.started_at = utcnow()
        started = time.perf_counter()
        self.observer.on_pipeline_start(pipeline, context, result)

        for stage in pipeline.execution_stages():
            workers = min(self.max_parallel_jobs, len(stage))
            if workers <= 1:
                for job in stage:
                    self._run_job(pipeline, job, context, result)
                continue
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="orq-job") as pool:
                futures = [
                    pool.submit(self._run_job, pipeline, job, context, result) for job in stage
                ]
                for future in futures:
                    future.result()

        result.status = self._pipeline_status(result, context)
        result.finished_at = utcnow()
        result.duration = time.perf_counter() - started
        self.observer.on_pipeline_end(pipeline, result)
        return result

    @staticmethod
    def _pipeline_status(result: PipelineResult, context: RunContext) -> Status:
        jobs = result.jobs.values()
        if context.cancelled and any(job.status == Status.CANCELLED for job in jobs):
            return Status.CANCELLED
        if any(job.status == Status.FAILURE and not job.allowed_failure for job in jobs):
            return Status.FAILURE
        return Status.SUCCESS

    def _run_job(
        self, pipeline: Pipeline, job: Job, context: RunContext, result: PipelineResult
    ) -> None:
        """Executa um job, convertendo qualquer erro inesperado em falha do job."""
        job_result = result.jobs[job.id]
        try:
            self._execute_job(pipeline, job, context, result, job_result)
        except Exception as exc:
            logger.exception("erro interno ao executar o job %s", job.id)
            job_result.error = f"erro interno: {exc}"
            self._finish_job(job, job_result, Status.FAILURE, started=None)

    def _finish_job(
        self,
        job: Job,
        job_result: JobResult,
        status: Status,
        *,
        started: float | None,
        pending_steps: Status = Status.SKIPPED,
    ) -> None:
        """Finaliza o job: status, steps pendentes, duração e evento ``on_job_end``."""
        job_result.status = status
        for step_result in job_result.steps:
            if not step_result.status.is_terminal:
                step_result.status = pending_steps
        job_result.finished_at = utcnow()
        if started is not None:
            job_result.duration = time.perf_counter() - started
        self.observer.on_job_end(job, job_result)

    def _execute_job(
        self,
        pipeline: Pipeline,
        job: Job,
        context: RunContext,
        result: PipelineResult,
        job_result: JobResult,
    ) -> None:
        if context.cancelled:
            self._finish_job(
                job, job_result, Status.CANCELLED, started=None, pending_steps=Status.CANCELLED
            )
            return

        base_env = {**context.builtin_env(pipeline.name, job.id), **pipeline.env, **job.env}
        variables = context.condition_variables(pipeline.name, job.id)
        dependencies_ok = all(result.jobs[dep].succeeded for dep in job.needs)
        condition = ConditionContext(
            variables=variables, env={**base_env, **context.env}, failed=not dependencies_ok
        )
        try:
            should_run = evaluate_condition(job.condition, condition)
        except ConditionError as exc:
            job_result.error = f"condição 'if' inválida: {exc}"
            self._finish_job(job, job_result, Status.FAILURE, started=None)
            return
        if not should_run:
            self._finish_job(job, job_result, Status.SKIPPED, started=None)
            return

        started = time.perf_counter()
        job_result.status = Status.RUNNING
        job_result.started_at = utcnow()
        self.observer.on_job_start(job, job_result)
        deadline = time.monotonic() + job.timeout if job.timeout else None

        try:
            session = self.executor.open_session(job, context)
        except Exception as exc:
            logger.exception("falha ao preparar o ambiente do job %s", job.id)
            job_result.error = f"falha ao preparar o ambiente do job: {exc}"
            self._finish_job(job, job_result, Status.FAILURE, started=started)
            return

        masker = SecretMasker(context.secrets.values())
        failed_step: str | None = None
        job_timed_out = False
        try:
            for block in step_blocks(job):
                if context.cancelled:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    job_timed_out = True
                    break
                job_failed = failed_step is not None
                if len(block) == 1:
                    index, step = block[0]
                    timed_out = self._run_step(
                        job,
                        step,
                        job_result.steps[index],
                        session,
                        context,
                        base_env,
                        variables,
                        job_failed=job_failed,
                        deadline=deadline,
                        masker=masker,
                    )
                    finished = [(index, step, timed_out)]
                else:
                    finished = self._run_parallel_block(
                        job,
                        block,
                        job_result,
                        session,
                        context,
                        base_env,
                        variables,
                        job_failed=job_failed,
                        deadline=deadline,
                        masker=masker,
                    )
                for index, step, timed_out in finished:
                    step_result = job_result.steps[index]
                    if (
                        step_result.status == Status.FAILURE
                        and not step.continue_on_error
                        and failed_step is None
                    ):
                        failed_step = step.name
                    job_timed_out = job_timed_out or timed_out
                if job_timed_out:
                    break
        finally:
            try:
                session.close()
            except Exception:
                logger.exception("falha ao encerrar a sessão do job %s", job.id)

        if context.cancelled:
            self._finish_job(
                job, job_result, Status.CANCELLED, started=started, pending_steps=Status.CANCELLED
            )
        elif job_timed_out:
            job_result.error = f"tempo limite do job excedido ({job.timeout:g}s)"
            self._finish_job(job, job_result, Status.FAILURE, started=started)
        elif failed_step is not None:
            job_result.error = f"step '{failed_step}' falhou"
            self._finish_job(job, job_result, Status.FAILURE, started=started)
        else:
            self._finish_job(job, job_result, Status.SUCCESS, started=started)

    def _run_parallel_block(
        self,
        job: Job,
        block: StepBlock,
        job_result: JobResult,
        session: JobSession,
        context: RunContext,
        base_env: dict[str, str],
        variables: dict[str, object],
        *,
        job_failed: bool,
        deadline: float | None,
        masker: SecretMasker,
    ) -> list[tuple[int, Step, bool]]:
        """Executa um grupo ``parallel`` com threads, respeitando ``max-parallel`` e ``fail-fast``.

        Returns:
            ``(índice, step, estourou tempo do job)`` para cada step, na ordem declarada.
        """
        group = job.groups.get(block[0][1].group or "")
        fail_fast = bool(group and group.fail_fast)
        limit = group.max_parallel if group and group.max_parallel else len(block)
        group_cancel = threading.Event()
        cancel = AnyCancelSignal(context.cancel_event, group_cancel)

        def run_one(item: tuple[int, Step]) -> tuple[int, Step, bool]:
            index, step = item
            step_result = job_result.steps[index]
            timed_out = self._run_step(
                job,
                step,
                step_result,
                session,
                context,
                base_env,
                variables,
                job_failed=job_failed,
                deadline=deadline,
                masker=masker,
                cancel=cancel,
                group_cancel=group_cancel,
            )
            if fail_fast and step_result.status == Status.FAILURE and not step.continue_on_error:
                group_cancel.set()
            return index, step, timed_out

        workers = max(1, min(limit, len(block)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="orq-step") as pool:
            return list(pool.map(run_one, block))

    def _finish_step_early(
        self, job: Job, step: Step, step_result: StepResult, status: Status, error: str | None
    ) -> bool:
        """Finaliza um step que não chegou a executar (ignorado, cancelado ou inválido)."""
        step_result.status = status
        step_result.error = error
        self.observer.on_step_end(job, step, step_result)
        return False

    def _run_step(
        self,
        job: Job,
        step: Step,
        step_result: StepResult,
        session: JobSession,
        context: RunContext,
        base_env: dict[str, str],
        variables: dict[str, object],
        *,
        job_failed: bool,
        deadline: float | None,
        masker: SecretMasker,
        cancel: CancelSignal | None = None,
        group_cancel: threading.Event | None = None,
    ) -> bool:
        """Executa um step e preenche ``step_result``.

        Returns:
            ``True`` se o step foi interrompido pelo tempo limite do *job*.
        """
        raw_env = {**base_env, **step.env, **context.env, "ORQ_STEP": step.name}
        if step.group:
            raw_env["ORQ_STEP_GROUP"] = step.group
        condition = ConditionContext(variables=variables, env=raw_env, failed=job_failed)
        try:
            should_run = evaluate_condition(step.condition, condition)
        except ConditionError as exc:
            return self._finish_step_early(
                job, step, step_result, Status.FAILURE, f"condição 'if' inválida: {exc}"
            )
        if not should_run:
            return self._finish_step_early(job, step, step_result, Status.SKIPPED, None)
        if group_cancel is not None and group_cancel.is_set():
            return self._finish_step_early(
                job, step, step_result, Status.CANCELLED, FAIL_FAST_REASON
            )

        try:
            env = {
                name: interpolate(value, secrets=context.secrets, env=raw_env)
                for name, value in raw_env.items()
            }
            command = interpolate(step.run, secrets=context.secrets, env=raw_env)
        except ExpressionError as exc:
            return self._finish_step_early(
                job, step, step_result, Status.FAILURE, f"expressão inválida: {exc}"
            )

        timeout = step.timeout
        limited_by_job = False
        if deadline is not None:
            remaining = max(deadline - time.monotonic(), 0.001)
            if timeout is None or remaining < timeout:
                timeout = remaining
                limited_by_job = True

        stdout: list[str] = []
        stderr: list[str] = []
        output: list[str] = []

        def on_output(stream: StreamName, text: str) -> None:
            text = masker.mask(text)
            if stream == "stdout":
                stdout.append(text)
            elif stream == "stderr":
                stderr.append(text)
            output.append(text)
            line = LogLine(stream=stream, text=text)
            self.observer.on_step_output(job, step, step_result, line)

        step_result.status = Status.RUNNING
        step_result.started_at = utcnow()
        self.observer.on_step_start(job, step, step_result)
        started = time.perf_counter()
        request = CommandRequest(
            command=command,
            env=env,
            shell=step.shell,
            working_directory=step.working_directory,
            timeout=timeout,
            cancel_event=cancel or context.cancel_event,
        )
        try:
            outcome = session.run(request, on_output)
        except Exception as exc:
            logger.exception("erro interno do executor no step %s", step.name)
            outcome = CommandResult(exit_code=None, error=f"erro interno do executor: {exc}")

        step_result.duration = time.perf_counter() - started
        step_result.finished_at = utcnow()
        step_result.exit_code = outcome.exit_code
        step_result.stdout = "\n".join(stdout)
        step_result.stderr = "\n".join(stderr)
        step_result.output = "\n".join(output)

        if outcome.cancelled:
            step_result.status = Status.CANCELLED
            interrupted_by_group = (
                group_cancel is not None and group_cancel.is_set() and not context.cancelled
            )
            step_result.error = FAIL_FAST_REASON if interrupted_by_group else "execução cancelada"
        elif outcome.timed_out:
            step_result.status = Status.FAILURE
            step_result.error = (
                "tempo limite do job excedido"
                if limited_by_job
                else f"tempo limite do step excedido ({step.timeout:g}s)"
            )
        elif outcome.error:
            step_result.status = Status.FAILURE
            step_result.error = masker.mask(outcome.error)
        elif outcome.exit_code != 0:
            step_result.status = Status.FAILURE
            step_result.error = f"comando terminou com código de saída {outcome.exit_code}"
        else:
            step_result.status = Status.SUCCESS

        self.observer.on_step_end(job, step, step_result)
        return outcome.timed_out and limited_by_job
