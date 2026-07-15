"""Tests del cliente ``focusyn deadcode`` (detección en monorepo + los tres backends).

El cliente NO detecta dead code: invoca al detector nativo y normaliza. Lo que se testea acá es
justamente eso —el parseo, la detección de proyectos, el re-basado de paths, el fail-soft— no la
calidad de vulture, deadcode ni knip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from focusyn_cli import deadcode
from focusyn_cli.deadcode import (
    Finding,
    Project,
    detect_projects,
    is_calibrated,
    parse_go_report,
    parse_knip_report,
    parse_vulture_line,
    scan,
    to_json,
)

# --------------------------------------------------------------------------------------------
# Detección de proyectos (el gap de D0: el marcador rara vez está en la raíz)
# --------------------------------------------------------------------------------------------


def test_detecta_lenguajes_por_marcador_en_la_raiz(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_projects(tmp_path) == [
        Project(lang="python", subpath="."),
        Project(lang="go", subpath="."),
    ]


def test_monorepo_encuentra_el_marcador_en_un_subdirectorio(tmp_path: Path) -> None:
    """El gap que D1 vino a cerrar: en un repo políglota el ``package.json`` vive en ``frontend/``.

    Buscando sólo en la raíz, el CLI devolvía ``["python"]`` y los archivos ``.ts/.tsx`` del
    frontend NUNCA se analizaban.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")

    assert detect_projects(tmp_path) == [
        Project(lang="python", subpath="."),
        Project(lang="ts", subpath="frontend"),
    ]


def test_un_proyecto_de_la_raiz_poda_a_sus_descendientes(tmp_path: Path) -> None:
    """Si el repo declara TS en la raíz, ese ``package.json`` gobierna sus workspaces.

    Descender igual duplicaría hallazgos: el detector nativo ya recorre lo suyo.
    """
    (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
    (tmp_path / "packages" / "web").mkdir(parents=True)
    (tmp_path / "packages" / "web" / "package.json").write_text("{}")

    assert detect_projects(tmp_path) == [Project(lang="ts", subpath=".")]


def test_dos_subproyectos_hermanos_del_mismo_lenguaje_salen_los_dos(tmp_path: Path) -> None:
    """Sin marcador en la raíz no hay quién gobierne: cada módulo es su propio proyecto."""
    for name in ("a", "b"):
        (tmp_path / "services" / name).mkdir(parents=True)
        (tmp_path / "services" / name / "go.mod").write_text(f"module {name}\n")

    assert detect_projects(tmp_path) == [
        Project(lang="go", subpath="services/a"),
        Project(lang="go", subpath="services/b"),
    ]


def test_no_desciende_a_node_modules_ni_a_dirs_ocultos(tmp_path: Path) -> None:
    """``node_modules`` tiene miles de ``package.json``: son deps ajenas, no el proyecto."""
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "package.json").write_text("{}")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "pyproject.toml").write_text("[project]\nname='dep'\n")

    assert detect_projects(tmp_path) == []


def test_repo_sin_marcadores_no_detecta_nada(tmp_path: Path) -> None:
    assert detect_projects(tmp_path) == []


# --------------------------------------------------------------------------------------------
# Calibración (sin config del detector, el reporte es ruido — y hay que decirlo)
# --------------------------------------------------------------------------------------------


