"""Cliente de ``POST /v1/vaults/{name}/scaffold`` (instancia un template pack en un dossier).

Extraído del httpx crudo que vivía en el CLI de operador. Usa la capa ``http.py`` compartida →
manejo de errores unificado. Requiere una credencial con scope ``vault`` o ``apply``.
"""

from __future__ import annotations

from typing import Any, cast

from focusyn_cli.http import GatewayClient


def scaffold_vault(
    client: GatewayClient, name: str, *, template: str = "dossier", force: bool = False
) -> dict[str, Any]:
    """Instancia ``template`` en el vault ``name``. Devuelve el dict de respuesta del gateway.

    Escribe los archivos del pack DIRECTO al checkout en un commit y pushea (el poller los indexa
    sin reiniciar). Idempotente: si el vault ya tiene contenido responde 409 → usar ``force=True``.
    """
    return cast(
        "dict[str, Any]",
        client.post(f"/v1/vaults/{name}/scaffold", json={"template": template, "force": force}),
    )


__all__ = ["scaffold_vault"]
