
import asyncio
import json
import re
import subprocess
from pathlib import Path

def ensure_playwright_browsers():
    marker = Path.home() / ".cache" / "ms-playwright"
    if not marker.exists() or not any(marker.iterdir()):
        subprocess.run(["playwright", "install", "chromium"], check=False)

ensure_playwright_browsers()
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

BASE_ORIGIN = "https://resultadosegundavuelta.onpe.gob.pe"
BASE = "/presentacion-backend"
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
                "diferencia_votos",
            }
        ):
            df[col] = df[col].apply(lambda x: format_number_for_display(x, 0))

        elif col_l.endswith(" %") or col_l == "diferencia_pp":
            df[col] = df[col].apply(lambda x: format_number_for_display(x, 3))

    df = df.where(pd.notnull(df), "")

    return df.rename(columns={c: titulo_columna(c) for c in df.columns})


def preparar_cambios_para_mostrar(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tabla de variaciones: no usa el filtro estricto de la tabla principal.
    Formatea cantidades y porcentajes, y renombra encabezados.
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    if "tipo_cambio" in df.columns:
        df["tipo_cambio"] = df["tipo_cambio"].replace({
            "sube": "Subió",
            "baja": "Bajó",
            "nuevo": "Nuevo lugar",
            "cambio": "Cambió",
        })

    for col in ["antes", "ahora", "variacion"]:
        if col in df.columns:
            def fmt_change(row):
                campo = str(row.get("campo", "")).lower()
                val = row.get(col)
                if val == "" or pd.isna(val):
                    return ""
                if campo == "lider":
                    return val
                if campo.endswith(" %") or campo == "diferencia_pp":
                    return format_number_for_display(val, 3)
                if "acta" in campo or "votos" in campo or campo == "diferencia_votos":
                    return format_number_for_display(val, 0)
                return val
            df[col] = df.apply(fmt_change, axis=1)

    df = df.where(pd.notnull(df), "")

    rename = {
        "fecha_consulta": "Fecha consulta",
        "lugar": "Lugar",
        "nivel": "Nivel",
        "ambito": "Ámbito",
        "campo": "Campo",
        "antes": "Antes",
        "ahora": "Ahora",
        "variacion": "Variación",
        "tipo_cambio": "Cambio",
    }
    df = df.rename(columns=rename)

    if "Campo" in df.columns:
        df["Campo"] = df["Campo"].apply(titulo_columna)

    return df


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
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        locale="es-PE",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
    )
    page = await context.new_page()
    await page.goto(f"{BASE_ORIGIN}/resumen", wait_until="domcontentloaded", timeout=45000)
    return pw, browser, page


async def page_api(page, path: str, params: Dict[str, Any], retries: int = 2, timeout_ms: int = 12000):
    url = endpoint(path, **params)
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            result = await page.evaluate(
                """async ({url, timeoutMs}) => {
                    const controller = new AbortController();
                    const t = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const res = await fetch(url, {
                            credentials: "same-origin",
                            cache: "no-store",
                            signal: controller.signal,
                            headers: {
                                "Accept": "application/json, text/plain, */*",
                                "X-Requested-With": "XMLHttpRequest"
                            }
                        });
                        const text = await res.text();
                        return {status: res.status, text};
                    } finally {
                        clearTimeout(t);
                    }
                }""",
                {"url": url, "timeoutMs": timeout_ms},
            )

            text = (result.get("text") or "").strip()
            if text.startswith("<"):
                raise RuntimeError("ONPE devolvió HTML")
            return json.loads(text).get("data", {})
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.5 * attempt)

    raise RuntimeError(f"{last_error} | {url}")


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
        deps = await get_departamentos(page, 1)

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
            conts = await get_departamentos(page, 2)
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
                "fecha_consulta": datetime.now().isoformat(timespec="seconds"),
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
        "lider",
        "diferencia_votos",
        "diferencia_pp",
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

    rows = agregar_diferencia_candidatos_a_rows(rows)
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



