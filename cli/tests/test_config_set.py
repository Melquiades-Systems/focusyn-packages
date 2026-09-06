"""`focusyn vault config-set` / `focusyn config set` — la edición puntual de la config (G3.6).

El invariante que estos tests protegen es uno solo y es destructivo si se rompe: el `PATCH` de
config **reemplaza el override entero**, así que editar una clave exige leer el override, mutarlo y
mandarlo completo. Un comando que mandara sólo la clave tocada borraría en silencio todo lo demás
que el vault tuviera configurado.
"""

from __future__ import annotations

import pytest

from focusyn_cli.cmd_admin import _coerce, _editar_override, _set_path, _unset_path
from focusyn_cli.http import CliError


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("null", None),
        ("7", 7),
        ("0.05", 0.05),
        ("-3", -3),
        ("qwen-vl", "qwen-vl"),
        ("", ""),
    ],
)
def test_coerce_tipa_el_valor(texto: str, esperado: object) -> None:
    """La shell entrega texto y el gateway valida con pydantic: `"true"` en un bool da 422."""
    assert _coerce(texto) == esperado


def test_coerce_no_confunde_booleano_con_texto() -> None:
    """`true` es bool, pero `truena` es texto: la comparación es exacta, no un prefijo."""
    assert _coerce("truena") == "truena"


def test_set_path_crea_el_submodelo() -> None:
    raiz: dict = {}
    _set_path(raiz, "groundedness.sample_rate", 0.1)
    assert raiz == {"groundedness": {"sample_rate": 0.1}}


def test_set_path_no_pisa_lo_que_ya_estaba() -> None:
    raiz: dict = {"groundedness": {"enabled": True}}
    _set_path(raiz, "groundedness.sample_rate", 0.1)
    assert raiz == {"groundedness": {"enabled": True, "sample_rate": 0.1}}


def test_unset_poda_el_submodelo_vacio() -> None:
    """Un `{"groundedness": {}}` es no-op para el gateway pero el panel lo muestra como tocado."""
    raiz: dict = {"groundedness": {"enabled": True}}
    _unset_path(raiz, "groundedness.enabled")
    assert raiz == {}


def test_unset_conserva_los_hermanos() -> None:
    raiz: dict = {"groundedness": {"enabled": True, "sample_rate": 0.1}}
    _unset_path(raiz, "groundedness.enabled")
    assert raiz == {"groundedness": {"sample_rate": 0.1}}


def test_unset_de_algo_que_no_esta_es_no_op() -> None:
    raiz: dict = {"retrieval": {"top_k_vector": 5}}
    _unset_path(raiz, "groundedness.enabled")
    assert raiz == {"retrieval": {"top_k_vector": 5}}


def test_editar_conserva_el_resto_del_override() -> None:
    """EL invariante: el PATCH reemplaza, así que lo no tocado tiene que viajar igual."""
    actual = {"retrieval": {"top_k_vector": 50}, "reranker": {"model": "bge-m3"}}
    nuevo = _editar_override(
        actual, "groundedness.sample_rate", "0.1", unset=False, como_json=False
    )
    assert nuevo == {
        "retrieval": {"top_k_vector": 50},
        "reranker": {"model": "bge-m3"},
        "groundedness": {"sample_rate": 0.1},
    }
    assert actual == {"retrieval": {"top_k_vector": 50}, "reranker": {"model": "bge-m3"}}, (
        "el override recibido no se muta: el caller puede querer reportarlo"
    )


def test_editar_sobre_override_vacio() -> None:
    assert _editar_override(None, "groundedness.enabled", "true", unset=False, como_json=False) == {
        "groundedness": {"enabled": True}
    }


def test_editar_json_para_listas() -> None:
    """Una lista se escribe con --json: adivinarla desde comas rompe cualquier valor con coma."""
    nuevo = _editar_override(
        None, "retrieval.rrf_weights", "[0.5, 0.3]", unset=False, como_json=True
    )
    assert nuevo == {"retrieval": {"rrf_weights": [0.5, 0.3]}}


def test_valor_faltante_es_error_util() -> None:
    with pytest.raises(CliError, match="--unset"):
        _editar_override(None, "groundedness.enabled", None, unset=False, como_json=False)
