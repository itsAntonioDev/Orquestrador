# Orquestrador

Orquestrador de CI/CD self-hosted escrito em Python — uma versão simplificada de
ferramentas como GitHub Actions e Jenkins. Pipelines são definidos em YAML e
executados localmente ou (nas próximas fases) disparados por webhooks do
GitHub/GitLab, rodando em containers Docker.

> Projeto de portfólio construído de forma incremental, fase por fase.

## Progresso

| Fase | Descrição | Status |
|------|-----------|--------|
| 1 | Núcleo: parser YAML, executor via subprocess, CLI | ✅ Concluída |
| 2 | Webhooks GitHub/GitLab (FastAPI) | ✅ Concluída |
| 3 | Execução em containers Docker | ✅ Concluída |
| 4 | Fila de jobs com Redis + RQ | ✅ Concluída |
| 5 | Persistência em PostgreSQL | ✅ Concluída |
| 6 | Dashboard web com logs ao vivo | ✅ Concluída |

## Instalação

Requer Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Uso da CLI

```bash
# Valida o pipeline e mostra a ordem de execução
orquestrador validate examples/hello.yml

# Executa localmente, simulando um push na main
orquestrador run examples/hello.yml --event push --branch main

# Variáveis extras, jobs em paralelo e relatório JSON
orquestrador run pipeline.yml -e VERSION=1.2.3 --parallel 4 --report result.json
```

Códigos de saída: `0` sucesso, `1` falha, `2` pipeline inválido, `130` cancelado (Ctrl+C).

## Execução em containers Docker

```bash
orquestrador run examples/docker.yml --executor docker            # CLI
ORQ_EXECUTOR=docker orquestrador serve                              # servidor
```

Cada **job** ganha um container próprio, criado a partir de `image` (do job, do pipeline ou
`ORQ_DOCKER_DEFAULT_IMAGE`), com o workspace montado em `/workspace`. Os steps rodam via
`docker exec` no mesmo container, então arquivos gerados por um step ficam disponíveis para os
seguintes. O container é removido ao fim do job.

```yaml
image: python:3.11-slim        # padrão para todos os jobs
jobs:
  test:
    image: python:3.12          # sobrescreve
    env: { PYTHONUNBUFFERED: "1" }
    volumes:
      - pip-cache:/root/.cache/pip        # volume nomeado
      - ./artefatos:/artefatos:ro         # relativo ao workspace
      - source: /srv/cache                # caminho do host (exige permissão)
        target: /cache
        read-only: true
    steps:
      - run: pytest
```

- **Isolamento de ambiente**: o container recebe só as variáveis do pipeline, nunca as do host.
- **Timeout/cancelamento**: a árvore de processos do step é morta *dentro* do container (via
  `/proc`), preservando o container para steps `if: always()`.
- **Segurança de volumes**: volumes nomeados e caminhos relativos ao workspace são sempre
  permitidos; caminhos absolutos do host (ex.: `/var/run/docker.sock`) são negados, a menos que
  `ORQ_DOCKER_ALLOW_BIND_MOUNTS=true`, opcionalmente restritos por
  `ORQ_DOCKER_ALLOWED_BIND_PATHS='["/srv/cache"]'`.
- **Limites**: `ORQ_DOCKER_MEMORY_LIMIT` (ex.: `2g`), `ORQ_DOCKER_CPUS`, `ORQ_DOCKER_NETWORK`,
  política de pull `ORQ_DOCKER_PULL_POLICY` (`always` | `if-not-present` | `never`).
- **Docker-out-of-Docker**: quando o orquestrador roda num container usando o socket do host,
  `ORQ_DOCKER_HOST_WORKSPACES_DIR` informa onde os workspaces estão no host.

Requisito da imagem: `/bin/sh` e `cut` (Debian, Alpine, Ubuntu...). Imagens distroless não são suportadas.

## Servidor de webhooks

```bash
cp projects.example.yml projects.yml      # configure os repositórios aceitos
export GITHUB_WEBHOOK_SECRET=...           # segredos referenciados via ${VAR}
orquestrador serve --host 0.0.0.0 --port 8000
```

Configure no GitHub (*Settings → Webhooks*) a URL `https://seu-host/webhooks/github`,
content type `application/json` e o mesmo segredo; no GitLab, `https://seu-host/webhooks/gitlab`
com o *Secret token*. Eventos aceitos: `push` (branches e tags) e `pull_request` / *Merge Request*.

