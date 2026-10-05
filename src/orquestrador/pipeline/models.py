"""Modelos declarativos de um pipeline (Pipeline -> Job -> Step).

Os modelos são implementados com Pydantic v2, o que nos dá validação estrita,
mensagens de erro detalhadas e serialização JSON gratuita (útil para a API,
a fila de jobs e a persistência nas fases seguintes).
"""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Job identifiers: letters, digits, ``_`` and ``-``, starting with a letter or ``_``.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

#: Valid environment variable name (POSIX).
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``source:/destination`` or ``source:/destination:ro|rw`` (source may contain ``C:\``).
VOLUME_SPEC_PATTERN = re.compile(r"^(?P<source>.+?):(?P<target>/[^:]*)(?::(?P<mode>ro|rw))?$")

#: Docker named volume name (no slashes: not a host path).
NAMED_VOLUME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _coerce_env(value: Any) -> dict[str, str]:
    """Normaliza um mapeamento de variáveis de ambiente para ``dict[str, str]``.

    YAML converte ``true``/``1`` em ``bool``/``int``; variáveis de ambiente são
    sempre strings, então convertemos escalares e rejeitamos estruturas aninhadas.

    Args:
        value: Valor bruto vindo do YAML.

    Returns:
        Dicionário com chaves e valores string.

    Raises:
        ValueError: Se o valor não for um mapeamento, se algum nome for inválido
            ou se algum valor não for escalar.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("env deve ser um mapeamento NOME: valor")
    result: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key)
        if not ENV_NAME_PATTERN.match(name):
            raise ValueError(f"nome de variável de ambiente inválido: {name!r}")
        if isinstance(raw, dict | list):
            raise ValueError(f"valor da variável {name!r} deve ser escalar")
        if raw is None:
            result[name] = ""
        elif isinstance(raw, bool):
            result[name] = "true" if raw else "false"
        else:
            result[name] = str(raw)
    return result


EnvMapping = Annotated[dict[str, str], Field(default_factory=dict)]


class StrictModel(BaseModel):
    """Base comum: proíbe campos desconhecidos para pegar erros de digitação no YAML."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)


