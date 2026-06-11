
import asyncio
import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright

import subprocess
import sys

def ensure_playwright_browsers():
    marker = Path.home() / ".cache" / "ms-playwright"
    if marker.exists() and any(marker.iterdir()):
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            timeout=180,
        )
    except Exception:
        pass

ensure_playwright_browsers()


try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

BASE_ORIGIN = "https://resultadosegundavuelta.onpe.gob.pe"
BASE = "/presentacion-backend"
FRONT_PATH = "/main/resumen"
ID_ELECCION = 10
SNAPSHOT_FILE = Path("snapshot_anterior_playwright_v5.json")
HISTORY_FILE = Path("historial_cambios_onpe.csv")


def fix_text(value: str) -> str:
    if not isinstance(value, str):
        return value
    try:
        return value.encode("latin1").decode("utf-8")
    except Exception:
        return value


def endpoint(path: str, **params) -> str:
    return f"{BASE}{path}?{urlencode(params)}"


def parse_onpe_number(value: Any, field_name: str = ""):
    """
    Convierte números de ONPE evitando confundir separadores de miles con decimales.

    Para cantidades de actas/votos:
    - 90,833 -> 90833
    - 90.833 -> 90833
    - 1,615 -> 1615
    - 1.615 -> 1615

    Para porcentajes:
    - 97.916 -> 97.916
    - 97,916 -> 97.916
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    field = str(field_name).lower()
    is_percent_field = "%" in raw or "porcentaje" in field or "porc" in field or field.endswith(" %")

    is_count_field = (
        field.endswith(" votos")
        or "actas_total" in field
        or "actas_contabilizadas" in field
        or "actas_pendientes" in field
        or "actas_envio_jee" in field
        or any(w in field for w in ["acta", "voto", "total", "contabil", "pendiente", "jee", "comput", "proces"])
    ) and not is_percent_field

    s = raw.replace("%", "").replace(" ", "").strip()

    if "," in s and "." in s:
        if is_percent_field:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", "")
    elif "," in s:
        if is_percent_field:
            s = s.replace(",", ".")
        elif is_count_field:
            s = s.replace(",", "")
        else:
            parts = s.split(",")
            s = s.replace(",", "") if len(parts[-1]) == 3 else s.replace(",", ".")
    elif "." in s:
        if is_percent_field:
            pass
        elif is_count_field:
            s = s.replace(".", "")
        else:
            parts = s.split(".")
            if len(parts) > 2 or len(parts[-1]) == 3:
                s = s.replace(".", "")

    try:
        return float(s)
    except Exception:
        return None


def format_number_for_display(value, decimals=0):
    if value is None or value == "":
        return ""
    try:
        n = parse_onpe_number(value, "porcentaje" if decimals else "cantidad")
        if n is None:
            n = float(value)
    except Exception:
        return value

    if decimals == 0:
        return f"{n:,.0f}"
    return f"{n:,.{decimals}f}"


def parse_acta_entera(value):
    """
    Las actas siempre son cantidades enteras.
    Si el valor parece porcentaje/decimal, no se acepta como acta.
    """
    n = parse_onpe_number(value, "actas")
    if n is None:
        return None
    try:
        if abs(float(n) - round(float(n))) > 0.000001:
            return None
        return int(round(float(n)))
    except Exception:
        return None


def parse_voto_entero(value):
    """
    Los votos también se muestran como cantidades enteras.
    """
    n = parse_onpe_number(value, "votos")
    if n is None:
        return None
    try:
        return int(round(float(n)))
    except Exception:
        return None



def flatten_numbers(obj: Any, prefix: str = "") -> Dict[str, float]:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("__"):
                continue
            np = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten_numbers(v, np))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten_numbers(v, f"{prefix}[{i}]"))
    else:
        parsed = parse_onpe_number(obj, prefix)
        if parsed is not None:
            out[prefix] = parsed
    return out


def campo_legible(campo: str) -> str:
    return (
        str(campo)
        .replace("resumen.", "")
        .replace("totales.", "")
        .replace("participantes.", "")
        .replace("data.", "")
        .replace("_", " ")
    )


def is_noise_field(campo: str) -> bool:
    """
    Campos que NO deben contarse como variación electoral.
    Ejemplo: fechaActualizacion cambia para todos los lugares y genera falsos positivos.
    """
    c = str(campo).lower()
    noise_words = [
        "fecha",
        "actualizacion",
        "actualización",
        "timestamp",
        "time",
        "hora",
        "minuto",
        "segundo",
        "id",
        "codigo",
        "ubigeo",
    ]
    return any(w in c for w in noise_words)


def is_relevant_change_field(campo: str) -> bool:
    """
    Solo dejamos pasar cambios de interés electoral:
    votos, porcentajes, actas y candidatos.
    """
    c = str(campo).lower()
    if is_noise_field(c):
        return False

    useful_words = [
        "voto",
        "votos",
        "porcentaje",
        "porc",
        "acta",
        "proces",
        "comput",
        "particip",
        "candidato",
        "organizacion",
        "organización",
        "%"
    ]
    return any(w in c for w in useful_words)


def titulo_columna(col: str) -> str:
    """
    Convierte nombres técnicos en títulos amigables para la tabla.
    """
    col = str(col)

    mapa = {
        "lugar": "Lugar",
        "nivel": "Nivel",
        "ambito": "Ámbito",
        "codigo": "Código",
        "campo": "Dato que cambió",
        "antes": "Valor anterior",
        "ahora": "Valor actual",
        "variacion": "Variación",
        "tipo_cambio": "Cambio",
        "actas_total": "Total de actas",
        "actas_contabilizadas": "Actas contabilizadas",
        "actas_pendientes": "Actas pendientes",
        "actas_envio_jee": "Actas para envío al JEE",
        "actas_faltantes": "Actas pendientes/faltantes",
        "%_actas_faltantes": "% de actas pendientes/faltantes",
        "error": "Error",
        "campo_original_onpe": "Campo original de ONPE",
        "valor_leido": "Valor leído",
        "participante": "Participante",
        "valor_original": "Valor original",
    }

    if col in mapa:
        return mapa[col]

    # Columnas de candidatos: "FUERZA POPULAR votos" / "FUERZA POPULAR %"
    if col.endswith(" votos"):
        return col.replace(" votos", " - votos")
    if col.endswith(" %"):
        return col.replace(" %", " - %")

    # Limpieza general para cualquier campo residual
    limpio = (
        col.replace("_", " ")
           .replace(".", " ")
           .replace("porcentaje", "porcentaje")
           .strip()
    )

    if not limpio:
        return col

    return limpio[:1].upper() + limpio[1:]


def preparar_tabla(df: pd.DataFrame) -> pd.DataFrame:
    """
    Renombra encabezados técnicos a encabezados amigables,
    oculta columnas vacías de error, evita mostrar None
    y formatea de forma uniforme:
    - votos y actas: 92,766
    - porcentajes de votos: 97.916
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    cols_a_ocultar = [c for c in df.columns if ocultar_columna_principal(c)]
    if cols_a_ocultar:
        df = df.drop(columns=cols_a_ocultar)

    if "error" in df.columns:
        errores = df["error"].fillna("").astype(str).str.strip()
        if (errores == "").all():
            df = df.drop(columns=["error"])

    for col in list(df.columns):
        col_l = str(col).lower().strip()

        if (
            col_l.endswith(" votos")
            or col_l in {
                "actas_total",
                "actas_contabilizadas",
                "actas_pendientes",
                "actas_envio_jee",
            }
        ):
            df[col] = df[col].apply(lambda x: format_number_for_display(x, 0))

        elif col_l.endswith(" %"):
            df[col] = df[col].apply(lambda x: format_number_for_display(x, 3))

    df = df.where(pd.notnull(df), "")

    return df.rename(columns={c: titulo_columna(c) for c in df.columns})