def test_python_calibrado_solo_si_el_repo_trae_config(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text("[project]\nname='x'\n")
    assert is_calibrated(tmp_path, "python") is False
    py.write_text("[project]\nname='x'\n\n[tool.vulture]\npaths = ['src']\n")
    assert is_calibrated(tmp_path, "python") is True


def test_ts_calibrado_por_knip_json_o_por_la_clave_del_package(tmp_path: Path) -> None:
    package = tmp_path / "package.json"
    package.write_text("{}")
    assert is_calibrated(tmp_path, "ts") is False

    package.write_text('{"knip": {"entry": ["src/main.tsx"]}}')
    assert is_calibrated(tmp_path, "ts") is True

    package.write_text("{}")
    (tmp_path / "knip.json").write_text("{}")
    assert is_calibrated(tmp_path, "ts") is True


def test_go_no_tiene_nada_que_calibrar(tmp_path: Path) -> None:
    """``deadcode`` no tiene archivo de config: calcula alcanzabilidad real desde ``main``.

    Avisar "calibrá esto" sería mandar al usuario a una tarea que no existe.
    """
    assert is_calibrated(tmp_path, "go") is True


# --------------------------------------------------------------------------------------------
# Parsers — un backend por familia, misma salida
# --------------------------------------------------------------------------------------------


def test_parse_vulture_line() -> None:
    line = "src/focusyn/deps.py:257: unused function 'get_general_session' (60% confidence)"
    assert parse_vulture_line(line) == Finding(
        lang="python",
        path="src/focusyn/deps.py",
        line=257,
        symbol="get_general_session",
        kind="function",
        confidence=60,
    )


def test_parse_vulture_line_ignora_ruido() -> None:
    """El runner escupe líneas que no son hallazgos (`Installed 1 package…`): no son findings."""
    assert parse_vulture_line("Installed 1 package in 6ms") is None
    assert parse_vulture_line("") is None


def test_parse_go_report(tmp_path: Path) -> None:
    stdout = json.dumps(
        [
            {
                "Name": "main",
                "Path": "example.com/x",
                "Funcs": [
                    {"Name": "Dead", "Position": {"File": "main.go", "Line": 9, "Col": 6}},
                ],
            }
        ]
    )
    assert parse_go_report(stdout, tmp_path) == [
        Finding(lang="go", path="main.go", line=9, symbol="Dead", kind="function", confidence=90)
    ]


def test_parse_go_report_sin_hallazgos_es_null_no_lista_vacia(tmp_path: Path) -> None:
    """``deadcode`` imprime ``null`` cuando no encuentra nada muerto.

    Tratar eso como error hacía que un repo Go **limpio** se reportara como "no se pudo analizar"
    (y con exit 1): el error opuesto al que la herramienta busca evitar.
    """
    assert parse_go_report("null", tmp_path) == []


def test_parse_go_report_descarta_codigo_generado(tmp_path: Path) -> None:
    """Borrar una función generada significa editar un generador, no el repo: no es accionable."""
    stdout = json.dumps(
        [
            {
                "Funcs": [
                    {
                        "Name": "GenDead",
                        "Position": {"File": "pb.go", "Line": 1},
                        "Generated": True,
                    },
                    {"Name": "Dead", "Position": {"File": "main.go", "Line": 2}},
                ]
            }
        ]
    )
    assert [f.symbol for f in parse_go_report(stdout, tmp_path)] == ["Dead"]


def test_parse_go_report_relativiza_paths_absolutos(tmp_path: Path) -> None:
    stdout = json.dumps(
        [{"Funcs": [{"Name": "Dead", "Position": {"File": str(tmp_path / "cmd" / "m.go")}}]}]
    )
    assert parse_go_report(stdout, tmp_path)[0].path == "cmd/m.go"


def test_parse_knip_report() -> None:
    stdout = json.dumps(
        {
            "issues": [
                {
                    "file": "src/lib/api/write.ts",
                    "exports": [{"name": "createVault", "line": 165, "col": 17}],
                    "types": [{"name": "LinkIn", "line": 18, "col": 13}],
                },
                {
                    "file": "package.json",
                    "devDependencies": [{"name": "prettier", "line": 47, "col": 6}],
                },
            ]
        }
    )
    assert parse_knip_report(stdout) == [
        Finding(
            lang="ts",
            path="src/lib/api/write.ts",
            line=165,
            symbol="createVault",
            kind="export",
            confidence=90,
        ),
        Finding(
            lang="ts",
            path="src/lib/api/write.ts",
            line=18,
            symbol="LinkIn",
            kind="type",
            confidence=90,
        ),
        Finding(
            lang="ts",
            path="package.json",
            line=47,
            symbol="prettier",
            kind="dependency",
            confidence=90,
        ),
    ]


def test_parse_knip_report_ignora_lo_que_no_es_dead_code() -> None:
    """``unlisted``/``unresolved`` son deps que el código USA sin declarar: el problema inverso.

    Y ``duplicates`` (el mismo símbolo exportado dos veces) es un smell, no un muerto — en un
    frontend React es puro ruido: cada componente hace `export function Foo` + `export default Foo`.
    """
    stdout = json.dumps(
        {
            "issues": [
                {
                    "file": "src/x.tsx",
                    "unlisted": [{"name": "highlight.js", "line": 10}],
                    "unresolved": [{"name": "./falta", "line": 3}],
                    "duplicates": [[{"name": "Foo", "line": 1}, {"name": "default", "line": 9}]],
                }
            ]
        }
    )
    assert parse_knip_report(stdout) == []


def test_parse_knip_report_archivo_entero_sin_usar() -> None:
    """Un ``file`` viene como string pelado, sin línea: se reporta en la 1."""
    stdout = json.dumps({"issues": [{"file": "x", "files": ["src/viejo.ts"]}]})
    assert parse_knip_report(stdout) == [
        Finding(
            lang="ts", path="src/viejo.ts", line=1, symbol="viejo.ts", kind="file", confidence=90
        )
    ]


# --------------------------------------------------------------------------------------------
# scan — re-basado de paths, filtro de ruido y fail-soft
# --------------------------------------------------------------------------------------------


def test_rebasa_los_paths_del_subproyecto_al_repo(tmp_path: Path) -> None:
    """Cada detector reporta relativo a SU directorio; el reporte es del repo entero.

    Sin esto, ``src/App.tsx`` no abre desde la raíz: el archivo real es ``frontend/src/App.tsx``.
    """
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text('{"knip": {}}')
    monkeypatched = [Finding("ts", "src/App.tsx", 3, "default", "export", 90)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(deadcode._BACKENDS, "ts", lambda root: monkeypatched)
        result = scan(tmp_path, [Project(lang="ts", subpath="frontend")])

    assert [f.path for f in result.findings] == ["frontend/src/App.tsx"]


def test_toolchain_ausente_avisa_sigue_y_queda_en_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: lo que no se pudo analizar se avisa y NO aborta las demás familias.

    Y —lo que importa— queda en ``skipped``: un proyecto sin analizar NO puede leerse como uno
    limpio (ver :class:`ScanResult`).
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n\n[tool.vulture]\npaths=['.']\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"knip": {}}')

    def sin_node_modules(root: Path) -> list[Finding]:
        raise deadcode.ToolchainMissing("faltan las dependencias instaladas (no hay node_modules)")

    monkeypatch.setitem(
        deadcode._BACKENDS,
        "python",
        lambda root: [Finding("python", "a.py", 1, "f", "function", 60)],
    )
    monkeypatch.setitem(deadcode._BACKENDS, "ts", sin_node_modules)

    result = scan(tmp_path, detect_projects(tmp_path))

    assert [f.symbol for f in result.findings] == ["f"]
    assert result.analyzed == [Project(lang="python", subpath=".")]
    assert result.skipped == [Project(lang="ts", subpath="web")]
    assert any("node_modules" in w for w in result.warnings)


def test_repo_sin_calibrar_avisa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin `[tool.vulture]` el reporte trae falsos positivos estructurales: hay que decirlo.

    Se parchea ``_BACKENDS`` (no ``run_python``): el dict ya capturó la función al importar, así
    que reemplazar el nombre del módulo no tendría efecto.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setitem(
        deadcode._BACKENDS,
        "python",
        lambda root: [Finding("python", "a.py", 1, "foo", "function", 60)],
    )
    result = scan(tmp_path, [Project(lang="python", subpath=".")])
    assert len(result.findings) == 1
    assert any("NO trae config" in w for w in result.warnings)


def test_filtra_el_ruido_de_ORM_por_defecto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una `variable` al 60% es, en un repo con ORM, casi siempre una columna: no es accionable.

    Pero una al 100% sí (vulture no se equivoca ahí), y `--all` muestra todo.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n\n[tool.vulture]\npaths=['.']\n")
    crudo = [
        Finding("python", "m.py", 1, "get_dead", "function", 60),  # accionable
        Finding("python", "m.py", 2, "applied_at", "variable", 60),  # columna ORM → ruido
        Finding("python", "m.py", 3, "as_of", "variable", 100),  # 100% → accionable igual
    ]
    monkeypatch.setitem(deadcode._BACKENDS, "python", lambda root: crudo)
    project = [Project(lang="python", subpath=".")]

    assert [f.symbol for f in scan(tmp_path, project).findings] == ["get_dead", "as_of"]

    todos = scan(tmp_path, project, all_kinds=True)
    assert [f.symbol for f in todos.findings] == ["get_dead", "applied_at", "as_of"]


def test_los_hallazgos_de_go_y_ts_no_los_filtra_el_ruido_de_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El filtro existe por el heurístico de vulture; knip y deadcode resuelven de verdad.

    Si sus kinds (``export``, ``type``, ``dependency``…) no fueran accionables, el filtro por
    defecto se los comería en silencio y el backend TS sería inútil.
    """
    (tmp_path / "go.mod").write_text("module x\n")
    monkeypatch.setitem(
        deadcode._BACKENDS,
        "go",
        lambda root: [Finding("go", "m.go", 1, "Dead", "function", 90)],
    )
    result = scan(tmp_path, [Project(lang="go", subpath=".")])
    assert [f.symbol for f in result.findings] == ["Dead"]

    ts_kinds = [
        Finding("ts", "a.ts", 1, "createVault", "export", 90),
        Finding("ts", "a.ts", 2, "LinkIn", "type", 90),
        Finding("ts", "package.json", 3, "prettier", "dependency", 90),
        Finding("ts", "viejo.ts", 1, "viejo.ts", "file", 90),
    ]
    assert all(deadcode.is_actionable(f) for f in ts_kinds)


def test_repo_sin_calibrar_no_analiza_las_dependencias_del_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin config, el path que recibe vulture lo elegimos NOSOTROS — y `.` incluye el virtualenv.

    Medido en un repo real, eso eran 9.971 de 10.097 "candidatos" dentro de ``.venv/`` (dead code de
    librerías de terceros), y en otro un ``SyntaxWarning`` de una dependencia tumbaba la corrida
    entera. Excluir esos directorios no es calibrar por el repo: es no analizar código que no es
    suyo.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")  # sin [tool.vulture]
    llamadas: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        llamadas.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/vulture")
    monkeypatch.setattr(subprocess, "run", fake_run)

    deadcode.run_python(tmp_path)

    (args,) = llamadas
    assert args[:2] == ["vulture", "."]
    excluidos = args[args.index("--exclude") + 1]
    for dependencia in (".venv", "node_modules", "site-packages"):
        assert f"*/{dependencia}/*" in excluidos


def test_repo_calibrado_manda_su_propia_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con `[tool.vulture]` no le pasamos NADA: paths y exclusiones son los del repo, siempre."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n\n[tool.vulture]\npaths=['src']\n"
    )
    llamadas: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        llamadas.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/vulture")
    monkeypatch.setattr(subprocess, "run", fake_run)

    deadcode.run_python(tmp_path)

    assert llamadas == [["vulture"]]


# --------------------------------------------------------------------------------------------
# Modo multi-repo (D2): de dónde salen los repos, y qué pasa con los que no se pudieron analizar
# --------------------------------------------------------------------------------------------


def _repo(workspace: Path, name: str, *, git: bool = True) -> Path:
    repo = workspace / name
    repo.mkdir()
    if git:
        (repo / ".git").mkdir()
    return repo


def test_find_repos_solo_toma_clones_git_hijos_directos(tmp_path: Path) -> None:
    """El origen de los repos es el disco: lo que está clonado, no lo que dice el registry."""
    _repo(tmp_path, "focusyn")
    _repo(tmp_path, "mind")
    _repo(tmp_path, "no-es-repo", git=False)  # un dir cualquiera del home
    (tmp_path / ".cache").mkdir()  # los ocultos no son repos de trabajo
    anidado = _repo(tmp_path, "padre")
    _repo(anidado, "hijo")  # sólo hijos DIRECTOS: no descendemos

    assert [r.name for r in deadcode.find_repos(tmp_path)] == ["focusyn", "mind", "padre"]


def test_find_repos_acepta_git_como_archivo(tmp_path: Path) -> None:
    """En un worktree (o un submódulo) ``.git`` es un ARCHIVO, no un directorio."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /otro/lado\n")

    assert [r.name for r in deadcode.find_repos(tmp_path)] == ["wt"]


