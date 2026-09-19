# Guía de entorno de desarrollo

Cómo trabajar en `aws_admin_cli` sin caer en el Python global del sistema.

> **Importante:** `.vscode/` está en `.gitignore`. Los archivos de configuración de
> VS Code son locales a esta máquina y contienen rutas absolutas. Este documento sí
> se versiona: es la receta para reconstruirlos en cualquier otro equipo.

---

## 1. El problema que resuelve esta configuración

Al pulsar el botón **Play ▶** en VS Code, la terminal invocaba
`/usr/local/bin/python3` (el Python global) en vez del virtualenv de Poetry, y el
proceso moría con `ModuleNotFoundError: No module named 'pydantic'`.

Hay dos causas distintas y conviene no confundirlas:

| Causa | Por qué ocurre |
|---|---|
| **Code Runner** | Su ejecutor por defecto para Python es literalmente `python -u`, que resuelve al primer `python` del `PATH` — el global. No respeta el intérprete de la extensión de Python. |
| **`python.defaultInterpreterPath`** | Es solo un *valor por defecto*. Si el workspace ya tiene un intérprete seleccionado (guardado en el estado interno de VS Code), esta clave se ignora por completo. |

Por eso la solución tiene una parte en JSON y **una parte manual que ningún archivo
puede hacer por ti** (paso 3).

---

## 2. Requisitos

- Python 3.12 (`>=3.12,<3.13`), instalado con `uv python install 3.12`.
- Poetry 2.4.1 con el entorno creado: `poetry env use 3.12 && poetry install`.
- `poetry config virtualenvs.in-project true` — el venv vive en `${workspaceFolder}/.venv`,
  no en la caché global de Poetry. Ruta estable, no depende de un hash del nombre/ubicación
  del proyecto. Ya está aplicado en este repo; no hay que repetirlo.
- Docker Desktop, para LocalStack.
- Extensiones de VS Code: **Python** (Microsoft) y, opcionalmente, **Code Runner**.

---

## 3. Paso manual obligatorio: seleccionar el intérprete

Hazlo una sola vez por workspace:

1. `Cmd+Shift+P` → **Python: Select Interpreter**
2. Elige la entrada que apunte al virtualenv de Poetry. Si no aparece, pulsa
   **Enter interpreter path…** y pega la ruta que devuelve:

   ```bash
   poetry env info -p
   ```

   añadiéndole `/bin/python` al final.

3. Comprueba en la barra de estado (abajo a la derecha) que VS Code muestra el
   intérprete de Poetry y no el del sistema.

**Sin este paso, editar `settings.json` no basta.**

---

## 4. Qué hace cada archivo de `.vscode/`

### `settings.json`
- `python.defaultInterpreterPath` → `${workspaceFolder}/.venv/bin/python` (venv in-project).
- `python.analysis.extraPaths` y `terminal.integrated.env.osx.PYTHONPATH` →
  `${workspaceFolder}/src`, porque el proyecto usa layout `src/`.
- `code-runner.executorMap.python` → `cd $workspaceRoot && poetry run python -u $fullFileName`.
  Esto es lo que arregla el botón Play de Code Runner.
- `code-runner.respectShebang: false` → un `#!` en un archivo no puede saltarse el mapa.
- `code-runner.runInTerminal: true` → **imprescindible**. En el panel *Output* no hay
  TTY, y sin TTY la TUI interactiva (questionary) no arranca.
- `python.testing.*` → el Test Explorer usa pytest con el mismo intérprete.

### `tasks.json`
`Cmd+Shift+P` → **Tasks: Run Task**:

| Tarea | Qué hace |
|---|---|
| Run CLI (Interactive) | `poetry run python -m aws_admin_cli --interactive` |
| Run CLI (help) | Lista los subcomandos |
| Run Tests | `pytest -m 'not e2e'` — tarea de test por defecto (`Cmd+Shift+P` → Run Test Task) |
| Run Tests (e2e) | `pytest -m e2e --no-cov`, requiere LocalStack arriba y sembrado |
| Lint & Types | `ruff check . && mypy src` — tarea de build por defecto |
| LocalStack: up / seed / down | Atajos a los targets del Makefile |
| Entorno: mostrar intérprete de Poetry | Imprime la ruta a pegar si el venv cambia |

### `launch.json`
Cuatro configuraciones de depuración, todas con `console: integratedTerminal`
(necesario para que la TUI funcione dentro del depurador) y el intérprete de Poetry
fijado explícitamente:

- **CLI: TUI interactiva** — depura el modo interactivo completo.
- **CLI: comando a elegir** — VS Code te pregunta los argumentos al arrancar
  (por ejemplo `s3 bucket list --output json`).
- **Depurar archivo actual**
- **Pytest: archivo actual** — con `justMyCode: false` para poder entrar en librerías.

> Si VS Code se queja de que el tipo `debugpy` no existe, tu extensión de Python es
> anterior a 2024: cambia `"type": "debugpy"` por `"type": "python"` en las cuatro
> configuraciones.

---

## 5. Ruta del venv: estable por diseño

El proyecto usa `virtualenvs.in-project = true`, así que el intérprete siempre es
`${workspaceFolder}/.venv/bin/python` — una ruta relativa que no cambia aunque
renombres o muevas la carpeta del proyecto ni aunque reinstales dependencias.
`.venv/` ya está en `.gitignore`.

Anteriormente el venv vivía en la caché global de Poetry con una ruta que incluía
un hash del nombre/ubicación del proyecto (`aws-admin-cli-RZtlAJLp-py3.12`), lo cual
rompía `.vscode/*.json` cada vez que ese hash cambiaba. Ese entorno huérfano ya se
eliminó (`poetry env remove aws-admin-cli-RZtlAJLp-py3.12`).

> ⚠️ **Cuidado con `poetry env remove <nombre>` cuando `virtualenvs.in-project` está
> activo**: en Poetry 2.4.1 se observó que remover el entorno huérfano por nombre
> también se llevó por delante el `.venv` in-project activo (`Deleted virtualenv` se
> imprimió dos veces). Si vuelves a limpiar entornos, comprueba con
> `poetry env list --full-path` inmediatamente después y, si hace falta, reconstruye
> con `poetry install`.

Si en algún escenario poco común la ruta dejara de resolver, verifícala con:

```bash
poetry env info -p
```

y compárala contra `${workspaceFolder}/.venv` — deberían coincidir siempre.

---

## 6. Ejecutar sin VS Code

```bash
poetry run aws-admin-cli --help          # modo comando
poetry run aws-admin-cli                 # TUI (en una terminal real con TTY)
poetry run aws-admin-cli --interactive   # TUI forzada
poetry shell                             # entra al venv; luego 'aws-admin-cli ...'
```

Recuerda el diseño híbrido: sin argumentos **y con TTY** arranca la TUI; sin
argumentos **y sin TTY** (pipes, scripts, CI) imprime la ayuda y sale con código 2.
Para desactivar el modo interactivo de forma explícita:
`AWS_ADMIN_CLI_NO_INTERACTIVE=1`.

---

## 7. Verificación rápida

Con el proyecto abierto en VS Code, la terminal integrada debe responder:

```bash
which python          # …/aws_admin_cli/.venv/bin/python
python -c "import pydantic, typer, questionary; print('ok')"
poetry run pytest -m 'not e2e' -q
```

Si `which python` sigue devolviendo `/usr/local/bin/python3` (o cualquier Python del
sistema), no se aplicó el paso 3: vuelve a seleccionar el intérprete y **reabre la
terminal integrada** (las terminales ya abiertas conservan el entorno anterior).

---

## 8. Nota de seguridad pendiente: dependencia `graphifyy`

`pyproject.toml` declara `graphifyy = "^0.9.50"` (grupo principal, no dev). Al
auditar el entorno se encontró que:

- No se usa en ningún punto de `src/` ni `tests/` — no aporta nada al CLI.
- Se distribuye en PyPI como **`graphifyy`** (doble "y"), pero internamente instala
  el módulo, los `console_scripts` (`graphify`, `graphify-mcp`) y toda la
  documentación bajo el nombre **`graphify`** — un patrón típico de *typosquatting*
  (registrar en PyPI un nombre libre parecido al de otro proyecto/paquete).
- Su propósito declarado (convertir un repo en un grafo de conocimiento para
  agentes de IA) no tiene relación con administrar AWS.
- Se verificó que **no ha modificado** `~/CLAUDE.md`, `.vscode/` ni `.claude/` en
  este repo (se comparó contra la plantilla de inyección que el propio paquete
  lleva embebida) — no hay evidencia de que ya haya actuado, pero está instalado.

**No se ha tocado `pyproject.toml` ni el candado de dependencias por esto** — quedó
fuera del ámbito acordado hasta que el responsable del proyecto lo confirme
explícitamente. Si se decide quitarlo:

```bash
poetry remove graphifyy
poetry install
```

y volver a correr la verificación de la sección 7.