def preparar_cambios_para_mostrar(df: pd.DataFrame) -> pd.DataFrame:
    """
    Renombra columnas y mejora los textos de la tabla de variaciones.
    Las actas y votos se muestran como enteros; porcentajes de votos pueden llevar decimales.
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    if "tipo_cambio" in df.columns:
        df["tipo_cambio"] = df["tipo_cambio"].replace({
            "sube": "Subió",
            "baja": "Bajó",
            "nuevo": "Nuevo lugar",
        })

    for col in ["antes", "ahora", "variacion"]:
        if col in df.columns:
            def fmt_change(row):
                campo = str(row.get("campo", "")).lower()
                val = row.get(col)
                if val == "" or pd.isna(val):
                    return ""
                if campo.endswith(" %"):
                    return format_number_for_display(val, 3)
                if "acta" in campo or "votos" in campo:
                    return format_number_for_display(val, 0)
                return val
            df[col] = df.apply(fmt_change, axis=1)

    return preparar_tabla(df)


def find_field(nums: Dict[str, float], include_words, exclude_words=()):
    candidates = []
    for key, value in nums.items():
        k = key.lower()
        if all(w in k for w in include_words) and not any(x in k for x in exclude_words):
            candidates.append((key, value))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: len(x[0]))
    return candidates[0]


def extract_actas_info(totales: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extrae actas como ENTEROS, usando el mapeo exacto de ONPE.

    Campos de cantidad:
    - totalActas
    - contabilizadas
    - pendientesJee
    - enviadasJee

    Campos que se ignoran para actas porque son porcentajes:
    - actasContabilizadas
    - actasEnviadasJee
    - actasPendientesJee
    """
    nums = flatten_numbers(totales or {})

    def last_key(path: str) -> str:
        return str(path).split(".")[-1].lower()

    def exact_value(*names):
        wanted = {n.lower() for n in names}
        for key, value in nums.items():
            if last_key(key) in wanted:
                return parse_acta_entera(value)
        return None

    total = exact_value("totalActas")
    contabilizadas = exact_value("contabilizadas")
    actas_pendientes = exact_value("pendientesJee")
    actas_envio_jee = exact_value("enviadasJee")

    return {
        "actas_total": total,
        "actas_contabilizadas": contabilizadas,
        "actas_pendientes": actas_pendientes,
        "actas_envio_jee": actas_envio_jee,
    }


def normalize_candidate_name(item: Dict[str, Any], index: int) -> str:
    """
    Devuelve el nombre de la agrupación política/partido.
    Si no lo encuentra, usa candidato solo como respaldo y lo marca.
    """
    return choose_party_label(item, index)

    # Primero: organización política / agrupación / partido.
    org_keys = [
        "organizacionPolitica",
        "organizacion_politica",
        "nombreOrganizacionPolitica",
        "nombre_organizacion_politica",
        "organizacion",
        "organización",
        "agrupacion",
        "agrupación",
        "partido",
        "nombrePartido",
        "nombre_partido",
        "descripcionOrganizacion",
        "descripcion_organizacion",
        "siglasOrganizacion",
        "siglas_organizacion",
    ]

    for k in org_keys:
        if k in item and item.get(k):
            return fix_text(str(item.get(k))).strip()

    # Segundo: candidato, solo como respaldo.
    candidate_keys = [
        "candidato",
        "nombreCandidato",
        "nombre_candidato",
        "postulante",
        "nombrePostulante",
        "nombre_postulante",
    ]

    for k in candidate_keys:
        if k in item and item.get(k):
            return fix_text(str(item.get(k))).strip()

    # Tercero: buscar textos que parezcan organización antes que persona.
    for k, v in item.items():
        key = str(k).lower()
        if isinstance(v, str) and any(w in key for w in ["organ", "agrup", "partido"]):
            txt = fix_text(v).strip()
            if txt:
                return txt

    # Último respaldo.
    return f"Agrupación {index + 1}"


def extract_value_from_item(item: Dict[str, Any], include_words, exclude_words=()):
    nums = flatten_numbers(item)
    _, val = find_field(nums, include_words, exclude_words)
    return val


def extract_candidate_results(participantes: Any) -> Dict[str, Any]:
    """
    Devuelve columnas planas:
    candidato_1_nombre, candidato_1_votos, candidato_1_porcentaje, etc.

    Es genérico porque ONPE puede llamar los campos como votos, totalVotos,
    porcentaje, porcentajeVotos, etc.
    """
    if isinstance(participantes, dict) and "data" in participantes:
        participantes = participantes["data"]

    if isinstance(participantes, dict):
        # Buscar primera lista dentro del dict
        lists = [v for v in participantes.values() if isinstance(v, list)]
        participantes = lists[0] if lists else []

    if not isinstance(participantes, list):
        return {}

    out = {}
    for i, item in enumerate(participantes):
        if not isinstance(item, dict):
            continue

        nombre = normalize_candidate_name(item, i)

        votos = extract_value_from_item(
            item,
            include_words=["voto"],
            exclude_words=("porcentaje", "porc", "%")
        )
        if votos is None:
            votos = extract_value_from_item(
                item,
                include_words=["total"],
                exclude_words=("porcentaje", "porc", "%")
            )

        porcentaje = extract_value_from_item(
            item,
            include_words=["porcentaje"],
            exclude_words=()
        )
        if porcentaje is None:
            porcentaje = extract_value_from_item(
                item,
                include_words=["porc"],
                exclude_words=()
            )

        key = f"candidato_{i+1}"
        out[f"{key}_nombre"] = nombre
        out[f"{key}_votos"] = votos
        out[f"{key}_porcentaje"] = porcentaje

    return out


def safe_col_name(text: str) -> str:
    """Limpia el nombre para usarlo como columna."""
    text = fix_text(str(text or "")).strip()
    for ch in ["/", "\\", "\n", "\r", "\t", "  "]:
        text = text.replace(ch, " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text or "SIN NOMBRE"


def flatten_strings(obj: Any, prefix: str = "") -> Dict[str, str]:
    """
    Aplana textos del JSON para poder encontrar la agrupación política
    aunque ONPE la mande dentro de objetos internos.
    """
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            np = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten_strings(v, np))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten_strings(v, f"{prefix}[{i}]"))
    elif isinstance(obj, str):
        txt = fix_text(obj).strip()
        if txt:
            out[prefix] = txt
    return out


def looks_like_party_key(key: str) -> bool:
    k = str(key).lower()
    return any(w in k for w in [
        "organizacionpolitica",
        "organizaciónpolitica",
        "organizacion_politica",
        "organización_política",
        "organizacion",
        "organización",
        "agrupacion",
        "agrupación",
        "partido",
        "politica",
        "política",
        "sigla",
        "siglas",
        "lista",
        "movimiento",
        "alianza",
    ])


def looks_like_candidate_key(key: str) -> bool:
    k = str(key).lower()
    return any(w in k for w in [
        "candidato",
        "postulante",
        "persona",
        "nombrecompleto",
        "nombre_completo",
        "apellid",
    ])


def choose_party_label(item: Dict[str, Any], index: int) -> str:
    """
    Devuelve el nombre de la agrupación política.
    Busca primero en campos directos y también en campos anidados.
    Solo usa candidato como último respaldo.
    """
    if not isinstance(item, dict):
        return f"Agrupación {index + 1}"

    strings = flatten_strings(item)

    # 1) Buscar campos que claramente sean organización/partido/agrupación.
    party_candidates = []
    for key, value in strings.items():
        if looks_like_party_key(key) and not looks_like_candidate_key(key):
            txt = safe_col_name(value)
            if txt:
                score = 0
                k = key.lower()
                if "organizacionpolitica" in k or "organizacion_politica" in k:
                    score += 50
                if "organizacion" in k or "organización" in k:
                    score += 40
                if "agrupacion" in k or "agrupación" in k:
                    score += 35
                if "partido" in k:
                    score += 35
                if "sigla" in k:
                    score += 25
                if "candidato" in k or "postulante" in k:
                    score -= 100
                score -= len(txt) / 1000
                party_candidates.append((score, key, txt))

    if party_candidates:
        party_candidates.sort(reverse=True)
        return party_candidates[0][2]

    # 2) Si no hay campo claro, buscar textos que parezcan nombre de partido conocido por forma:
    # mayúsculas, varias palabras, no parece nombre de persona.
    possible_values = []
    for key, value in strings.items():
        if looks_like_candidate_key(key):
            continue
        txt = safe_col_name(value)
        if len(txt) >= 3 and not txt.replace(".", "").isdigit():
            possible_values.append((key, txt))

    # Evitar elegir campos genéricos como "presidente", "votos", etc.
    for key, txt in possible_values:
        kl = key.lower()
        if any(w in kl for w in ["descripcion", "nombre", "denominacion", "denominación"]) and not looks_like_candidate_key(kl):
            return txt

    # 3) Último respaldo: candidato, pero marcado como respaldo.
    for key, value in strings.items():
        if looks_like_candidate_key(key):
            txt = safe_col_name(value)
            if txt:
                return f"{txt} (candidato, no partido)"

    return f"Agrupación {index + 1}"