| Endpoint | Descrição |
|----------|-----------|
| `POST /webhooks/{github\|gitlab}` | Recebe o evento, valida assinatura e agenda a execução |
| `GET /health` | Status da API e quantidade de projetos |
| `GET /docs` | Documentação OpenAPI interativa |

Respostas: `202 queued` (execução agendada), `200 ignored`/`pong`, `401` assinatura inválida,
`404` repositório não configurado, `400` payload inválido, `413` corpo grande demais,
`503` falha ao agendar.

Fluxo de uma execução:

1. A rota lê o corpo bruto (com limite), identifica o projeto e valida a **assinatura HMAC-SHA256**
   (GitHub, `X-Hub-Signature-256`) ou o **token** (GitLab, `X-Gitlab-Token`) em tempo constante.
2. O evento é normalizado num `TriggerEvent` e enviado ao `Dispatcher`; a API responde na hora.
3. O `RunService` clona o repositório no commit exato (`git clone` endurecido: sem prompt,
   protocolos restritos, SHA validado), lê o pipeline de dentro do repositório, confere os
   gatilhos `on` (globs `*`, `**` e negação `!`) e executa com o `PipelineRunner`.
4. O resultado é gravado em `<ORQ_DATA_DIR>/runs/<run_id>.json` e o workspace é removido.

Configuração por ambiente (prefixo `ORQ_`, veja `.env.example`): `ORQ_PROJECTS_FILE`,
`ORQ_DATA_DIR`, `ORQ_MAX_PARALLEL_JOBS`, `ORQ_MAX_CONCURRENT_RUNS`, `ORQ_KEEP_WORKSPACES`,
`ORQ_MAX_WEBHOOK_BODY_BYTES`, `ORQ_CHECKOUT_TIMEOUT`.

## Fila de execuções (Redis + RQ)

Por padrão (`ORQ_DISPATCHER=thread`) a API executa os pipelines num pool de threads do próprio
processo — prático para desenvolvimento. Em produção, use a fila:

```bash
export ORQ_DISPATCHER=rq ORQ_REDIS_URL=redis://localhost:6379/0
orquestrador serve                 # recebe webhooks e só enfileira (responde 202 na hora)
orquestrador worker                # consome a fila e executa; rode quantos quiser
orquestrador worker --burst        # processa o que houver e encerra
```

```
GitHub/GitLab ──webhook──▶ API (FastAPI) ──RunRequest JSON──▶ Redis (fila "orquestrador")
                                                                  │
                                         ┌────────────────────────┼────────────────────────┐
                                         ▼                        ▼                        ▼
                                     worker 1                 worker 2                 worker N
                                  RunService → Executor (local | docker) → PipelineRunner
```

- **Desacoplamento**: a API nunca executa pipeline; se o Redis cair, o webhook recebe `503` e o
  GitHub/GitLab pode reenviar.
- **Paralelismo em dois níveis**: N workers = N execuções simultâneas; dentro de cada execução,
  jobs independentes rodam em paralelo (`ORQ_MAX_PARALLEL_JOBS`).
- **Serialização JSON** (não pickle): a fila é legível e um Redis comprometido não injeta objetos
  Python nos workers. O `run_id` é o ID do job no RQ (idempotente e rastreável).
- **Workers**: `fork` (padrão no Linux, cada execução num processo filho) ou `simple` (Windows).
  `ORQ_RUN_TIMEOUT` limita a duração máxima; falhas inesperadas vão para o *failed registry* do RQ.
- `GET /health` mostra o estado da fila: `queued`, `started`, `failed` e `workers`.

## Histórico de execuções (PostgreSQL)

Cada execução é gravada com status, duração e logs de cada step:

```
runs ──1:N── job_runs ──1:N── step_runs ──1:N── log_lines
(evento, commit,   (status, erro,     (status, exit code,   (seq, stdout/stderr,
 status, motivo)    duração)           duração, truncadas)   texto, timestamp)
```

```bash
export ORQ_DATABASE_URL=postgresql+psycopg://orq:senha@localhost:5432/orquestrador
orquestrador db upgrade          # aplica as migrações Alembic
orquestrador db current          # revisão aplicada x disponível
orquestrador history --project api --status failure
```

Sem `ORQ_DATABASE_URL`, usa SQLite em `<ORQ_DATA_DIR>/orquestrador.db` (ótimo para desenvolvimento).