class Step(StrictModel):
    """Um passo de um job: um comando de shell executado em sequência.

    Attributes:
        name: Nome legível do step. Se omitido, derivado do comando.
        run: Comando (ou script multi-linha) a executar.
        env: Variáveis de ambiente específicas deste step.
        condition: Expressão ``if`` avaliada antes de executar o step.
        timeout: Tempo máximo em segundos (``None`` = sem limite).
        continue_on_error: Se ``True``, uma falha não interrompe o job.
        working_directory: Diretório relativo ao workspace onde rodar o comando.
        shell: Shell a usar (ex.: ``bash``, ``sh``). ``None`` = padrão do executor.
        group: Grupo paralelo (preenchido pelo parser a partir de blocos ``parallel``).
    """

    name: str = ""
    run: str
    env: EnvMapping
    condition: str | None = Field(default=None, alias="if")
    timeout: float | None = Field(default=None, gt=0)
    continue_on_error: bool = Field(default=False, alias="continue-on-error")
    working_directory: str | None = Field(default=None, alias="working-directory")
    shell: str | None = None
    group: str | None = None

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: Any) -> dict[str, str]:
        return _coerce_env(value)

    @field_validator("run")
    @classmethod
    def _validate_run(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run não pode ser vazio")
        return value

    @model_validator(mode="after")
    def _default_name(self) -> Step:
        if not self.name.strip():
            first_line = self.run.strip().splitlines()[0]
            self.name = first_line if len(first_line) <= 60 else first_line[:57] + "..."
        return self


class VolumeMount(StrictModel):
    """Volume montado no container de um job.

    Aceita a forma curta ``origem:/destino[:ro]`` ou o mapeamento completo.
    A origem pode ser um volume nomeado (``cache``), um caminho relativo ao
    workspace (``./cache``) ou um caminho absoluto do host (``/data``) — este
    último só é aceito se o executor permitir bind mounts.

    Attributes:
        source: Volume nomeado ou caminho de origem.
        target: Caminho absoluto dentro do container.
        read_only: Monta somente leitura.
    """

    source: str = Field(min_length=1)
    target: str
    read_only: bool = Field(default=False, alias="read-only")

    @model_validator(mode="before")
    @classmethod
    def _parse_short_syntax(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        match = VOLUME_SPEC_PATTERN.match(value.strip())
        if match is None:
            raise ValueError(f"volume inválido: {value!r} (use origem:/destino[:ro])")
        return {
            "source": match.group("source"),
            "target": match.group("target"),
            "read_only": match.group("mode") == "ro",
        }

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("o destino do volume deve ser um caminho absoluto no container")
        normalized = posixpath.normpath(value)
        if normalized == "/":
            raise ValueError("o destino do volume não pode ser '/'")
        return normalized

    @property
    def is_named(self) -> bool:
        """Se a origem é um volume nomeado (e não um caminho do host)."""
        return bool(NAMED_VOLUME_PATTERN.match(self.source))


#: Accepted keys in a ``parallel`` step block.
PARALLEL_BLOCK_KEYS = frozenset(
    {"parallel", "name", "fail-fast", "fail_fast", "max-parallel", "max_parallel"}
)


class ParallelGroup(StrictModel):
    """Configuração de um bloco ``parallel`` de steps.

    Attributes:
        name: Identificador do grupo.
        fail_fast: Cancela os demais steps do grupo quando um deles falha.
        max_parallel: Máximo de steps simultâneos (``None`` = todos).
    """

    name: str = Field(min_length=1)
    fail_fast: bool = Field(default=False, alias="fail-fast")
    max_parallel: int | None = Field(default=None, alias="max-parallel", ge=1)


def _expand_parallel_blocks(data: dict[str, Any]) -> dict[str, Any]:
    """Converte blocos ``parallel`` em steps comuns marcados com ``group``.

    Mantém ``Job.steps`` como lista plana — executores, banco e dashboard
    continuam lidando com steps indexados; só o runner agrupa a execução.

    Raises:
        ValueError: Bloco com menos de 2 steps, aninhado, com campos
            desconhecidos ou com nome repetido.
    """
    steps: list[Any] = []
    groups: dict[str, Any] = dict(data.get("groups") or {})
    counter = 0
    for position, item in enumerate(data["steps"]):
        if not (isinstance(item, dict) and "parallel" in item):
            steps.append(item)
            continue
        unknown = set(item) - PARALLEL_BLOCK_KEYS
        if unknown:
            raise ValueError(
                f"steps.{position}: campos desconhecidos no bloco parallel: "
                + ", ".join(sorted(unknown))
            )
        children = item["parallel"]
        if not isinstance(children, list) or len(children) < 2:
            raise ValueError(f"steps.{position}: bloco parallel deve conter ao menos 2 steps")
        counter += 1
        group_id = str(item.get("name") or f"paralelo-{counter}")
        if group_id in groups:
            raise ValueError(f"grupo paralelo repetido: {group_id}")
        groups[group_id] = {
            "name": group_id,
            "fail-fast": item.get("fail-fast", item.get("fail_fast", False)),
            "max-parallel": item.get("max-parallel", item.get("max_parallel")),
        }
        for child in children:
            if isinstance(child, dict) and "parallel" in child:
                raise ValueError("blocos parallel não podem ser aninhados")
            steps.append({**child, "group": group_id} if isinstance(child, dict) else child)
    return {**data, "steps": steps, "groups": groups}


class Job(StrictModel):
    """Um job: sequência de steps executados no mesmo ambiente.

    Attributes:
        id: Identificador do job (chave no mapeamento ``jobs`` do YAML).
        name: Nome legível; padrão = ``id``.
        steps: Steps executados em ordem.
        env: Variáveis de ambiente do job (sobrescrevem as do pipeline).
        needs: IDs dos jobs que precisam terminar com sucesso antes deste.
        condition: Expressão ``if`` avaliada antes de iniciar o job.
        timeout: Tempo máximo do job inteiro em segundos.
        continue_on_error: Se ``True``, falha do job não marca o pipeline como falho.
        image: Imagem de container usada pelo ``DockerExecutor``.
        volumes: Volumes extras montados no container do job.
        groups: Grupos paralelos de steps, indexados pelo nome.
    """

    id: str = ""
    name: str = ""
    steps: list[Step] = Field(min_length=1)
    env: EnvMapping
    needs: list[str] = Field(default_factory=list)
    condition: str | None = Field(default=None, alias="if")
    timeout: float | None = Field(default=None, gt=0)
    continue_on_error: bool = Field(default=False, alias="continue-on-error")
    image: str | None = Field(default=None, min_length=1)
    volumes: list[VolumeMount] = Field(default_factory=list)
    groups: dict[str, ParallelGroup] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _expand_parallel(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return _expand_parallel_blocks(data)
        return data

    @model_validator(mode="after")
    def _validate_groups(self) -> Job:
        for step in self.steps:
            if step.group is not None and step.group not in self.groups:
                raise ValueError(
                    f"step '{step.name}' referencia grupo paralelo inexistente: {step.group}"
                )
        return self

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: Any) -> dict[str, str]:
        return _coerce_env(value)

    @field_validator("volumes")
    @classmethod
    def _unique_volume_targets(cls, value: list[VolumeMount]) -> list[VolumeMount]:
        targets = [volume.target for volume in value]
        duplicated = sorted({target for target in targets if targets.count(target) > 1})
        if duplicated:
            raise ValueError(f"destino de volume repetido: {', '.join(duplicated)}")
        return value

    @field_validator("needs", mode="before")
    @classmethod
    def _normalize_needs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class TriggerFilter(StrictModel):
    """Filtro de um evento que dispara o pipeline (ex.: ``push`` em ``main``).

    Attributes:
        branches: Padrões glob de branches aceitos (vazio = todos).
        tags: Padrões glob de tags aceitas (vazio = todas).
    """

    branches: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("branches", "tags", mode="before")
    @classmethod
    def _to_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class Pipeline(StrictModel):
    """Definição completa de um pipeline.

    Attributes:
        name: Nome do pipeline.
        triggers: Eventos que disparam o pipeline (chave ``on`` no YAML).
        env: Variáveis de ambiente globais.
        image: Imagem padrão para jobs que não definem ``image``.
        jobs: Jobs indexados por ID, na ordem de declaração.
    """

    name: str = Field(min_length=1)
    triggers: dict[str, TriggerFilter] = Field(default_factory=dict, alias="on")
    env: EnvMapping
    image: str | None = Field(default=None, min_length=1)
    jobs: dict[str, Job] = Field(min_length=1)

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: Any) -> dict[str, str]:
        return _coerce_env(value)

    @field_validator("triggers", mode="before")
    @classmethod
    def _normalize_triggers(cls, value: Any) -> dict[str, Any]:
        """Aceita ``on: push``, ``on: [push, pull_request]`` ou o mapeamento completo."""
        if value is None:
            return {}
        if isinstance(value, str):
            return {value: {}}
        if isinstance(value, list):
            return {str(event): {} for event in value}
        if isinstance(value, dict):
            return {str(k): (v or {}) for k, v in value.items()}
        return value

    @field_validator("jobs", mode="before")
    @classmethod
    def _inject_job_ids(cls, value: Any) -> Any:
        """Copia a chave do mapeamento para ``Job.id`` e valida o formato do identificador."""
        if not isinstance(value, dict):
            return value
        jobs: dict[str, Any] = {}
        for key, raw in value.items():
            job_id = str(key)
            if not IDENTIFIER_PATTERN.match(job_id):
                raise ValueError(f"ID de job inválido: {job_id!r}")
            if isinstance(raw, dict):
                raw = {**raw, "id": job_id}
                raw.setdefault("name", job_id)
            jobs[job_id] = raw
        return jobs

    @model_validator(mode="after")
    def _validate_graph(self) -> Pipeline:
        """Garante que ``needs`` referencia jobs existentes e que não há ciclos."""
        for job in self.jobs.values():
            if not job.name:
                job.name = job.id
            for dep in job.needs:
                if dep not in self.jobs:
                    raise ValueError(f"job {job.id!r} depende de job inexistente {dep!r}")
                if dep == job.id:
                    raise ValueError(f"job {job.id!r} não pode depender de si mesmo")
        cycle = _find_cycle({job_id: job.needs for job_id, job in self.jobs.items()})
        if cycle:
            raise ValueError("dependência circular entre jobs: " + " -> ".join(cycle))
        if self.image:
            for job in self.jobs.values():
                if job.image is None:
                    job.image = self.image
        return self

    def execution_stages(self) -> list[list[Job]]:
        """Agrupa os jobs em estágios topológicos.

        Jobs do mesmo estágio não dependem uns dos outros e podem rodar em
        paralelo; cada estágio só começa quando todos os anteriores terminam.

        Returns:
            Lista de estágios, cada um com jobs na ordem de declaração.
        """
        remaining = dict(self.jobs)
        done: set[str] = set()
        stages: list[list[Job]] = []
        while remaining:
            ready = [job for job in remaining.values() if set(job.needs) <= done]
            # The cycle validator ensures that ``ready`` is never empty.
            stages.append(ready)
            for job in ready:
                done.add(job.id)
                del remaining[job.id]
        return stages


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Encontra um ciclo num grafo dirigido via DFS.

    Args:
        graph: Mapeamento nó -> lista de nós dos quais ele depende.

    Returns:
        O caminho do ciclo (com o nó inicial repetido no final) ou ``None``.
    """
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = gray
        stack.append(node)
        for dep in graph.get(node, []):
            if color.get(dep) == gray:
                return [*stack[stack.index(dep) :], dep]
            if color.get(dep) == white:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = black
        return None

    for node in graph:
        if color[node] == white:
            found = visit(node)
            if found:
                return found
    return None