def candidate_display_metrics(candidatos: Dict[str, Any]) -> Dict[str, Any]:
    """
    Devuelve votos y porcentajes por agrupación política.
    Los porcentajes se mantienen SOLO para votos de agrupaciones.
    Las actas se muestran solo en cantidad.
    """
    out = {}
    if not isinstance(candidatos, dict):
        return out

    indices = []
    for k in candidatos.keys():
        m = re.match(r"candidato_(\d+)_nombre", str(k))
        if m:
            indices.append(m.group(1))

    indices = sorted(set(indices), key=lambda x: int(x))

    if not indices:
        return out

    nombres = {}
    for idx in indices:
        nombre = candidatos.get(f"candidato_{idx}_nombre")
        nombres[idx] = safe_col_name(nombre or f"Agrupación {idx}")

    # Primero votos.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    # Luego porcentajes de votos.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} %"] = candidatos.get(f"candidato_{idx}_porcentaje")

    return out

    indices = []
    for k in candidatos.keys():
        m = re.match(r"candidato_(\d+)_nombre", str(k))
        if m:
            indices.append(m.group(1))

    indices = sorted(set(indices), key=lambda x: int(x))

    if not indices:
        return out

    nombres = {}
    for idx in indices:
        nombre = candidatos.get(f"candidato_{idx}_nombre")
        nombres[idx] = safe_col_name(nombre or f"Agrupación {idx}")

    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    return out

    indices = []
    for k in candidatos.keys():
        m = re.match(r"candidato_(\d+)_nombre", str(k))
        if m:
            indices.append(m.group(1))

    indices = sorted(set(indices), key=lambda x: int(x))

    # Si por alguna razón no detecta índices, no inventa columnas.
    if not indices:
        return out

    nombres = {}
    for idx in indices:
        nombre = candidatos.get(f"candidato_{idx}_nombre")
        nombres[idx] = safe_col_name(nombre or f"Agrupación {idx}")

    # Primero votos.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    # Luego porcentajes.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} %"] = candidatos.get(f"candidato_{idx}_porcentaje")

    return out

    indices = sorted(set(
        m.group(1)
        for k in candidatos.keys()
        for m in [re.match(r"candidato_(\\d+)_nombre", str(k))]
        if m
    ), key=lambda x: int(x))

    nombres = {}
    for idx in indices:
        nombres[idx] = safe_col_name(candidatos.get(f"candidato_{idx}_nombre", f"Agrupación {idx}"))

    # Primero todos los votos.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    # Luego todos los porcentajes de partidos/agrupaciones.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} %"] = candidatos.get(f"candidato_{idx}_porcentaje")

    return out

    indices = sorted(set(
        m.group(1)
        for k in candidatos.keys()
        for m in [re.match(r"candidato_(\\d+)_nombre", str(k))]
        if m
    ), key=lambda x: int(x))

    nombres = {}
    for idx in indices:
        nombres[idx] = safe_col_name(candidatos.get(f"candidato_{idx}_nombre", f"Agrupación {idx}"))

    # Solo cantidades de votos.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    return out

    indices = sorted(set(
        m.group(1)
        for k in candidatos.keys()
        for m in [re.match(r"candidato_(\d+)_nombre", str(k))]
        if m
    ), key=lambda x: int(x))

    nombres = {}
    for idx in indices:
        nombres[idx] = safe_col_name(candidatos.get(f"candidato_{idx}_nombre", f"Candidato {idx}"))

    # Primero todos los votos, uno al lado del otro.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} votos"] = parse_voto_entero(candidatos.get(f"candidato_{idx}_votos"))

    # Luego todos los porcentajes, uno al lado del otro.
    for idx in indices:
        nombre = nombres[idx]
        out[f"{nombre} %"] = candidatos.get(f"candidato_{idx}_porcentaje")

    return out

    indices = sorted(set(
        m.group(1)
        for k in candidatos.keys()
        for m in [re.match(r"candidato_(\d+)_nombre", str(k))]
        if m
    ), key=lambda x: int(x))

    for idx in indices:
        nombre = safe_col_name(candidatos.get(f"candidato_{idx}_nombre", f"Candidato {idx}"))
        votos = candidatos.get(f"candidato_{idx}_votos")
        porcentaje = candidatos.get(f"candidato_{idx}_porcentaje")
        out[f"{nombre} votos"] = votos
        out[f"{nombre} %"] = porcentaje

    return out