| Endpoint | Descrição |
|----------|-----------|
| `GET /api/runs?project=&status=&limit=&offset=` | Execuções paginadas, mais recentes primeiro |
| `GET /api/runs/{run_id}` | Detalhes com jobs, steps e contagem de linhas de log |
| `GET /api/runs/{run_id}/jobs/{job_id}/steps/{n}/logs?after=` | Logs incrementais (base do streaming ao vivo) |

Ciclo de vida registrado:

1. **queued** — a API registra a execução antes de enfileirar (se o Redis falhar, vira **error**).
2. **running** — o worker marca o início; após o checkout, cria jobs e steps como *pending*.
3. Durante a execução, o `PersistenceObserver` atualiza jobs/steps e grava logs **em lotes**
   (100 linhas ou 0,5 s), com limite por step (`ORQ_MAX_LOG_LINES_PER_STEP`).
4. **success / failure / cancelled / skipped / error** — desfecho final com duração e motivo.

Decisões: SQLAlchemy 2.0 tipado + Alembic (migrações versionadas, `render_as_batch` para
SQLite); datas sempre em UTC; `ON DELETE CASCADE` em toda a hierarquia; o repositório devolve
DTOs Pydantic, então API e CLI nunca manipulam sessões ORM; falhas do banco são isoladas e
**nunca** interrompem um pipeline.

## Dashboard web

Com `orquestrador serve` no ar, acesse `http://localhost:8000`:

| Página | O que mostra |
|--------|--------------|
| `/runs` | Execuções com filtros por projeto e status; a tabela se atualiza sozinha a cada 3 s (HTMX) |
| `/runs/{run_id}` | Metadados do commit, jobs e steps com status e duração, e **logs ao vivo** por step |
| `/projects` | Projetos configurados e a última execução de cada um (segredos nunca são exibidos) |

Logs ao vivo via **WebSocket** (`/ws/runs/{run_id}`): o servidor consulta o banco periodicamente
(`ORQ_LIVE_POLL_INTERVAL`, padrão 0,5 s) e envia só o que mudou — o estado da execução quando algo
muda (`run`), as linhas novas de cada step (`logs`) e `end` quando termina. O navegador reconecta
sozinho com backoff se a conexão cair.

Decisões:

- **Server-side rendering + HTMX** em vez de SPA: nenhum build de frontend, templates Jinja2 com
  escape automático e o HTMX (vendorizado em `static/`, funciona sem internet) para atualizações parciais.
- **WebSocket alimentado pelo banco** em vez de Redis pub/sub: API e workers só precisam
  compartilhar o PostgreSQL; o intervalo casa com o lote de gravação de logs do `PersistenceObserver`.
- **Segurança no navegador**: logs e mensagens de commit entram no DOM apenas via `textContent`.

## Formato do pipeline

```yaml
name: meu-projeto

on:                         # gatilhos (usados pelos webhooks)
  push:
    branches: [main, "release/*"]
  pull_request:

env:                        # variáveis globais
  PYTHON_VERSION: "3.11"

jobs:
  lint:
    steps:
      - run: ruff check .

  test:
    needs: lint             # dependências (formam um DAG)
    timeout: 600            # tempo limite do job (s)
    env:
      SUITE: unit
    steps:
      - name: testes
        run: pytest
        timeout: 300        # tempo limite do step (s)
      - name: cobertura opcional
        run: coverage report --fail-under=90
        continue-on-error: true
      - name: limpeza
        if: always()        # roda mesmo após falha
        run: rm -rf .cache

  deploy:
    needs: test
    if: branch == 'main' && event == 'push'
    steps:
      - run: ./deploy.sh
        shell: bash         # bash | sh | python | pwsh | powershell | cmd | "template {0}"
        working-directory: infra
```

### Semântica de execução

- **Steps** rodam em sequência. Se um step falha, os seguintes são **ignorados**,
  exceto os que usam `if: always()` ou `if: failure()`.
- **Jobs** são ordenados topologicamente pelo `needs`. Jobs sem dependência entre si
  podem rodar em paralelo (`--parallel N`). Se uma dependência falha ou é ignorada,
  o job é ignorado (salvo `if: always()`).
- `continue-on-error` marca o step/job como falho, mas não propaga a falha.
- Cada step recebe `stdout`, `stderr`, código de saída e duração no resultado.

### Expressões `if`

Avaliadas por um interpretador seguro baseado em AST (nunca `eval`).

