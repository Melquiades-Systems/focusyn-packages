"""Capa HTTP compartida de los comandos NUEVOS (init/doctor/version/scaffold y los wrappers F2).

``memory_sync``/``attachment_upload`` conservan su propio cliente httpx (movidos y ya testeados);
esto es para todo lo demás. Traduce los errores de httpx a ``CliError`` con un mensaje humano y un
exit code, para que los comandos no repitan el mismo bloque try/except.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import httpx

from focusyn_cli import __version__
from focusyn_cli.config import Credential, validate_gateway_url
from focusyn_cli.errors import CliError

# Timeout generoso: el gateway puede tardar (clasificación LLM, commit+push, embeddings).
_DEFAULT_TIMEOUT = 120.0


def _explain_status(exc: httpx.HTTPStatusError) -> CliError:
    """Un status de error del gateway → mensaje accionable (no un volcado de httpx)."""
    status = exc.response.status_code
    body = exc.response.text[:300]
    if status == 401:
        return CliError(
            "401 no autenticado: la credencial falta o no es válida. Revisá `focusyn doctor` "
            "o volvé a correr `focusyn init`.",
            code=1,
        )
    if status == 403:
        return CliError(
            "403 sin permiso: tu credencial no tiene el scope que este endpoint exige. "
            "Pedile al owner una key/usuario con el scope correcto (`focusyn doctor` los muestra).",
            code=1,
        )
    if status == 421:
        return CliError(
            "421 Host no permitido: el gateway rechaza el Host de la URL. Verificá que la "
            "--gateway-url sea exactamente la pública (MCP_ALLOWED_HOSTS del server).",
            code=1,
        )
    return CliError(f"HTTP {status}: {body}", code=1)


class GatewayClient:
    """Cliente httpx tipado contra el gateway. Context manager. Nunca loguea la credencial."""

    def __init__(
        self,
        credential: Credential,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        refresher: Callable[[], str | None] | None = None,
    ) -> None:
        self._cred = credential
        # Defensa en profundidad: por acá pasa TODO el tráfico autenticado. `resolve()` ya valida,
        # pero una URL que llegó por otro camino (config a mano, un `claude mcp get`) muere acá.
        validate_gateway_url(credential.gateway_url)
        # ``refresher`` (login JWT): ante un 401 devuelve un access token fresco (o None). Renueva
        # el Bearer expirado sin que el usuario re-loguee. Con API key es None.
        self._refresher = refresher
        self._client = httpx.Client(
            base_url=credential.gateway_url.rstrip("/"),
            timeout=timeout,
            headers=credential.auth_header(),
            transport=transport,
        )

    def _maybe_refresh(self) -> bool:
        """Ante un 401 con refresher: renueva el Bearer y actualiza el header. True si renovó."""
        if self._refresher is None:
            return False
        token = self._refresher()
        if not token:
            return False
        self._client.headers["Authorization"] = f"Bearer {token}"
        return True

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GatewayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- primitivas ---------------------------------------------------------- #

    def get(self, path: str, /, **params: Any) -> Any:
        # `path` es posicional-only: así un query param llamado `path` (ej. /v1/read?path=…) no
        # colisiona con el argumento de la URL.
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.get(path, params=clean)
            if resp.status_code == 401 and self._maybe_refresh():
                resp = self._client.get(path, params=clean)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _explain_status(exc) from exc
        except httpx.HTTPError as exc:
            raise CliError(f"No se pudo conectar a {self._cred.gateway_url}: {exc}") from exc
        return resp.json()

    def post(self, path: str, json: Any = None) -> Any:
        return self._send("POST", path, json=json)

    def put(self, path: str, json: Any = None) -> Any:
        return self._send("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self._send("DELETE", path)

    def _send(self, method: str, path: str, json: Any = None) -> Any:
        try:
            resp = self._client.request(method, path, json=json)
            if resp.status_code == 401 and self._maybe_refresh():
                resp = self._client.request(method, path, json=json)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _explain_status(exc) from exc
        except httpx.HTTPError as exc:
            raise CliError(f"No se pudo conectar a {self._cred.gateway_url}: {exc}") from exc
        return resp.json() if resp.content else None

    # -- endpoints públicos / de diagnóstico -------------------------------- #

    def capabilities(self) -> dict[str, Any]:
        """``GET /v1/capabilities`` — público, sin auth. Base del check de URL y de versión."""
        return cast("dict[str, Any]", self.get("/v1/capabilities"))

    def whoami(self) -> dict[str, Any]:
        """``GET /v1/auth/whoami`` — principal + scopes efectivos (Fase 3). Requiere auth."""
        return cast("dict[str, Any]", self.get("/v1/auth/whoami"))


def check_url(
    credential: Credential, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Valida que la URL sea un gateway focusyn (público, ANTES de pedir credencial).

    Distingue 'URL mal escrita' de 'credencial mala' — el fallo silencioso que motivó todo esto.
    Devuelve el dict de capabilities; eleva ``CliError`` si la URL no responde como un gateway.
    (``transport`` sólo se usa en tests.)
    """
    with GatewayClient(credential, transport=transport) as client:
        caps = client.capabilities()
    if "api_version" not in caps:
        raise CliError(
            f"{credential.gateway_url} respondió, pero no parece un gateway focusyn "
            "(sin 'api_version' en /v1/capabilities). ¿URL correcta?"
        )
    return caps


def _parse_version(v: str) -> tuple[int, ...]:
    """'1.28.0' → (1, 28, 0); tolera sufijos ('0.1.0-dev' → (0, 1, 0))."""
    core = v.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for part in core.split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out) or (0,)


def version_warning(caps: dict[str, Any]) -> str | None:
    """Aviso si el gateway declara un ``min_client_version`` mayor al del cliente. Nunca instala."""
    minimum = caps.get("min_client_version")
    if not minimum:
        return None
    if _parse_version(__version__) < _parse_version(str(minimum)):
        return (
            f"cliente {__version__} < mínimo {minimum} que exige el gateway. "
            "Actualizá: `uv tool upgrade focusyn-cli`."
        )
    return None


__all__ = ["CliError", "GatewayClient", "check_url", "version_warning"]