def test_find_repos_workspace_inexistente_no_explota(tmp_path: Path) -> None:
    assert deadcode.find_repos(tmp_path / "no-existe") == []


def test_select_projects_no_inventa_proyectos_en_un_barrido(tmp_path: Path) -> None:
    """``--lang python`` sobre un repo NOMBRADO es una orden; sobre 40 repos barridos, invención."""
    (tmp_path / "go.mod").write_text("module x\n")

    assert deadcode.select_projects(tmp_path, "python") == []
    assert deadcode.select_projects(tmp_path, "python", force=True) == [
        Project(lang="python", subpath=".")
    ]
    assert deadcode.select_projects(tmp_path, "go") == [Project(lang="go", subpath=".")]


def test_scan_repos_un_repo_que_falla_no_arrastra_a_los_demas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El fail-soft sube un nivel: en el ecosistema, un repo roto no cancela el reporte."""
    bueno = _repo(tmp_path, "bueno")
    (bueno / "pyproject.toml").write_text("[project]\nname='x'\n\n[tool.vulture]\npaths=['.']\n")
    roto = _repo(tmp_path, "roto")
    (roto / "pyproject.toml").write_text("[project]\nname='y'\n")

    def por_repo(root: Path) -> list[Finding]:
        if root.name == "roto":
            raise deadcode.ToolchainMissing("vulture no está instalado")
        return [Finding("python", "a.py", 1, "muerto", "function", 60)]

    monkeypatch.setitem(deadcode._BACKENDS, "python", por_repo)

    reports = deadcode.scan_repos(deadcode.find_repos(tmp_path))

    assert [r.name for r in reports] == ["bueno", "roto"]
    assert [r.reviewed() for r in reports] == [True, False]
    assert [f.symbol for f in reports[0].result.findings] == ["muerto"]
    # Y el roto no queda como "limpio": no tiene hallazgos, pero tampoco está revisado.
    assert reports[1].result.findings == []
    assert reports[1].result.skipped == [Project(lang="python", subpath=".")]


def test_scan_repos_repo_sin_marcador_no_es_un_repo_limpio(tmp_path: Path) -> None:
    """Un repo de puro Dockerfile/YAML no se analizó — y eso NO es lo mismo que estar limpio."""
    _repo(tmp_path, "infra")

    (report,) = deadcode.scan_repos(deadcode.find_repos(tmp_path))

    assert report.reviewed() is False
    assert report.result.findings == []
    assert any("sin proyecto analizable" in w for w in report.result.warnings)


def test_to_json_distingue_revisado_y_limpio_de_no_revisado(tmp_path: Path) -> None:
    """La garantía central también en la salida de máquina: ambos son ``findings: []``.

    Un consumidor que sólo viera la lista de hallazgos (la forma anterior del JSON) no podría
    separar el repo limpio del repo que nunca se analizó — exactamente la mentira que el CLI
    existe para no decir.
    """
    limpio = deadcode.RepoReport(
        name="limpio",
        path="/w/limpio",
        result=deadcode.ScanResult(
            findings=[], warnings=[], analyzed=[Project(lang="python", subpath=".")], skipped=[]
        ),
    )
    sin_revisar = deadcode.RepoReport(
        name="sin-revisar",
        path="/w/sin-revisar",
        result=deadcode.ScanResult(
            findings=[],
            warnings=["ts: faltan las dependencias instaladas (no hay node_modules)"],
            analyzed=[],
            skipped=[Project(lang="ts", subpath="frontend")],
        ),
    )
    con_hallazgos = deadcode.RepoReport(
        name="sucio",
        path="/w/sucio",
        result=deadcode.ScanResult(
            findings=[Finding("python", "a.py", 3, "foo", "function", 60)],
            warnings=[],
            analyzed=[Project(lang="python", subpath=".")],
            skipped=[],
        ),
    )
    out = tmp_path / "r.json"
    to_json([limpio, sin_revisar, con_hallazgos], out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    por_nombre = {r["repo"]: r for r in payload["repos"]}

    assert por_nombre["limpio"]["findings"] == []
    assert por_nombre["limpio"]["analyzed"] == ["python"]  # ← revisado y limpio

    assert por_nombre["sin-revisar"]["findings"] == []
    assert por_nombre["sin-revisar"]["analyzed"] == []  # ← NO revisado: no es lo mismo
    assert por_nombre["sin-revisar"]["skipped"] == ["ts (frontend)"]

    assert por_nombre["sucio"]["findings"][0]["symbol"] == "foo"


# ------------------------------------------------------------------------------- pins (V3)
# `go run …@latest` y `npx --yes knip` sin versión ejecutan lo que el upstream publique HOY:
# un compromiso (o typosquat) sería RCE en la máquina del dev. Los backends remotos van PINEADOS.


def _pin_re() -> str:
    return r"@v?\d+\.\d+\.\d+$"


def test_go_deadcode_corre_pineado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    llamadas: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        llamadas.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="null", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(subprocess, "run", fake_run)

    deadcode.run_go(tmp_path)

    (args,) = llamadas
    assert args[:2] == ["go", "run"]
    modulo = args[2]
    assert modulo.startswith("golang.org/x/tools/cmd/deadcode@")
    assert "@latest" not in modulo
    assert re.search(_pin_re(), modulo), modulo  # versión concreta, p. ej. @v0.48.0


def test_npx_knip_corre_pineado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    (tmp_path / "node_modules").mkdir()  # knip exige deps instaladas; sin binario local
    llamadas: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        llamadas.append(args)
        return subprocess.CompletedProcess(args, 1, stdout='{"issues": []}', stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npx")
    monkeypatch.setattr(subprocess, "run", fake_run)

    deadcode.run_ts(tmp_path)

    (args,) = llamadas
    assert args[:2] == ["npx", "--yes"]
    paquete = args[2]
    assert paquete.startswith("knip@")
    assert "@latest" not in paquete
    assert re.search(_pin_re(), paquete), paquete  # versión concreta, p. ej. knip@6.26.0


def test_knip_local_del_repo_sigue_teniendo_prioridad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Si el repo trae knip en devDependencies, manda el SUYO (lo pinea su lockfile, no nosotros).
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "knip").write_text("#!/bin/sh\n")
    llamadas: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        llamadas.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='{"issues": []}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    deadcode.run_ts(tmp_path)

    (args,) = llamadas
    assert args[0] == str(bin_dir / "knip")
    assert not any(a.startswith("npx") for a in args)