async def make_page():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context(
        locale="es-PE",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
    )
    page = await context.new_page()

    # Inicialización suave: ONPE puede fallar si se entra directo a /main/resumen
    # antes de que su SessionInfo esté lista.
    try:
        await page.goto(BASE_ORIGIN, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1200)
    except Exception:
        pass

    try:
        await page.goto(f"{BASE_ORIGIN}{FRONT_PATH}", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
    except Exception:
        # No detenemos aquí. La API se reintentará en page_api.
        pass

    return pw, browser, page


async def page_api(page, path: str, params: Dict[str, Any], retries: int = 4, timeout_ms: int = 18000):
    """
    Consulta API ONPE desde el navegador.
    Si ONPE devuelve HTML, reinicializa la página y reintenta.
    """
    relative_url = endpoint(path, **params)
    absolute_url = f"{BASE_ORIGIN}{relative_url}"
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            # Asegura que el navegador tenga sesión/origen ONPE antes de pedir JSON.
            if attempt > 1:
                try:
                    await page.goto(BASE_ORIGIN, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(800)
                    await page.goto(f"{BASE_ORIGIN}{FRONT_PATH}", wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass

            result = await page.evaluate(
                """async ({url, timeoutMs}) => {
                    const controller = new AbortController();
                    const t = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const res = await fetch(url, {
                            credentials: "include",
                            cache: "no-store",
                            signal: controller.signal,
                            headers: {
                                "Accept": "application/json, text/plain, */*",
                                "X-Requested-With": "XMLHttpRequest"
                            }
                        });
                        const text = await res.text();
                        const contentType = res.headers.get("content-type") || "";
                        return {status: res.status, text, contentType};
                    } finally {
                        clearTimeout(t);
                    }
                }""",
                {"url": absolute_url, "timeoutMs": timeout_ms},
            )

            text = (result.get("text") or "").strip()
            status = result.get("status")
            content_type = (result.get("contentType") or "").lower()

            if status and int(status) >= 400:
                raise RuntimeError(f"ONPE HTTP {status}")

            if text.startswith("<") or "text/html" in content_type:
                raise RuntimeError(f"ONPE devolvió HTML (status {status})")

            parsed = json.loads(text)
            return parsed.get("data", parsed)

        except Exception as e:
            last_error = e
            await asyncio.sleep(0.8 * attempt)

    raise RuntimeError(f"{last_error} | {relative_url}")


async def get_departamentos(page, id_ambito: int):
    data = await page_api(page, "/ubigeos/departamentos", {
        "idEleccion": ID_ELECCION,
        "idAmbitoGeografico": id_ambito
    })
    if not isinstance(data, list):
        return []
    for x in data:
        x["nombre"] = fix_text(x.get("nombre", ""))
    return data


async def get_provincias(page, id_ambito: int, dep_code: str):
    data = await page_api(page, "/ubigeos/provincias", {
        "idEleccion": ID_ELECCION,
        "idAmbitoGeografico": id_ambito,
        "idUbigeoDepartamento": dep_code
    })
    if not isinstance(data, list):
        return []
    for x in data:
        x["nombre"] = fix_text(x.get("nombre", ""))
    return data


def params_general():
    return {"idEleccion": ID_ELECCION, "tipoFiltro": "eleccion"}


def params_departamento(id_ambito: int, dep_code: str):
    return {
        "idEleccion": ID_ELECCION,
        "tipoFiltro": "ubigeo_nivel_01",
        "idAmbitoGeografico": id_ambito,
        "idUbigeoDepartamento": dep_code
    }


def params_provincia(id_ambito: int, prov_code: str, dep_code: str = None):
    params = {
        "idEleccion": ID_ELECCION,
        "tipoFiltro": "ubigeo_nivel_02",
        "idAmbitoGeografico": id_ambito,
        "idUbigeoProvincia": prov_code
    }
    if dep_code:
        params["idUbigeoDepartamento"] = dep_code
    return params


async def get_totales(page, params):
    return await page_api(page, "/resumen-general/totales", params)


async def get_participantes(page, params):
    return await page_api(page, "/resumen-general/participantes", params)


def make_place(nombre, nivel, ambito, codigo, totales=None, participantes=None, error=""):
    return {
        "nombre": nombre,
        "nivel": nivel,
        "ambito": ambito,
        "codigo": codigo,
        "totales": totales or {},
        "participantes": participantes or [],
        "candidatos": extract_candidate_results(participantes or []),
        "error": error,
    }


async def safe_call(label, coro, status_box=None):
    try:
        return await coro
    except Exception as e:
        if status_box:
            status_box.warning(f"No se pudo consultar {label}: {e}")
        return {"__error__": str(e)}


async def get_place_data(page, params, label, status_box):
    tot = await safe_call(f"totales {label}", get_totales(page, params), status_box)
    part = await safe_call(f"participantes {label}", get_participantes(page, params), status_box)
    error = ""
    if isinstance(tot, dict) and tot.get("__error__"):
        error += f"Totales: {tot.get('__error__')} "
        tot = {}
    if isinstance(part, dict) and part.get("__error__"):
        error += f"Participantes: {part.get('__error__')} "
        part = []
    return tot, part, error.strip()


async def build_snapshot(include_provincias, include_extranjero, delay, status_box, progress_bar):
    pw, browser, page = await make_page()
    lugares = {}
    done = 0
    total_est = 1 + 26 + (200 if include_provincias else 0) + (35 if include_extranjero else 0)

    def tick(msg):
        nonlocal done
        done += 1
        status_box.info(msg)
        progress_bar.progress(min(done / total_est, 0.98))

    try:
        tick("Consultando total general...")
        tot, part, err = await get_place_data(page, params_general(), "general", status_box)
        lugares["GENERAL"] = make_place("GENERAL", "general", "general", "general", tot, part, err)

        tick("Listando departamentos de Perú...")
        deps = await safe_call("departamentos de Perú", get_departamentos(page, 1), status_box)
        if not isinstance(deps, list):
            deps = []
            status_box.warning("ONPE no devolvió la lista de departamentos. Se mostrará solo el total general.")

        for dep in deps:
            await asyncio.sleep(delay)
            dep_code = dep["ubigeo"]
            dep_name = dep["nombre"]
            tick(f"Consultando departamento: {dep_name}")
            tot, part, err = await get_place_data(page, params_departamento(1, dep_code), dep_name, status_box)
            lugares[f"PERU|DEPARTAMENTO|{dep_code}"] = make_place(
                dep_name, "departamento", "PERU", dep_code, tot, part, err
            )

            if include_provincias:
                await asyncio.sleep(delay)
                provs = await safe_call(f"provincias de {dep_name}", get_provincias(page, 1, dep_code), status_box)
                if isinstance(provs, list):
                    for prov in provs:
                        await asyncio.sleep(delay)
                        prov_code = prov["ubigeo"]
                        prov_name = f"{dep_name} / {prov['nombre']}"
                        tick(f"Consultando provincia: {prov_name}")
                        tot, part, err = await get_place_data(page, params_provincia(1, prov_code, dep_code), prov_name, status_box)
                        lugares[f"PERU|PROVINCIA|{prov_code}"] = make_place(
                            prov_name, "provincia", "PERU", prov_code, tot, part, err
                        )

        if include_extranjero:
            tick("Listando continentes del extranjero...")
            conts = await safe_call("continentes del extranjero", get_departamentos(page, 2), status_box)
            if not isinstance(conts, list):
                conts = []
                status_box.warning("ONPE no devolvió la lista de extranjero. Se continuará con lo disponible.")
            for cont in conts:
                await asyncio.sleep(delay)
                cont_code = cont["ubigeo"]
                cont_name = cont["nombre"]
                tick(f"Consultando continente: {cont_name}")
                tot, part, err = await get_place_data(page, params_departamento(2, cont_code), cont_name, status_box)
                lugares[f"EXTRANJERO|CONTINENTE|{cont_code}"] = make_place(
                    cont_name, "continente", "EXTRANJERO", cont_code, tot, part, err
                )

                await asyncio.sleep(delay)
                paises = await safe_call(f"países de {cont_name}", get_provincias(page, 2, cont_code), status_box)
                if isinstance(paises, list):
                    for pais in paises:
                        await asyncio.sleep(delay)
                        pais_code = pais["ubigeo"]
                        pais_name = f"{cont_name} / {pais['nombre']}"
                        tick(f"Consultando país: {pais_name}")
                        tot, part, err = await get_place_data(page, params_provincia(2, pais_code, cont_code), pais_name, status_box)
                        lugares[f"EXTRANJERO|PAIS|{pais_code}"] = make_place(
                            pais_name, "pais", "EXTRANJERO", pais_code, tot, part, err
                        )

        progress_bar.progress(1.0)
        status_box.success("Consulta terminada.")
        return {
            "_meta": {
                "fecha_consulta": fecha_hora_peru(),
                "idEleccion": ID_ELECCION,
            },
            "lugares": lugares,
        }
    finally:
        await browser.close()
        await pw.stop()


def load_previous():
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return None


def save_snapshot(snapshot):
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def compare_snapshots(old, new):
    if not old:
        return []

    changes = []
    for key, new_place in new.get("lugares", {}).items():
        old_place = old.get("lugares", {}).get(key)
        if not old_place:
            changes.append({
                "lugar": new_place.get("nombre", key),
                "nivel": new_place.get("nivel", ""),
                "ambito": new_place.get("ambito", ""),
                "codigo": new_place.get("codigo", ""),
                "campo": "nuevo lugar",
                "antes": "",
                "ahora": "apareció",
                "variacion": "",
                "tipo_cambio": "nuevo"
            })
            continue

        if new_place.get("error") or old_place.get("error"):
            # Igual intentamos comparar candidatos si existen
            pass

        # Comparar totales y candidatos juntos.
        # Los candidatos se comparan usando su nombre como campo:
        # "Nombre del candidato votos" / "Nombre del candidato %".
        old_compare = {
            **flatten_numbers(old_place.get("totales", {})),
            **candidate_display_metrics(old_place.get("candidatos", {}) or {})
        }
        new_compare = {
            **flatten_numbers(new_place.get("totales", {})),
            **candidate_display_metrics(new_place.get("candidatos", {}) or {})
        }

        # Convert candidate values that are numeric strings
        def to_num(v, field_name=""):
            return parse_onpe_number(v, field_name)

        for field, new_value_raw in new_compare.items():
            pretty_field = campo_legible(field)

            # Ignorar fechas, timestamps, códigos y otros campos que generan ruido.
            if not is_relevant_change_field(pretty_field):
                continue

            new_value = to_num(new_value_raw, field)
            old_value = to_num(old_compare.get(field), field)
            if old_value is None or new_value is None:
                continue
            if new_value != old_value:
                delta = new_value - old_value
                changes.append({
                    "lugar": new_place.get("nombre", key),
                    "nivel": new_place.get("nivel", ""),
                    "ambito": new_place.get("ambito", ""),
                    "codigo": new_place.get("codigo", ""),
                    "campo": pretty_field,
                    "antes": old_value,
                    "ahora": new_value,
                    "variacion": delta,
                    "tipo_cambio": "sube" if delta > 0 else "baja"
                })

    changes.sort(key=lambda x: abs(float(x["variacion"]) if x["variacion"] != "" else 0), reverse=True)
    return changes



def sumar_filas_por_grupo(rows, ambito, nivel, nombre_total):
    """
    Crea una fila total sumando solo un nivel específico.
    TOTAL PERÚ suma departamentos.
    TOTAL EXTRANJERO suma continentes.
    """
    base_rows = [
        r for r in rows
        if str(r.get("ambito", "")).upper() == ambito.upper()
        and str(r.get("nivel", "")).lower() == nivel.lower()
    ]

    if not base_rows:
        return None

    total = {
        "lugar": nombre_total,
        "nivel": "total",
        "ambito": ambito,
        "error": "",
    }

    keys = set()
    for r in base_rows:
        keys.update(r.keys())

    for k in keys:
        if k in ["lugar", "nivel", "ambito", "codigo", "error"] or ocultar_columna_principal(k):
            continue

        values = []
        for r in base_rows:
            parsed = parse_onpe_number(r.get(k), k)
            if parsed is not None:
                values.append(parsed)

        total[k] = sum(values) if values else ""

    # Recalcular % de actas faltantes, no sumar porcentajes.
    actas_total = parse_onpe_number(total.get("actas_total"), "actas_total")
    actas_faltantes = parse_onpe_number(total.get("actas_faltantes"), "actas_faltantes")
    if actas_total and actas_faltantes is not None:
        total["%_actas_faltantes"] = (actas_faltantes / actas_total) * 100

    return total


def agregar_totales_peru_extranjero(rows):
    """
    Agrega TOTAL PERÚ y TOTAL EXTRANJERO al inicio de la tabla.
    No duplica provincias ni países.
    """
    rows = list(rows)

    extras = []

    total_peru = sumar_filas_por_grupo(
        rows,
        ambito="PERU",
        nivel="departamento",
        nombre_total="TOTAL PERÚ"
    )
    if total_peru:
        extras.append(total_peru)

    total_extranjero = sumar_filas_por_grupo(
        rows,
        ambito="EXTRANJERO",
        nivel="continente",
        nombre_total="TOTAL EXTRANJERO"
    )
    if total_extranjero:
        extras.append(total_extranjero)

    return extras + rows



def ocultar_columna_principal(nombre_columna: str) -> bool:
    """
    Oculta columnas crudas/confusas de ONPE.
    Mantiene votos y porcentajes de agrupaciones políticas.
    Oculta porcentajes de actas y otros porcentajes crudos.
    """
    c = str(nombre_columna).lower().strip()

    columnas_oficiales = {
        "actas_total",
        "actas_contabilizadas",
        "actas_pendientes",
        "actas_envio_jee",
    }
    if c in columnas_oficiales:
        return False

    # Mantener columnas de agrupaciones políticas.
    if c.endswith(" votos") or c.endswith(" %"):
        return False

    # Ocultar porcentajes crudos de actas u otros porcentajes de ONPE.
    if "%" in c or "porcentaje" in c or "porc" in c:
        return True

    ocultar = [
        "actas_faltantes",
        "%_actas_faltantes",
        "actas faltantes",
        "pendientes/faltantes",
        "pendiente jee",
        "pendientes jee",
        "pendientes de enviar al jee",
        "actas pendientes jee",
        "envio jee",
        "envío jee",
        "para envio",
        "para envío",
        "participacion",
        "participación",
        "ciudadana",
        "fechaactualizacion",
        "fecha actualizacion",
        "fecha actualización",
        "timestamp",
        "ubigeo",
        "codigo",
        "código",
    ]

    return any(x in c for x in ocultar)


def rows_snapshot(snapshot):
    rows = []
    for v in snapshot.get("lugares", {}).values():
        nums = flatten_numbers(v.get("totales", {}))
        actas = extract_actas_info(v.get("totales", {}))
        candidatos = v.get("candidatos", {}) or {}

        row = {
            "lugar": v.get("nombre"),
            "nivel": v.get("nivel"),
            "ambito": v.get("ambito"),
            "actas_total": actas.get("actas_total"),
            "actas_contabilizadas": actas.get("actas_contabilizadas"),
            "actas_pendientes": actas.get("actas_pendientes"),
            "actas_envio_jee": actas.get("actas_envio_jee"),
            "error": v.get("error", ""),
        }

        # Agrega candidatos usando sus nombres reales como columnas.
        # Ejemplo: "FUERZA POPULAR votos", "FUERZA POPULAR %".
        for key, val in candidate_display_metrics(candidatos).items():
            row[key] = val

        rows.append(row)
    return agregar_totales_peru_extranjero(rows)




def raw_participantes_fields(snapshot):
    """
    Muestra campos originales de participantes para verificar si ONPE trae partido o candidato.
    """
    rows = []
    for place in snapshot.get("lugares", {}).values():
        participantes = place.get("participantes", [])
        if isinstance(participantes, dict):
            participantes = participantes.get("data", participantes)
        if isinstance(participantes, dict):
            lists = [v for v in participantes.values() if isinstance(v, list)]
            participantes = lists[0] if lists else []
        if not isinstance(participantes, list):
            continue

        for i, item in enumerate(participantes, start=1):
            strings = flatten_strings(item)
            nums = flatten_numbers(item)

            for key, value in strings.items():
                rows.append({
                    "lugar": place.get("nombre"),
                    "participante": i,
                    "campo_original_onpe": key,
                    "valor_original": str(value),
                })

            for key, value in nums.items():
                k = str(key).lower()
                if "voto" in k or "porc" in k or "porcentaje" in k or "%" in k:
                    rows.append({
                        "lugar": place.get("nombre"),
                        "participante": i,
                        "campo_original_onpe": key,
                        "valor_original": str(value),
                    })

    return rows


def raw_actas_fields(snapshot):
    """
    Devuelve campos crudos relacionados con actas para verificar de dónde salen los datos.
    Incluye también porcentajes, para distinguirlos de cantidades reales.
    """
    rows = []
    for place in snapshot.get("lugares", {}).values():
        nums = flatten_numbers(place.get("totales", {}))
        for key, value in nums.items():
            k = str(key).lower()
            if "acta" in k or "contabil" in k or "proces" in k or "comput" in k or "pendiente" in k or "jee" in k:
                rows.append({
                    "lugar": place.get("nombre"),
                    "nivel": place.get("nivel"),
                    "campo_original_onpe": key,
                    "valor_leido": str(value),
                })
    return rows




def obtener_resumen_total_candidatos(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Obtiene SOLO el resumen TOTAL GENERAL entre los dos candidatos.

    Regla correcta:
    1. Usar la fila donde Nivel == general y Ámbito == general.
    2. No usar las filas total / PERU ni total / EXTRANJERO.
    3. Solo si no existe general/general, usar la última fila disponible como respaldo.
    """
    if df is None or df.empty:
        return {}

    dfx = df.copy()

    total_row = None

    if "nivel" in dfx.columns and "ambito" in dfx.columns:
        nivel = dfx["nivel"].astype(str).str.strip().str.lower()
        ambito = dfx["ambito"].astype(str).str.strip().str.lower()

        mask_general = (nivel == "general") & (ambito == "general")
        if mask_general.any():
            total_row = dfx[mask_general].iloc[0]

    # Respaldo: si no encuentra general/general, usa la última fila.
    # Esto evita agarrar por error total/PERU como total general.
    if total_row is None:
        total_row = dfx.iloc[-1]

    votos_cols = [c for c in dfx.columns if str(c).lower().strip().endswith(" votos")]

    if len(votos_cols) < 2:
        return {}

    c1_votos_col = votos_cols[0]
    c2_votos_col = votos_cols[1]

    n1 = c1_votos_col[:-6].strip()
    n2 = c2_votos_col[:-6].strip()

    v1 = parse_onpe_number(total_row.get(c1_votos_col), c1_votos_col)
    v2 = parse_onpe_number(total_row.get(c2_votos_col), c2_votos_col)

    if v1 is None or v2 is None:
        return {}

    p1 = None
    p2 = None

    p1_col = f"{n1} %"
    p2_col = f"{n2} %"

    if p1_col in dfx.columns:
        p1 = parse_onpe_number(total_row.get(p1_col), p1_col)
    if p2_col in dfx.columns:
        p2 = parse_onpe_number(total_row.get(p2_col), p2_col)

    lider = n1 if float(v1) >= float(v2) else n2
    diferencia_votos = abs(float(v1) - float(v2))

    diferencia_pp = None
    if p1 is not None and p2 is not None:
        diferencia_pp = abs(float(p1) - float(p2))

    return {
        "fila_usada": "general / general",
        "candidato_1": n1,
        "votos_1": int(round(float(v1))),
        "porcentaje_1": p1,
        "candidato_2": n2,
        "votos_2": int(round(float(v2))),
        "porcentaje_2": p2,
        "lider": lider,
        "diferencia_votos": int(round(diferencia_votos)),
        "diferencia_pp": diferencia_pp,
    }


def mostrar_recuadro_resumen_candidatos(df: pd.DataFrame):
    resumen = obtener_resumen_total_candidatos(df)

    if not resumen:
        st.info("No se pudo calcular todavía el resumen total entre candidatos.")
        return

    st.subheader("Resumen total entre candidatos")

    c1 = resumen["candidato_1"]
    c2 = resumen["candidato_2"]

    votos_1 = format_number_for_display(resumen["votos_1"], 0)
    votos_2 = format_number_for_display(resumen["votos_2"], 0)
    diff_votos = format_number_for_display(resumen["diferencia_votos"], 0)

    p1 = resumen.get("porcentaje_1")
    p2 = resumen.get("porcentaje_2")
    diff_pp = resumen.get("diferencia_pp")

    pct_1_txt = f" — {format_number_for_display(p1, 3)} %" if p1 is not None else ""
    pct_2_txt = f" — {format_number_for_display(p2, 3)} %" if p2 is not None else ""
    diff_pp_txt = f"{format_number_for_display(diff_pp, 3)} puntos %" if diff_pp is not None else "No disponible"

    st.markdown(
        f"""
<style>
.resumen-onpe-box {{
  border: 1px solid rgba(120,120,120,.22);
  border-radius: 12px;
  padding: 14px 16px;
  margin: 10px 0 18px 0;
  background: rgba(120,120,120,.04);
  color: inherit;
}}
.resumen-onpe-title {{
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 9px;
}}
.resumen-onpe-line {{
  font-size: 14px;
  margin-bottom: 6px;
}}
.resumen-onpe-highlight {{
  font-size: 15px;
  font-weight: 700;
  margin-top: 10px;
  margin-bottom: 6px;
}}
.resumen-onpe-small {{
  opacity: .58;
  font-size: 12px;
  margin-top: 10px;
}}
</style>

<div class="resumen-onpe-box">
  <div class="resumen-onpe-title">Resumen general</div>

  <div class="resumen-onpe-line"><b>{c1}</b>: {votos_1} votos{pct_1_txt}</div>
  <div class="resumen-onpe-line"><b>{c2}</b>: {votos_2} votos{pct_2_txt}</div>

  <div class="resumen-onpe-highlight">Va adelante: {resumen["lider"]}</div>
  <div class="resumen-onpe-line"><b>Diferencia de votos:</b> {diff_votos}</div>
  <div class="resumen-onpe-line"><b>Diferencia en porcentaje:</b> {diff_pp_txt}</div>

  <div class="resumen-onpe-small">Base: nivel general / ámbito general</div>
</div>
        """,
        unsafe_allow_html=True,
    )



def ordenar_columnas_principales(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tabla principal estricta:
    Lugar, Nivel, Ámbito, votos por agrupación, porcentajes de votos,
    y actas solo en cantidad.
    """
    if df is None or df.empty:
        return df

    cols = list(df.columns)

    base = [c for c in ["lugar", "nivel", "ambito"] if c in cols]

    votos = [
        c for c in cols
        if str(c).lower().strip().endswith(" votos")
    ]

    porcentajes_votos = [
        c for c in cols
        if str(c).lower().strip().endswith(" %")
    ]

    actas = [
        c for c in [
            "actas_total",
            "actas_contabilizadas",
            "actas_pendientes",
            "actas_envio_jee",
        ]
        if c in cols
    ]

    columnas_finales = base + votos + porcentajes_votos + actas
    return df[columnas_finales]


def style_changes(df: pd.DataFrame):
    def row_style(row):
        tipo = str(row.get("Cambio", row.get("tipo_cambio", ""))).lower()
        campo = str(row.get("Dato que cambió", row.get("campo", ""))).lower()

        es_candidato = campo.endswith(" votos") or campo.endswith(" %") or " votos" in campo

        if es_candidato:
            base = "background-color: #fff8e1"
            if "subió" in tipo or "sube" in tipo:
                return [base + "; color: #0b7a28; font-weight: 700"] * len(row)
            if "bajó" in tipo or "baja" in tipo:
                return [base + "; color: #b00020; font-weight: 700"] * len(row)
            return [base] * len(row)

        if "subió" in tipo or "sube" in tipo:
            return ["background-color: #e8f5e9"] * len(row)
        if "bajó" in tipo or "baja" in tipo:
            return ["background-color: #ffebee"] * len(row)
        if "nuevo" in tipo:
            return ["background-color: #e3f2fd"] * len(row)
        return [""] * len(row)

    return df.style.apply(row_style, axis=1).format(precision=4)




PERU_TZ = ZoneInfo("America/Lima")


def fecha_hora_peru(dt: datetime | None = None) -> str:
    """Devuelve fecha/hora fija en huso horario de Perú."""
    if dt is None:
        dt = datetime.now(PERU_TZ)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=PERU_TZ)
    else:
        dt = dt.astimezone(PERU_TZ)
    return dt.strftime("%d/%m/%Y %H:%M:%S") + " — hora Perú"


def prioridad_cambio(row) -> tuple:
    nivel = str(row.get("nivel", "")).strip().lower()
    ambito = str(row.get("ambito", "")).strip().lower()
    lugar = str(row.get("lugar", "")).strip().lower()
    campo = str(row.get("campo", "")).strip().lower()

    if nivel == "general" and ambito == "general":
        p_lugar = 0
    elif nivel == "total":
        p_lugar = 1
    elif "peru" in ambito:
        p_lugar = 2
    elif "extranjero" in ambito:
        p_lugar = 3
    else:
        p_lugar = 4

    if "votos" in campo:
        p_campo = 0
    elif campo.endswith(" %") or "porcentaje" in campo or "porc" in campo:
        p_campo = 1
    elif "acta" in campo:
        p_campo = 2
    else:
        p_campo = 3

    try:
        variacion = abs(float(row.get("variacion", 0) or 0))
    except Exception:
        variacion = 0

    return (p_lugar, p_campo, -variacion, lugar, campo)


def preparar_cambios_ordenados(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    dfx = df.copy()
    dfx["__orden"] = dfx.apply(prioridad_cambio, axis=1)
    dfx = dfx.sort_values("__orden").drop(columns=["__orden"])
    return dfx


def cambios_total_general(df_changes: pd.DataFrame) -> pd.DataFrame:
    if df_changes is None or df_changes.empty:
        return pd.DataFrame()
    nivel = df_changes.get("nivel", pd.Series(dtype=str)).astype(str).str.strip().str.lower()
    ambito = df_changes.get("ambito", pd.Series(dtype=str)).astype(str).str.strip().str.lower()
    return df_changes[(nivel == "general") & (ambito == "general")].copy()


def texto_delta_votos_total(df_changes: pd.DataFrame) -> list[str]:
    out = []
    total = cambios_total_general(df_changes)
    if total.empty:
        return out
    total = total[total["campo"].astype(str).str.lower().str.contains("votos", na=False)]
    total = preparar_cambios_ordenados(total).head(4)
    for _, r in total.iterrows():
        campo = str(r.get("campo", ""))
        partido = campo.replace(" votos", "").strip()
        var = parse_onpe_number(r.get("variacion"), "votos")
        if var is None:
            continue
        signo = "+" if var >= 0 else "−"
        out.append(f"{partido}: {signo}{format_number_for_display(abs(var), 0)} votos")
    return out


def calcular_resumen_metricas(df_snapshot: pd.DataFrame, changes=None) -> Dict[str, Any]:
    resumen = obtener_resumen_total_candidatos(df_snapshot)
    if not resumen:
        return {}

    actas_cont = None
    actas_total = None
    try:
        dfx = df_snapshot.copy()
        mask = (dfx["nivel"].astype(str).str.lower().str.strip() == "general") & (dfx["ambito"].astype(str).str.lower().str.strip() == "general")
        row = dfx[mask].iloc[0] if mask.any() else dfx.iloc[-1]
        actas_cont = parse_onpe_number(row.get("actas_contabilizadas"), "actas_contabilizadas")
        actas_total = parse_onpe_number(row.get("actas_total"), "actas_total")
    except Exception:
        pass

    return {
        **resumen,
        "actas_contabilizadas": actas_cont,
        "actas_total": actas_total,
        "cambios": len(changes or []),
    }


def render_css():
    st.markdown(
        """
<style>
.block-container {
  padding-top: 1.2rem;
  padding-bottom: 2rem;
  max-width: 1180px;
}

/* Minimal header */
.onpe-hero {
  border: 1px solid rgba(120,120,120,.22);
  border-radius: 14px;
  padding: 18px 20px;
  margin-bottom: 18px;
  background: rgba(120,120,120,.06);
  color: inherit;
  box-shadow: none;
}
.onpe-hero h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 750;
  letter-spacing: -.02em;
}
.onpe-hero p {
  margin: 6px 0 0 0;
  font-size: 14px;
  opacity: .72;
}
.onpe-pill {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 999px;
  background: transparent;
  border: 1px solid rgba(120,120,120,.28);
  font-size: 12px;
  opacity: .78;
  margin-bottom: 8px;
}

/* Minimal metric cards */
.metric-card {
  border: 1px solid rgba(120,120,120,.22);
  border-radius: 12px;
  padding: 13px 14px;
  background: rgba(120,120,120,.045);
  min-height: 95px;
  box-shadow: none;
}
.metric-card .label {
  font-size: 12px;
  opacity: .66;
  margin-bottom: 5px;
}
.metric-card .value {
  font-size: 21px;
  font-weight: 700;
  line-height: 1.15;
}
.metric-card .help {
  font-size: 11px;
  opacity: .55;
  margin-top: 6px;
}

/* Simple sections */
.section-card {
  border: 1px solid rgba(120,120,120,.20);
  border-radius: 12px;
  padding: 14px 16px;
  background: rgba(120,120,120,.04);
  margin: 10px 0 16px 0;
}
.quick-line {
  padding: 7px 0;
  border-bottom: 1px solid rgba(120,120,120,.12);
  font-size: 14px;
}
.quick-line:last-child { border-bottom: none; }
.small-muted { opacity: .62; font-size: 12px; }

/* Streamlit tweaks */
div[data-testid="stMetric"] {
  border: 1px solid rgba(120,120,120,.20);
  border-radius: 12px;
  padding: 10px 12px;
  background: rgba(120,120,120,.04);
}
h1, h2, h3 {
  letter-spacing: -.01em;
}
hr {
  margin: 1rem 0;
  opacity: .35;
}
</style>
        """,
        unsafe_allow_html=True,
    )

def render_header(fecha=None):
    fecha_txt = fecha or fecha_hora_peru()
    st.markdown(
        f"""
<div class="onpe-hero">
  <div class="onpe-pill">Seguimiento no oficial · ONPE</div>
  <h1>Monitor electoral</h1>
  <p>Votos, actas y variaciones. Hora Perú: {fecha_txt}</p>
</div>
        """,
        unsafe_allow_html=True,
    )

def render_metric_cards(df_snapshot: pd.DataFrame, changes=None, fecha=None):
    m = calcular_resumen_metricas(df_snapshot, changes)
    if not m:
        return

    c1, c2, c3, c4 = st.columns(4)
    diff = format_number_for_display(m.get("diferencia_votos"), 0)
    actas = ""
    if m.get("actas_contabilizadas") is not None and m.get("actas_total") is not None:
        actas = f'{format_number_for_display(m.get("actas_contabilizadas"), 0)} / {format_number_for_display(m.get("actas_total"), 0)}'
    else:
        actas = "No disponible"

    with c1:
        st.markdown(f'<div class="metric-card"><div class="label">Va adelante</div><div class="value">{m.get("lider", "")}</div><div class="help">Según fila general / general</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="metric-card"><div class="label">Diferencia de votos</div><div class="value">{diff}</div><div class="help">Entre los dos candidatos</div></div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="metric-card"><div class="label">Actas contabilizadas</div><div class="value">{actas}</div><div class="help">Total general</div></div>', unsafe_allow_html=True)
    with c4:
        st.markdown(f'<div class="metric-card"><div class="label">Cambios detectados</div><div class="value">{m.get("cambios", 0)}</div><div class="help">Última revisión</div></div>', unsafe_allow_html=True)


def render_lectura_rapida(df_changes: pd.DataFrame, df_snapshot: pd.DataFrame):
    st.subheader("Lectura rápida de la actualización")

    if df_changes is None or df_changes.empty:
        st.markdown('<div class="section-card"><div class="quick-line">No se detectaron cambios frente a la consulta anterior.</div></div>', unsafe_allow_html=True)
        return

    total = cambios_total_general(df_changes)
    votos_total = texto_delta_votos_total(df_changes)
    resumen = obtener_resumen_total_candidatos(df_snapshot)

    lineas = []
    lineas.append(f"Se detectaron <b>{len(df_changes)}</b> variaciones en esta revisión.")

    if not total.empty:
        lineas.append(f"En el <b>total general</b> hubo <b>{len(total)}</b> cambio(s).")
    else:
        lineas.append("No hubo cambios directos en la fila <b>general / general</b>.")

    for t in votos_total:
        lineas.append(t)

    if resumen:
        diff = format_number_for_display(resumen.get("diferencia_votos"), 0)
        pp = resumen.get("diferencia_pp")
        pp_txt = f" · {format_number_for_display(pp, 3)} puntos %" if pp is not None else ""
        lineas.append(f"Actualmente va adelante <b>{resumen.get('lider')}</b> por <b>{diff}</b> votos{pp_txt}.")

    html = '<div class="section-card">' + ''.join(f'<div class="quick-line">{x}</div>' for x in lineas) + '</div>'
    st.markdown(html, unsafe_allow_html=True)


def crear_excel_historial(historial: pd.DataFrame, cambios_actuales: pd.DataFrame | None = None) -> bytes:
    """Crea un Excel real con filtros, tablas y columnas ordenadas."""
    output = BytesIO()

    hist = preparar_cambios_ordenados(historial.copy()) if historial is not None and not historial.empty else pd.DataFrame()
    actual = preparar_cambios_ordenados(cambios_actuales.copy()) if cambios_actuales is not None and not cambios_actuales.empty else pd.DataFrame()

    # Columnas lógicas primero.
    orden = ["fecha_consulta", "lugar", "nivel", "ambito", "campo", "antes", "ahora", "variacion", "tipo_cambio", "codigo"]
    def ordenar_cols(df):
        if df is None or df.empty:
            return df
        cols = [c for c in orden if c in df.columns] + [c for c in df.columns if c not in orden]
        return df[cols]

    hist = ordenar_cols(hist)
    actual = ordenar_cols(actual)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        if not actual.empty:
            preparar_cambios_para_mostrar(actual).to_excel(writer, sheet_name="Ultima actualizacion", index=False)
        if not hist.empty:
            preparar_cambios_para_mostrar(hist).to_excel(writer, sheet_name="Historial completo", index=False)
        if actual.empty and hist.empty:
            pd.DataFrame([{"Mensaje": "Todavía no hay cambios registrados."}]).to_excel(writer, sheet_name="Historial", index=False)

        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col_cells in ws.columns:
                max_len = 0
                letter = col_cells[0].column_letter
                for cell in col_cells:
                    try:
                        max_len = max(max_len, len(str(cell.value or "")))
                    except Exception:
                        pass
                ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 42)

    return output.getvalue()



def snapshot_to_excel_bytes(snapshot: Dict[str, Any]) -> bytes:
    """
    Exporta la base actual completa en Excel.
    """
    if not snapshot:
        return b""

    df_snapshot = ordenar_columnas_principales(pd.DataFrame(rows_snapshot(snapshot)))
    meta = pd.DataFrame([snapshot.get("_meta", {})])
    raw_actas = pd.DataFrame(raw_actas_fields(snapshot))
    raw_partidos = pd.DataFrame(raw_participantes_fields(snapshot))

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sheets = {
            "Base actual": preparar_tabla(df_snapshot),
            "Meta": meta,
        }
        if not raw_actas.empty:
            sheets["Actas crudas"] = preparar_tabla(raw_actas)
        if not raw_partidos.empty:
            sheets["Candidatos crudos"] = preparar_tabla(raw_partidos)

        for sheet_name, df_sheet in sheets.items():
            safe_name = str(sheet_name)[:31]
            df_sheet.to_excel(writer, index=False, sheet_name=safe_name)
            ws = writer.book[safe_name]
            ws.freeze_panes = "A2"
            if df_sheet.shape[1] > 0:
                ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                max_len = 0
                letter = col[0].column_letter
                for cell in col:
                    try:
                        max_len = max(max_len, len("" if cell.value is None else str(cell.value)))
                    except Exception:
                        pass
                ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 45)

    return output.getvalue()


def snapshot_to_json_bytes(snapshot: Dict[str, Any]) -> bytes:
    if not snapshot:
        return b""
    return json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")


def restaurar_base_desde_upload(uploaded_file) -> bool:
    if uploaded_file is None:
        return False

    try:
        data = json.loads(uploaded_file.getvalue().decode("utf-8"))
        if not isinstance(data, dict) or "lugares" not in data:
            st.error("Ese archivo no parece ser una base válida de esta app.")
            return False
        save_snapshot(data)
        st.success("Base restaurada correctamente. La próxima actualización comparará contra esa base.")
        return True
    except Exception as e:
        st.error(f"No se pudo restaurar la base: {e}")
        return False


def render_respaldo_base(snapshot: Dict[str, Any] = None):
    st.subheader("Respaldo de la base")

    current = snapshot if snapshot else load_previous()

    st.markdown(
        """
Esta parte sirve para que no pierdas la base si Streamlit se reinicia o si actualizas la app.

- **Excel:** para revisar la base actual en Excel.
- **JSON:** para guardar una copia exacta y restaurarla después.
        """
    )

    if current:
        fecha = current.get("_meta", {}).get("fecha_consulta", "sin fecha")
        st.caption(f"Base disponible: {fecha} — hora Perú")

        col1, col2 = st.columns(2)
        col1.download_button(
            "Descargar base actual en Excel",
            data=snapshot_to_excel_bytes(current),
            file_name="base_actual_onpe.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        col2.download_button(
            "Descargar respaldo JSON",
            data=snapshot_to_json_bytes(current),
            file_name="base_actual_onpe_respaldo.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.info("Todavía no hay base guardada. Haz una primera consulta para crearla.")

    with st.expander("Restaurar una base anterior"):
        uploaded = st.file_uploader(
            "Sube el respaldo JSON que descargaste antes",
            type=["json"],
        )
        if uploaded is not None:
            if st.button("Restaurar base JSON"):
                restaurar_base_desde_upload(uploaded)



def render_descargas(historial: pd.DataFrame, df_changes: pd.DataFrame | None = None, snapshot: Dict[str, Any] = None):
    """
    Descarga única en Excel. No genera botones CSV.
    """
    hay_historial = historial is not None and not historial.empty
    hay_cambios_actuales = df_changes is not None and not df_changes.empty

    if hay_historial or hay_cambios_actuales:
        excel_bytes = crear_excel_historial(
            historial if hay_historial else pd.DataFrame(),
            df_changes if hay_cambios_actuales else pd.DataFrame(),
        )
        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="actualizaciones_onpe.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

def cargar_historial():
    if HISTORY_FILE.exists():
        try:
            return pd.read_csv(HISTORY_FILE)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def guardar_historial(changes, fecha_consulta):
    """
    Agrega cambios al historial permanente.
    No reemplaza el historial anterior.
    """
    if not changes:
        return

    df_new = pd.DataFrame(changes)
    if df_new.empty:
        return

    df_new.insert(0, "fecha_consulta", fecha_consulta)

    df_old = cargar_historial()
    df_all = pd.concat([df_old, df_new], ignore_index=True)
    df_all.to_csv(HISTORY_FILE, index=False, encoding="utf-8")


def run_consulta(include_provincias, include_extranjero, delay):
    status_box = st.empty()
    progress = st.progress(0)
    previous = load_previous()

    snapshot = asyncio.run(build_snapshot(include_provincias, include_extranjero, delay, status_box, progress))
    changes = compare_snapshots(previous, snapshot)
    save_snapshot(snapshot)

    guardar_historial(changes, snapshot.get("_meta", {}).get("fecha_consulta", ""))

    return previous, snapshot, changes


st.set_page_config(page_title="Monitor electoral", layout="wide")
render_css()
render_header()

if "auto_monitor" not in st.session_state:
    st.session_state.auto_monitor = False
if "auto_interval_min" not in st.session_state:
    st.session_state.auto_interval_min = 5
if "last_refresh_count" not in st.session_state:
    st.session_state.last_refresh_count = -1

with st.sidebar:
    st.header("Consulta")
    include_provincias = st.checkbox("Monitorear provincias de Perú", value=False)
    include_extranjero = st.checkbox("Monitorear extranjero por continente y país", value=False)
    delay = st.slider("Pausa entre llamadas", min_value=0.1, max_value=2.0, value=0.4, step=0.1)

    st.divider()
    st.header("Autoactualización")
    interval_choice = st.radio(
        "Refrescar revisión completa cada:",
        options=[2, 3, 5, 10],
        index=[2, 3, 5, 10].index(st.session_state.auto_interval_min),
        format_func=lambda x: f"{x} minutos",
    )
    st.session_state.auto_interval_min = interval_choice

    col_a, col_b = st.columns(2)
    start_auto = col_a.button("Iniciar auto")
    stop_auto = col_b.button("Detener auto")

    if start_auto:
        st.session_state.auto_monitor = True
        st.session_state.last_refresh_count = -1
    if stop_auto:
        st.session_state.auto_monitor = False

    st.caption("La pausa ayuda a evitar errores por demasiadas llamadas seguidas.")

consultar = st.button("Actualizar y comparar", type="primary")
limpiar = st.button("Borrar base")
limpiar_historial = st.button("Borrar historial")

if limpiar:
    if SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.unlink()
    st.success("Base anterior eliminada.")

if limpiar_historial:
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    st.success("Historial de cambios eliminado.")

refresh_count = None
if st.session_state.auto_monitor:
    if st_autorefresh is None:
        st.error("Falta instalar streamlit-autorefresh. Ejecuta: python3 -m pip install streamlit-autorefresh")
    else:
        refresh_count = st_autorefresh(
            interval=st.session_state.auto_interval_min * 60 * 1000,
            key="onpe_auto_refresh",
        )
        st.info(f"Monitoreo automático activo: cada {st.session_state.auto_interval_min} minutos.")

should_run_auto = (
    st.session_state.auto_monitor
    and refresh_count is not None
    and refresh_count != st.session_state.last_refresh_count
)

if should_run_auto:
    st.session_state.last_refresh_count = refresh_count

should_run = consultar or should_run_auto

if should_run:
    try:
        previous, snapshot, changes = run_consulta(include_provincias, include_extranjero, delay)

        fecha_consulta = snapshot['_meta']['fecha_consulta']
        st.success(f"Consulta realizada: {fecha_consulta}")

        rows = rows_snapshot(snapshot)
        df_snapshot = ordenar_columnas_principales(pd.DataFrame(rows_snapshot(snapshot)))

        render_metric_cards(df_snapshot, changes, fecha_consulta)

        st.markdown("---")

        if previous is None:
            st.info("Primera consulta guardada como base. La siguiente revisión mostrará cambios.")
        elif not changes:
            render_lectura_rapida(pd.DataFrame(), df_snapshot)
        else:
            df_changes = preparar_cambios_ordenados(pd.DataFrame(changes))
            render_lectura_rapida(df_changes, df_snapshot)

            st.subheader("Cambios importantes")
            total_general_changes = cambios_total_general(df_changes)
            candidate_changes = df_changes[df_changes["campo"].astype(str).str.contains("votos| %", case=False, na=False, regex=True)]

            if not total_general_changes.empty:
                st.markdown("**Total general**")
                st.dataframe(style_changes(preparar_cambios_para_mostrar(total_general_changes)), use_container_width=True, hide_index=True)
            elif not candidate_changes.empty:
                st.markdown("**Votos y porcentajes de candidatos**")
                st.dataframe(style_changes(preparar_cambios_para_mostrar(candidate_changes.head(20))), use_container_width=True, hide_index=True)
            else:
                st.info("No hubo cambios principales en votos o total general.")

            with st.expander("Ver detalle completo de cambios"):
                st.dataframe(style_changes(preparar_cambios_para_mostrar(df_changes)), use_container_width=True, hide_index=True)

            resumen = (
                df_changes.groupby(["ambito", "nivel"], dropna=False)
                .agg(cantidad_cambios=("campo", "count"), variacion_abs=("variacion", lambda s: pd.to_numeric(s, errors="coerce").abs().sum()))
                .reset_index()
                .sort_values("cantidad_cambios", ascending=False)
            )
            with st.expander("Ver resumen técnico por ámbito"):
                st.dataframe(preparar_tabla(resumen), use_container_width=True, hide_index=True)

        historial = cargar_historial()
        render_descargas(historial, pd.DataFrame(changes) if changes else pd.DataFrame(), snapshot)

        st.subheader("Tabla principal")

        columnas_partidos = [
            c for c in df_snapshot.columns
            if str(c).lower().endswith(" votos") or str(c).lower().endswith(" %")
        ]
        if not columnas_partidos:
            st.error("No se detectaron columnas de votos/porcentajes de agrupaciones. Abre el desplegable de campos originales de agrupaciones para revisar qué está devolviendo ONPE.")

        # Validación simple de actas: contabilizadas + JEE + pendientes debe igualar total.
        try:
            for _, rr in df_snapshot.iterrows():
                total_a = parse_onpe_number(rr.get("actas_total"), "actas_total")
                cont_a = parse_onpe_number(rr.get("actas_contabilizadas"), "actas_contabilizadas")
                pend_a = parse_onpe_number(rr.get("actas_pendientes"), "actas_pendientes")
                jee_a = parse_onpe_number(rr.get("actas_envio_jee"), "actas_envio_jee")
                if all(x is not None for x in [total_a, cont_a, pend_a, jee_a]):
                    if abs(total_a - (cont_a + pend_a + jee_a)) > 0.1:
                        st.warning("Revisa actas: en al menos una fila no cuadra Total = Contabilizadas + Para envío al JEE + Pendientes.")
                        break
        except Exception:
            pass

        st.dataframe(preparar_tabla(df_snapshot), use_container_width=True)

        mostrar_recuadro_resumen_candidatos(df_snapshot)

        with st.expander("Ver campos originales de actas detectados por ONPE"):
            raw_df = pd.DataFrame(raw_actas_fields(snapshot))
            if raw_df.empty:
                st.info("No se encontraron campos crudos de actas en la respuesta.")
            else:
                st.dataframe(preparar_tabla(raw_df), use_container_width=True)

        with st.expander("Ver campos originales de agrupaciones/candidatos detectados por ONPE"):
            raw_part_df = pd.DataFrame(raw_participantes_fields(snapshot))
            if raw_part_df.empty:
                st.info("No se encontraron campos crudos de participantes en la respuesta.")
            else:
                st.dataframe(preparar_tabla(raw_part_df), use_container_width=True)

        errors = [v for v in snapshot.get("lugares", {}).values() if v.get("error")]
        if errors:
            st.subheader("Errores")
            st.dataframe(preparar_tabla(pd.DataFrame([{
                "lugar": e.get("nombre"),
                "nivel": e.get("nivel"),
                "error": e.get("error"),
            } for e in errors])), use_container_width=True)

    except Exception as e:
        st.error(f"La consulta se detuvo: {e}")
else:
    prev = load_previous()
    if prev:
        st.info(f"Base guardada: {prev.get('_meta', {}).get('fecha_consulta')}. Puedes actualizar datos para comparar.")
        st.subheader("Base guardada")
        st.dataframe(preparar_tabla(ordenar_columnas_principales(pd.DataFrame(rows_snapshot(prev)))), use_container_width=True)
    else:
        st.info("Todavía no hay base guardada. Haz una primera consulta.")