def ordenar_columnas_principales(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tabla principal estricta:
    Lugar, Nivel, Ámbito, votos por agrupación, porcentajes de votos,
    diferencia total entre agrupaciones, y actas solo en cantidad.
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

    diferencia = [
        c for c in ["lider", "diferencia_votos", "diferencia_pp"]
        if c in cols
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

    columnas_finales = base + votos + porcentajes_votos + diferencia + actas
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



def ordenar_variaciones(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    def bloque(campo):
        c = str(campo).lower()
        if "diferencia" in c or c in ["lider"]:
            return "1. Diferencia total entre candidatos"
        if c.endswith(" votos") or " votos" in c:
            return "2. Cambios de votos"
        if c.endswith(" %"):
            return "3. Cambios de porcentaje de votos"
        if "acta" in c:
            return "4. Cambios de actas"
        return "5. Otros cambios"

    df["bloque"] = df["campo"].apply(bloque)

    if "variacion" in df.columns:
        df["_magnitud"] = df["variacion"].apply(lambda x: abs(parse_onpe_number(x, "variacion") or 0))
    else:
        df["_magnitud"] = 0

    sort_cols = [c for c in ["bloque", "lugar", "nivel", "_magnitud", "campo"] if c in df.columns]
    ascending = [True] * len(sort_cols)
    if "_magnitud" in sort_cols:
        ascending[sort_cols.index("_magnitud")] = False

    df = df.sort_values(sort_cols, ascending=ascending) if sort_cols else df
    df = df.drop(columns=["_magnitud"], errors="ignore")
    return df


def mostrar_variaciones_sistematizadas(df_changes: pd.DataFrame):
    if df_changes is None or df_changes.empty:
        st.info("No se detectaron cambios frente a la base anterior.")
        return

    df_ord = ordenar_variaciones(df_changes)

    bloques = [
        "1. Diferencia total entre candidatos",
        "2. Cambios de votos",
        "3. Cambios de porcentaje de votos",
        "4. Cambios de actas",
        "5. Otros cambios",
    ]

    for bloque in bloques:
        parte = df_ord[df_ord["bloque"] == bloque].drop(columns=["bloque"], errors="ignore")
        if parte.empty:
            continue

        titulo = bloque.split(". ", 1)[1]
        st.markdown(f"**{titulo}**")
        st.dataframe(preparar_cambios_para_mostrar(parte), use_container_width=True)



st.set_page_config(page_title="Monitor ONPE Desktop v35", layout="wide")
st.title("Monitor ONPE — variaciones ordenadas v35")

st.write(
    "Ordena variaciones por bloques, agrega diferencia total entre candidatos y mantiene historial acumulado."
)

if "auto_monitor" not in st.session_state:
    st.session_state.auto_monitor = False
if "auto_interval_min" not in st.session_state:
    st.session_state.auto_interval_min = 5
if "last_refresh_count" not in st.session_state:
    st.session_state.last_refresh_count = -1

with st.sidebar:
    st.header("Configuración")
    include_provincias = st.checkbox("Monitorear provincias de Perú", value=False)
    include_extranjero = st.checkbox("Monitorear extranjero por continente y país", value=False)
    delay = st.slider("Pausa entre llamadas", min_value=0.1, max_value=2.0, value=0.4, step=0.1)

    st.divider()
    st.header("Monitoreo automático")
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

    st.caption("El intervalo automático es entre revisiones completas. La pausa entre llamadas es dentro de cada revisión.")

consultar = st.button("Consultar ahora y comparar", type="primary")
limpiar = st.button("Limpiar base anterior")
limpiar_historial = st.button("Limpiar historial de cambios")

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

        st.success(f"Consulta realizada: {snapshot['_meta']['fecha_consulta']}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Lugares consultados", len(snapshot.get("lugares", {})))
        c2.metric("Variaciones detectadas", len(changes))
        c3.metric("Errores", sum(1 for v in snapshot.get("lugares", {}).values() if v.get("error")))

        rows = rows_snapshot(snapshot)
        c4.metric("Columnas principales", "votos, %, actas")

        if previous is None:
            st.info("Primera consulta guardada como base. La siguiente revisión mostrará cambios.")
        elif not changes:
            st.success("No se detectaron cambios frente a la consulta anterior.")
        else:
            st.warning(f"Se detectaron {len(changes)} variaciones.")
            df_changes = pd.DataFrame(changes)

            st.subheader("Variaciones resaltadas")
            st.write("Amarillo = cambio en votos/porcentaje de candidato. Verde = subió. Rojo = bajó. Azul = lugar nuevo.")
            st.dataframe(style_changes(preparar_cambios_para_mostrar(df_changes)), use_container_width=True)

            candidate_changes = df_changes[df_changes["campo"].str.contains(" votos| %", case=False, na=False, regex=True)]
            if not candidate_changes.empty:
                st.subheader("Cambios de votos/porcentaje por agrupación política")
                st.dataframe(style_changes(preparar_cambios_para_mostrar(candidate_changes)), use_container_width=True)

            resumen = (
                df_changes.groupby(["ambito", "nivel"], dropna=False)
                .agg(cantidad_cambios=("campo", "count"), variacion_abs=("variacion", lambda s: pd.to_numeric(s, errors="coerce").abs().sum()))
                .reset_index()
                .sort_values("cantidad_cambios", ascending=False)
            )
            st.subheader("Resumen de cambios")
            st.dataframe(preparar_tabla(resumen), use_container_width=True)

            st.download_button(
                "Descargar variaciones CSV",
                preparar_cambios_para_mostrar(df_changes).to_csv(index=False).encode("utf-8"),
                "variaciones_onpe.csv",
                "text/csv",
            )

        st.subheader("Historial acumulado de cambios")
        historial = cargar_historial()
        if historial.empty:
            st.info("Todavía no hay historial acumulado de cambios.")
        else:
            st.dataframe(preparar_cambios_para_mostrar(ordenar_variaciones(historial)), use_container_width=True)
            st.download_button(
                "Descargar historial de cambios CSV",
                preparar_cambios_para_mostrar(ordenar_variaciones(historial)).to_csv(index=False).encode("utf-8"),
                "historial_cambios_onpe.csv",
                "text/csv",
            )

        st.subheader("Estado actual con votos, porcentajes y actas")

        df_snapshot = ordenar_columnas_principales(pd.DataFrame(rows_snapshot(snapshot)))

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
        st.info(f"Base guardada: {prev.get('_meta', {}).get('fecha_consulta')}. Puedes consultar ahora para comparar.")
        st.subheader("Base guardada")
        st.dataframe(preparar_tabla(ordenar_columnas_principales(pd.DataFrame(rows_snapshot(prev)))), use_container_width=True)
    else:
        st.info("Todavía no hay base guardada. Haz una primera consulta.")