| Recurso | Exemplo |
|---------|---------|
| Variáveis | `branch`, `tag`, `event`, `commit`, `ref`, `repository`, `actor`, `pipeline`, `job`, `run_id` |
| Ambiente | `env.DEPLOY == 'true'` |
| Operadores | `==`, `!=`, `<`, `in`, `not in`, `and`/`&&`, `or`/`\|\|`, `not`/`!` |
| Funções | `startsWith()`, `endsWith()`, `contains()`, `matches()` (glob) |
| Status | `success()`, `failure()`, `always()`, `cancelled()` |

Sem função de status, a expressão é combinada implicitamente com `success()`.

### Variáveis injetadas

`CI=true`, `ORQ_RUN_ID`, `ORQ_PIPELINE`, `ORQ_JOB`, `ORQ_STEP`, `ORQ_EVENT`,
`ORQ_WORKSPACE`, `ORQ_REF`, `ORQ_BRANCH`, `ORQ_TAG`, `ORQ_COMMIT`, `ORQ_REPOSITORY`, `ORQ_ACTOR`,
`ORQ_BASE_BRANCH` e `ORQ_PULL_REQUEST` (em pull/merge requests).

Precedência (maior vence): `--env` da CLI > `env` do step > `env` do job > `env` do pipeline > variáveis injetadas.

## Arquitetura (até agora)

```
src/orquestrador/
├── pipeline/          # modelos Pydantic, parser YAML, avaliador de condições
├── execution/         # PipelineRunner, RunContext, resultados, observadores de eventos
├── executors/         # Executor/JobSession, LocalExecutor, DockerExecutor, shells, fábrica
├── webhooks/          # adaptadores GitHub/GitLab, TriggerEvent, matching de gatilhos
├── dispatch/          # RunRequest/RunOutcome, RunService, dispatchers (sync, threads, RQ)
├── bootstrap.py       # composition root: monta registro, serviço e dispatcher
├── api/               # FastAPI: create_app, rotas de webhooks, health e /api/runs
├── db/                # SQLAlchemy: modelos, repositório, observer, listener, migrações Alembic
├── web/               # dashboard: páginas Jinja2/HTMX, WebSocket de logs ao vivo, CSS/JS
├── projects.py        # registro de projetos (projects.yml com ${VAR})
├── scm.py             # checkout git seguro
├── config.py          # Settings (pydantic-settings, prefixo ORQ_)
├── console.py         # observador que imprime o progresso no terminal
└── cli.py             # CLI (Typer): run, validate, serve, worker, history, db, version
```

- **Executor → JobSession**: o executor é uma fábrica sem estado; a sessão guarda o
  estado de um job (diretório temporário no `LocalExecutor`, o container no
  `DockerExecutor`). Isso permite jobs paralelos com uma única instância de executor, e
  trocar subprocess por Docker não exigiu nenhuma mudança no runner.
- **Observadores**: o runner só emite eventos (`on_step_output`, `on_job_end`...).
  Console, banco de dados, WebSocket e notificações são observadores independentes.
- **Dispatcher**: a API nunca executa pipelines na requisição; ela entrega um `RunRequest`
  (JSON serializável) a um `Dispatcher` — pool de threads ou fila Redis + RQ. Trocar um pelo
  outro na Fase 4 não mudou nenhuma linha da rota de webhooks nem do `RunService`.
- **Adaptadores de provedor**: GitHub e GitLab implementam o mesmo `ProviderAdapter`
  (repositório, verificação, tipo de evento, parse), então há uma única rota genérica.
- **Pipeline no repositório**: como no GitHub Actions, o YAML vive no próprio repositório e é
  lido no commit exato do evento; o servidor só guarda o mapeamento repositório → segredo.
- O próprio projeto tem um pipeline de CI em [.orquestrador.yml](.orquestrador.yml).

## Testes

```bash
pytest                 # suíte completa
pytest --cov=orquestrador
pytest -m docker       # só integração com Docker real (pulados sem daemon)
ORQ_TEST_POSTGRES_URL=postgresql+psycopg://postgres:teste@localhost:5433/postgres pytest -m postgres
ruff check .
```

Os testes do `DockerExecutor` usam um cliente Docker falso ([tests/fake_docker.py](tests/fake_docker.py)),
então rodam em qualquer máquina; [tests/test_docker_integration.py](tests/test_docker_integration.py)
exercita um daemon real quando disponível.
