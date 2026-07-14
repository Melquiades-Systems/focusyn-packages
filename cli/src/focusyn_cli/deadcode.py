"""Cliente de dead code cross-repo — orquesta los detectores NATIVOS de cada lenguaje.

``focusyn deadcode`` corre en la máquina del desarrollador (o en CI), **nunca dentro del
gateway**. La razón es de seguridad y no es negociable (ADR MEL-DEC-197): los detectores de Go y
TS necesitan **construir** el proyecto (``go mod download`` / ``npm install``), y hacerlo en el
contenedor sería ejecutar código arbitrario de repos ajenos —un ``postinstall`` de npm lo es— en
el mismo proceso que tiene ``CREDENTIALS_ENC_KEY``, la clave Fernet que descifra las credenciales
de todas las empresas. Acá el toolchain y las dependencias del repo ya existen.

**No detecta nada por sí mismo: invoca y normaliza.** El valor que agrega es una interfaz y una
salida homogéneas sobre tres backends; la inteligencia está en cada detector. Y la parte cara —la
allowlist de convenciones del framework (que FastAPI inyecta por ``Depends``, que SQLAlchemy
dispara por ``@event.listens_for``, que Alembic llama ``upgrade``/``downgrade`` por convención)—
es **de cada repo**: este cliente usa la config del repo analizado, jamás una propia. Un repo sin
calibrar da ruido, y el cliente lo dice en vez de fingir que el resultado es limpio.

**Opera sobre el working tree local**, no sobre el clon del volumen del gateway ni sobre el índice:
el veredicto es sobre el código real y no sobre el último HEAD que ingestó el poller. En el modo
**multi-repo** (``--workspace``) eso mismo decide de dónde salen los repos: de los **clones que ya
están en la máquina**, no del registry ni de un clon on-demand (ADR MEL-DEC-198). Un repo que no
está en el disco no aparece en el reporte: no se puede analizar lo que no se puede construir.

El resultado son **candidatos**, no sentencias: el veredicto es humano (MEL-DEC-197). Un símbolo
puede estar vivísimo sin que nadie lo invoque por nombre.

Contrato del comando (opciones, salida, códigos de salida, gotchas de cada backend): instructivo
**MEL-CLI-009** en el vault. La decisión de fondo —por qué detectores nativos y no un detector
propio sobre el code-graph— es el ADR **MEL-DEC-197**.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

# Marcadores de lenguaje: el archivo que declara el proyecto. Un repo políglota (focusyn,
# gz-procurement, surgicx-platform tienen Python *y* TS) matchea varios y se analiza por cada uno.
_MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg"),
    "go": ("go.mod",),
    "ts": ("package.json",),
}

# El marcador rara vez está sólo en la raíz: focusyn tiene el ``package.json`` en ``frontend/``, y
# lo mismo pasa en gz-procurement y surgicx-platform —los tres políglotas, justo los que más TS
# tienen—. Buscar sólo en la raíz devolvía ``["python"]`` y el frontend entero no se analizaba.
_MAX_DEPTH = 2

# Dirs que nunca contienen el proyecto a analizar, sólo sus dependencias o su build. Descender ahí
# es caro (``node_modules`` tiene miles de ``package.json``) y produce hallazgos sobre código ajeno.
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "out",
        "target",
        "__pycache__",
        "site-packages",
        ".venv",
        "venv",
    }
)

# Dónde busca knip su config. Sin ella corre igual (infiere los entrypoints de los plugins de
# Vite/Next/etc.), pero es el mismo trato que vulture: sin calibrar, el reporte trae ruido.
_KNIP_CONFIG_FILES = (
    "knip.json",
    "knip.jsonc",
    ".knip.json",
    ".knip.jsonc",
    "knip.ts",
    "knip.js",
    "knip.config.ts",
    "knip.config.js",
)

# Salida de vulture: ``path/to/f.py:12: unused function 'foo' (60% confidence)``
_VULTURE_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): unused (?P<kind>\w+) '(?P<symbol>[^']+)' "
    r"\((?P<confidence>\d+)% confidence\)$"
)

# Confianza que se asigna a los backends que NO emiten una escala (Go y TS). **No es una
# probabilidad**: es alta —no certeza— porque su análisis es estructural y real (alcanzabilidad por
# call-graph en Go, resolución de imports contra las deps instaladas en TS), pero sigue ciego a lo
# que no se ve en la estructura: reflection y ``go:linkname``, imports dinámicos por string,
# entrypoints que la config no declara. Vulture, en cambio, sí emite su propia escala (60/90/100).
_STRUCTURAL_CONFIDENCE = 90


class ToolchainMissing(RuntimeError):
    """El detector nativo del lenguaje no está disponible (o el repo no está listo para él)."""


@dataclass(frozen=True)
class Project:
    """Un proyecto dentro del repo: un lenguaje y **dónde** vive (``.`` = la raíz).

    El subpath no es cosmético: ``knip`` hay que correrlo en ``frontend/`` —donde está el
    ``package.json`` y el ``node_modules``—, no en la raíz del repo.
    """

    lang: str
    subpath: str

    def root(self, repo: Path) -> Path:
        """El directorio donde se invoca el detector."""
        return repo if self.subpath == "." else repo / self.subpath

    def label(self) -> str:
        """Cómo se nombra en los avisos: ``ts (frontend)``, o sólo ``python`` si está en la raíz."""
        return self.lang if self.subpath == "." else f"{self.lang} ({self.subpath})"


@dataclass(frozen=True)
class Finding:
    """Un candidato a dead code, normalizado — misma forma venga del backend que venga."""

    lang: str
    path: str  # relativo a la RAÍZ del repo (no al subproyecto): el reporte es del repo entero
    line: int
    symbol: str
    kind: str  # function | class | method | import | export | type | dependency | file | …
    confidence: int  # 0-100


# Categorías accionables. El resto (``variable``, ``attribute``) son, en un repo con ORM y modelos
# Pydantic, casi todas COLUMNAS y CAMPOS: declaraciones que la librería lee por introspección, no
# código muerto. En focusyn son ~120 de 133 — diez hallazgos reales escondidos entre falsos es un
# reporte que nadie lee. El crudo sigue disponible con ``--all``.
#
# El filtro muerde sobre todo en Python: vulture es un heurístico y por eso confunde una columna con
# un símbolo muerto. Go y TS no tienen ese ruido —resuelven de verdad—, así que todo lo que emiten
# es accionable; sus kinds están acá para que el filtro no los descarte en silencio.
_ACTIONABLE_KINDS = frozenset(
    {
        # Python (vulture)
        "function",
        "class",
        "method",
        "import",
        "property",
        # TS (knip)
        "export",
        "type",
        "member",
        "dependency",
        "file",
    }
)


def is_actionable(f: Finding) -> bool:
    """``True`` si el hallazgo merece los ojos de un humano (o si el detector está 100% seguro)."""
    return f.kind in _ACTIONABLE_KINDS or f.confidence == 100


def _langs_at(directory: Path) -> list[str]:
    """Lenguajes cuyo archivo-marcador está en ``directory`` (orden estable)."""
    return [
        lang for lang, markers in _MARKERS.items() if any((directory / m).exists() for m in markers)
    ]


def _candidate_dirs(repo: Path) -> Iterator[Path]:
    """``repo`` y sus subdirectorios hasta :data:`_MAX_DEPTH`, en profundidad creciente (BFS).

    El orden importa: garantiza que un proyecto de la raíz se vea **antes** que cualquiera de sus
    descendientes, que es lo que permite podarlos (ver :func:`detect_projects`).
    """
    yield repo
    level = [repo]
    for _ in range(_MAX_DEPTH):
        children: list[Path] = []
        for parent in level:
            for child in sorted(parent.iterdir()):
                if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP_DIRS:
                    continue
                yield child
                children.append(child)
        level = children


def detect_projects(repo: Path) -> list[Project]:
    """Proyectos analizables en ``repo``: ``(lenguaje, subpath)`` por cada marcador encontrado.

    Busca hasta :data:`_MAX_DEPTH` niveles abajo, porque el marcador rara vez está en la raíz de un
    monorepo. **Un proyecto poda a sus descendientes del mismo lenguaje**: si el repo declara TS en
    la raíz, ese ``package.json`` gobierna sus workspaces y descender duplicaría hallazgos — el
    detector nativo ya sabe recorrer lo suyo (``workspaces`` de npm, ``./...`` de Go, los ``paths``
    de vulture). Dos subproyectos hermanos del mismo lenguaje (``services/a``, ``services/b``) sí
    salen los dos.
    """
    projects: list[Project] = []
    claimed: dict[str, list[Path]] = {}
    for directory in _candidate_dirs(repo):
        for lang in _langs_at(directory):
            # Como el recorrido es por profundidad creciente, un ancestro del mismo lenguaje ya
            # está en `claimed` cuando llegamos acá.
            if any(owner in directory.parents for owner in claimed.get(lang, ())):
                continue
            claimed.setdefault(lang, []).append(directory)
            subpath = "." if directory == repo else directory.relative_to(repo).as_posix()
            projects.append(Project(lang=lang, subpath=subpath))
    order = list(_MARKERS)
    projects.sort(key=lambda p: (order.index(p.lang), p.subpath))
    return projects


def is_calibrated(root: Path, lang: str) -> bool:
    """``True`` si el proyecto trae la config del detector — sin ella el reporte es ruido.

    No es un detalle cosmético: la calibración (los ``ignore_decorators`` de los entrypoints del
    framework, los ``exclude`` del código generado, los ``entry`` de knip) es lo que separa 10
    hallazgos reales de 108 falsos.

    **Go es la excepción y devuelve siempre ``True``**: ``deadcode`` no tiene archivo de config
    porque no hay allowlist que escribir — calcula alcanzabilidad real desde ``main``, no adivina
    por convenciones. Lo que igual puede mentirle (reflection, ``go:linkname``) no se arregla con
    una config, así que avisar "calibrá esto" sería mandar al usuario a una tarea inexistente.
    """
    if lang == "python":
        pyproject = root / "pyproject.toml"
        return pyproject.exists() and "[tool.vulture]" in pyproject.read_text(encoding="utf-8")
    if lang == "ts":
        if any((root / name).exists() for name in _KNIP_CONFIG_FILES):
            return True
        package = root / "package.json"
        if not package.exists():
            return False
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return isinstance(manifest, dict) and "knip" in manifest
    return lang == "go"


def parse_vulture_line(line: str) -> Finding | None:
    """Una línea de vulture → ``Finding``. ``None`` si no es un hallazgo (ruido del runner)."""
    m = _VULTURE_RE.match(line.strip())
    if m is None:
        return None
    return Finding(
        lang="python",
        path=m["path"],
        line=int(m["line"]),
        symbol=m["symbol"],
        kind=m["kind"],
        confidence=int(m["confidence"]),
    )


# Sólo para el repo SIN calibrar. Ahí el path que recibe vulture lo elegimos NOSOTROS (``.``), y
# ``.`` incluye el virtualenv: en ``melquiades-mind`` eso eran **9.971 de 10.097 hallazgos dentro de
# ``.venv/``** —dead code de librerías de terceros, sobre el que el repo no puede hacer nada—, y en
# ``melquiades-embeddings`` un ``SyntaxWarning`` de una dependencia hacía salir a vulture con código
# 1 y el repo entero quedaba **sin revisar**. Excluirlos no es ponerle una config nuestra al repo:
# es no analizar código que no es suyo — el mismo principio que ya gobierna :data:`_SKIP_DIRS` en
# la detección. Si el repo SÍ trae ``[tool.vulture]``, no le pasamos nada: manda su config, siempre.
_VULTURE_EXCLUDE = ",".join(
    pattern for d in sorted(_SKIP_DIRS) for pattern in (f"{d}/*", f"*/{d}/*")
)


def run_python(root: Path) -> list[Finding]:
    """Corre ``vulture`` sobre ``root`` con **la config del propio repo** (``[tool.vulture]``).

    Vulture es el único de los tres backends que no necesita construir el proyecto: es puro AST
    sobre los ``.py``. Por eso —y sólo por eso— este sería el único que podría correr en el
    gateway; no lo hacemos, para no partir la superficie en dos.
    """
    if shutil.which("vulture") is None:
        raise ToolchainMissing("vulture no está instalado (pip install vulture)")
    # cwd=root ⇒ vulture lee el [tool.vulture] del repo analizado. Sin config, hay que darle un
    # path (y decirle qué NO es del repo: ver _VULTURE_EXCLUDE) o no analiza nada.
    args = (
        ["vulture"]
        if is_calibrated(root, "python")
        else ["vulture", ".", "--exclude", _VULTURE_EXCLUDE]
    )
    proc = subprocess.run(  # noqa: S603 - argv fijo, sin shell
        args, cwd=root, capture_output=True, text=True, check=False
    )
    # vulture sale 3 cuando encuentra código muerto, 0 cuando no; ambos son éxito para nosotros.
    if proc.returncode not in (0, 3):
        raise ToolchainMissing(f"vulture falló ({proc.returncode}): {proc.stderr.strip()[:200]}")
    return [f for line in proc.stdout.splitlines() if (f := parse_vulture_line(line)) is not None]


# El paquete `deadcode` de golang.org/x/tools, resuelto por el propio toolchain del repo. `go run`
# lo compila una vez y lo cachea; la primera corrida en una máquina nueva necesita red.
_GO_DEADCODE = "golang.org/x/tools/cmd/deadcode@latest"


def parse_go_report(stdout: str, root: Path) -> list[Finding]:
    """Salida ``-json`` de ``deadcode`` → ``Finding``s (paths relativos al proyecto).

    El JSON es una lista de paquetes, cada uno con las funciones inalcanzables desde ``main``.
    Las marcadas como ``Generated`` se descartan: son código generado (protobuf, mocks), y
    "borrarlas" significaría editar un generador, no el repo.

    **Un repo sin dead code imprime ``null``, no ``[]``** — y confundir eso con un error hacía que
    el caso bueno (repo limpio) se reportara como "no se pudo analizar".
    """
    packages = json.loads(stdout) or []
    findings: list[Finding] = []
    for package in packages:
        for func in package.get("Funcs", ()):
            if func.get("Generated"):
                continue
            position = func.get("Position", {})
            file = position.get("File", "")
            findings.append(
                Finding(
                    lang="go",
                    path=_relativize(file, root),
                    line=int(position.get("Line", 0)),
                    symbol=func.get("Name", "?"),
                    kind="function",
                    confidence=_STRUCTURAL_CONFIDENCE,
                )
            )
    return findings


def run_go(root: Path) -> list[Finding]:
    """Corre ``deadcode`` (golang.org/x/tools) sobre ``root``.

    A diferencia de vulture, esto **construye el paquete**: el análisis es de alcanzabilidad sobre
    el call-graph real (RTA) desde ``main``, no un heurístico sobre el AST. Por eso es mucho más
    preciso —y por eso necesita el toolchain y las deps del repo, que es exactamente lo que no
    queremos dentro del gateway.
    """
    if shutil.which("go") is None:
        raise ToolchainMissing(
            "el toolchain de Go no está instalado (no encuentro `go` en el PATH)"
        )
    proc = subprocess.run(  # noqa: S603 - argv fijo, sin shell
        ["go", "run", _GO_DEADCODE, "-json", "./..."],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # No compila, faltan deps (`go mod download`), o no hay red para bajar el detector. Nada
        # de eso lo arregla este CLI: se avisa y se sigue con las demás familias.
        raise ToolchainMissing(f"deadcode falló ({proc.returncode}): {proc.stderr.strip()[:200]}")
    try:
        return parse_go_report(proc.stdout, root)
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise ToolchainMissing(f"no pude leer la salida de deadcode: {exc}") from exc


# Qué tipos de issue de knip son dead code, y a qué `kind` normalizado corresponden.
#
# Los que NO están acá se descartan a propósito: `unlisted`/`unresolved`/`binaries` son deps que el
# código USA sin declararlas (el problema inverso al que buscamos), y `duplicates` es el mismo
# símbolo exportado dos veces —un smell, no un muerto—, que en un frontend React es puro ruido
# (`export function Foo` + `export default Foo` en cada componente).
_KNIP_KINDS: dict[str, str] = {
    "files": "file",
    "dependencies": "dependency",
    "devDependencies": "dependency",
    "exports": "export",
    "types": "type",
    "classMembers": "member",
    "enumMembers": "member",
    "namespaceMembers": "member",
}


def parse_knip_report(stdout: str) -> list[Finding]:
    """Salida ``--reporter json`` de knip → ``Finding``s (paths relativos al proyecto).

    knip agrupa por archivo y mete cada tipo de issue en su propia lista. Un archivo entero sin
    usar (``files``) viene como string pelado, sin línea: se reporta en la línea 1.
    """
    report = json.loads(stdout)
    findings: list[Finding] = []
    for issue in report.get("issues", ()):
        file = issue.get("file", "")
        for issue_type, kind in _KNIP_KINDS.items():
            for entry in issue.get(issue_type, ()):
                # `files` son strings; el resto, objetos {name, line, col, pos}.
                if isinstance(entry, str):
                    symbol, line = Path(entry).name, 1
                    file_path = entry
                else:
                    symbol, line = entry.get("name", "?"), int(entry.get("line", 1) or 1)
                    file_path = file
                findings.append(
                    Finding(
                        lang="ts",
                        path=file_path,
                        line=line,
                        symbol=symbol,
                        kind=kind,
                        confidence=_STRUCTURAL_CONFIDENCE,
                    )
                )
    return findings


def run_ts(root: Path) -> list[Finding]:
    """Corre ``knip`` sobre ``root`` — el directorio del ``package.json``, no la raíz del repo.

    knip **necesita ``node_modules``**: no adivina, resuelve cada import contra las dependencias
    realmente instaladas. Sin ellas no da un reporte peor: da uno *vacío o mentiroso*, que es la
    forma más cara de equivocarse (un reporte limpio que no analizó nada). Por eso se chequea antes
    de invocarlo, y si falta se avisa en vez de correr igual. Este cliente **no instala nada**:
    ``npm install`` ejecuta los ``postinstall`` del repo, y decidir eso es del usuario, no nuestro.
    """
    if not (root / "node_modules").is_dir():
        raise ToolchainMissing(
            "faltan las dependencias instaladas (no hay node_modules) — knip resuelve los imports "
            "contra ellas; corré `npm install` en ese directorio y repetí"
        )
    local = root / "node_modules" / ".bin" / "knip"
    if local.exists():
        args = [str(local)]
    elif shutil.which("npx") is not None:
        # El repo no lo trae en devDependencies: npx lo baja (y lo cachea). Es el modo del diseño.
        args = ["npx", "--yes", "knip"]
    else:
        raise ToolchainMissing("no encuentro `knip` ni `npx` (¿está Node instalado?)")
    proc = subprocess.run(  # noqa: S603 - argv fijo, sin shell
        [*args, "--reporter", "json"], cwd=root, capture_output=True, text=True, check=False
    )
    # knip sale 1 cuando ENCUENTRA issues (y también si crashea) ⇒ el returncode no distingue. La
    # señal de éxito real es que la salida sea el JSON que pedimos.
    try:
        return parse_knip_report(proc.stdout)
    except (json.JSONDecodeError, TypeError, AttributeError):
        detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
        raise ToolchainMissing(f"knip falló ({proc.returncode}): {detail}") from None


_BACKENDS = {
    "python": run_python,
    "go": run_go,
    "ts": run_ts,
}


def _relativize(file: str, root: Path) -> str:
    """Path de un backend → relativo a ``root``. Los detectores mezclan absolutos y relativos."""
    path = Path(file)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _rebase(finding: Finding, subpath: str) -> Finding:
    """Re-basa un hallazgo del subproyecto al repo: ``src/App.tsx`` → ``frontend/src/App.tsx``.

    Cada detector reporta relativo a **su** directorio; el reporte es del **repo**. Sin esto, un
    ``path:línea`` del frontend no abre en el editor desde la raíz.
    """
    if subpath == ".":
        return finding
    return replace(finding, path=f"{subpath}/{finding.path}")


@dataclass(frozen=True)
class ScanResult:
    """Qué se encontró, qué se avisó y —crucialmente— **qué se llegó a analizar**.

    ``analyzed`` y ``skipped`` no son estadística: son lo que impide la mentira más cara de esta
    herramienta. Sin ellos, un proyecto TS sin ``node_modules`` producía cero hallazgos y el CLI
    los reportaba como "sin candidatos" —un repo entero sin revisar presentado como un repo
    limpio—. Un reporte vacío y uno no-ejecutado se ven idénticos si no se distinguen a propósito.
    """

    findings: list[Finding]
    warnings: list[str]
    analyzed: list[Project]
    skipped: list[Project]


def scan(repo: Path, projects: list[Project], *, all_kinds: bool = False) -> ScanResult:
    """Analiza cada proyecto con su detector nativo.

    Por defecto filtra a lo **accionable** (:func:`is_actionable`); ``all_kinds=True`` devuelve el
    crudo, con las variables y atributos que en un repo con ORM son casi todos falsos positivos.

    **Fail-soft**: un toolchain que falta o un proyecto que no está listo (sin ``node_modules``, sin
    ``go mod download``) produce un aviso, cae en ``skipped`` y NO aborta las demás familias — un
    reporte a medias en silencio es peor que uno incompleto y honesto.
    """
    findings: list[Finding] = []
    warnings: list[str] = []
    analyzed: list[Project] = []
    skipped: list[Project] = []
    for project in projects:
        backend = _BACKENDS.get(project.lang)
        if backend is None:
            warnings.append(f"{project.label()}: lenguaje sin backend — se omite")
            skipped.append(project)
            continue
        root = project.root(repo)
        try:
            found = backend(root)
        except ToolchainMissing as exc:
            warnings.append(f"{project.label()}: {exc}")
            skipped.append(project)
            continue
        analyzed.append(project)
        if not is_calibrated(root, project.lang):
            warnings.append(
                f"{project.label()}: el proyecto NO trae config del detector → el reporte incluye "
                f"falsos positivos estructurales (entrypoints del framework, código generado). "
                f"Calibrar antes de creerle."
            )
        kept = found if all_kinds else [f for f in found if is_actionable(f)]
        findings.extend(_rebase(f, project.subpath) for f in kept)
    findings.sort(key=lambda f: (f.lang, f.path, f.line))
    return ScanResult(findings=findings, warnings=warnings, analyzed=analyzed, skipped=skipped)


def find_repos(workspace: Path) -> list[Path]:
    """Repos git que son hijos **directos** de ``workspace`` (orden estable).

    De acá —y de ningún otro lado— salen los repos del modo multi-repo: **clones locales de la
    máquina que corre el comando**. Ni el volumen del gateway (construir un repo ajeno junto a
    ``CREDENTIALS_ENC_KEY`` es justo lo que prohíbe MEL-DEC-197) ni un clon on-demand (necesitaría
    un PAT descifrado, y un checkout recién bajado no se puede analizar sin instalarle las deps —
    ejecutar sus ``postinstall``— que este cliente nunca instala). Ver ADR MEL-DEC-198.

    Un repo que no está en el disco no se puede analizar, y por lo tanto **no se reporta**: no es
    un repo limpio, es un repo ausente.
    """
    if not workspace.is_dir():
        return []
    return sorted(
        child
        for child in workspace.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / ".git").exists()
    )


def select_projects(repo: Path, lang: str = "auto", *, force: bool = False) -> list[Project]:
    """Proyectos a analizar en ``repo``, aplicando el filtro de ``--lang``.

    ``force`` sólo tiene sentido cuando el usuario **nombró** el repo: pedir ``--lang python`` en un
    repo sin ``pyproject.toml`` es una orden (correr vulture en la raíz igual), pero forzarlo sobre
    los N repos que barrió un ``--workspace`` sería inventar proyectos donde no los hay.
    """
    projects = detect_projects(repo)
    if lang == "auto":
        return projects
    picked = [p for p in projects if p.lang == lang]
    if picked or not force:
        return picked
    return [Project(lang=lang, subpath=".")]


# Un repo sin marcador de lenguaje no es un fallo: hay repos de infra que son puro Dockerfile y
# YAML. Pero tampoco es "limpio" — no se analizó nada —, así que se reporta con su motivo.
_NO_PROJECT = (
    f"sin proyecto analizable (no hay pyproject.toml / go.mod / package.json hasta {_MAX_DEPTH} "
    "niveles abajo)"
)


@dataclass(frozen=True)
class RepoReport:
    """Lo que se pudo —y lo que NO se pudo— analizar de **un** repo del reporte."""

    name: str  # el nombre del directorio: es como el humano llama al repo
    path: str  # absoluto: el reporte de varios repos tiene que decir a cuál hay que ir
    result: ScanResult

    def reviewed(self) -> bool:
        """``True`` si se analizó al menos un proyecto. Un repo no revisado NO es un repo limpio."""
        return bool(self.result.analyzed)


def scan_repos(
    repos: list[Path],
    *,
    lang: str = "auto",
    all_kinds: bool = False,
    force_lang: bool = False,
) -> list[RepoReport]:
    """Analiza cada repo por separado. Un repo que falla no arrastra a los demás.

    El fail-soft de :func:`scan` (un proyecto que no se pudo analizar cae en ``skipped``) sube acá
    un nivel: en un reporte del ecosistema, la pregunta *"¿qué repos quedaron sin revisar?"* es tan
    importante como *"¿qué candidatos hay?"* — un repo entero omitido en silencio se lee como uno
    limpio, que es la forma más cara de equivocarse.
    """
    reports: list[RepoReport] = []
    for repo in repos:
        projects = select_projects(repo, lang, force=force_lang)
        result = (
            scan(repo, projects, all_kinds=all_kinds)
            if projects
            else ScanResult(findings=[], warnings=[_NO_PROJECT], analyzed=[], skipped=[])
        )
        reports.append(RepoReport(name=repo.name, path=str(repo.resolve()), result=result))
    return reports


def to_json(reports: list[RepoReport], out: Path) -> None:
    """Vuelca el reporte a ``out`` (JSON), para consumo programático.

    La forma es siempre la misma —un repo o veinte— y **cada repo lleva su ``analyzed``/``skipped``,
    no sólo sus hallazgos**. Una lista pelada de hallazgos (la forma anterior) hacía indistinguibles
    los dos casos que este comando existe para separar: un repo revisado y limpio, y uno que nunca
    se llegó a analizar. Ambos son ``findings: []``; sólo el primero es una buena noticia.
    """
    payload = {
        "repos": [
            {
                "repo": r.name,
                "path": r.path,
                "analyzed": [p.label() for p in r.result.analyzed],
                "skipped": [p.label() for p in r.result.skipped],
                "findings": [asdict(f) for f in r.result.findings],
            }
            for r in reports
        ]
    }
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
