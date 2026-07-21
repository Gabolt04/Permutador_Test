# flask_app.py
# Backend para el Permutador de Horarios del TEC.
#
# Descubrimos (leyendo js/jsescuela.js) que la Guía de Horarios NO usa
# postback de ASP.NET: usa PageMethods vía AJAX. Es decir, el propio sitio
# expone estos "endpoints" JSON que nosotros podemos llamar directamente:
#
#   POST {BASE_URL}/cargaEscuelas            -> lista de escuelas
#   POST {BASE_URL}/cargaModalidadPeriodos    -> lista de modalidades
#   POST {BASE_URL}/getdatosEscuelaAno        -> cursos/grupos/horarios
#          body: {"escuela": "<id>", "ano": "<año>"}
#
# Cada respuesta viene envuelta así: {"d": "<string con el JSON real>"}
# (patrón clásico de ASP.NET ScriptService) hay que hacer json.loads(d) dos veces.
#
# LIMITACIÓN CONOCIDA: esta guía no expone el campo de aula/salón — solo
# sede, código, nombre del curso, grupo, profesor, modalidad, periodo y
# horario (día + hora inicio/fin). Si necesitas aula, no viene de aquí.
#
# Para desplegarlo, ver requirements.txt e instrucciones de Render.

import json
import re
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import urllib3
from bs4 import BeautifulSoup

app = Flask(__name__, static_folder=".")
CORS(app)  # permite llamar esta API desde un frontend en otro dominio (ej. GitHub Pages)

BASE_URL = "https://tec-appsext.itcr.ac.cr/guiahorarios/escuela.aspx"

# El servidor del TEC tiene mal configurada la cadena de certificados SSL
# (le falta el intermedio). Los navegadores lo disimulan porque ya traen
# esos intermedios cacheados; `requests` no. Como solo leemos datos
# públicos de horarios, desactivamos la verificación para este dominio y
# silenciamos el warning correspondiente.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
VERIFICAR_SSL = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DIA_MAP = {
    "LUNES": "L",
    "MARTES": "K",
    "MIERCOLES": "M",
    "MIÉRCOLES": "M",
    "JUEVES": "J",
    "VIERNES": "V",
    "SABADO": "S",
    "SÁBADO": "S",
    "DOMINGO": "D",
}

# ---------------------------------------------------------------------
# Caché simple en memoria (por proceso). Evita que dos personas buscando
# lo mismo casi al mismo tiempo disparen el scraping completo dos veces.
# No es una caché compartida entre workers/procesos (para eso haría falta
# algo como Redis), pero para el uso esperado aquí es suficiente y no
# agrega ninguna dependencia extra.
# ---------------------------------------------------------------------
import time
import threading

_CACHE = {}
_CACHE_LOCK = threading.Lock()
TTL_SEGUNDOS = 10 * 60  # 10 minutos


def con_cache(clave, ttl=TTL_SEGUNDOS):
    """Devuelve (encontrado, valor) desde la caché si sigue vigente."""
    with _CACHE_LOCK:
        entrada = _CACHE.get(clave)
        if entrada and (time.time() - entrada[0]) < ttl:
            return True, entrada[1]
    return False, None


def guardar_cache(clave, valor):
    with _CACHE_LOCK:
        _CACHE[clave] = (time.time(), valor)


def nueva_sesion():
    """Crea una sesión con cookies válidas visitando primero la página
    (algunos sitios ASP.NET exigen una sesión/cookie previa para aceptar
    llamadas a PageMethods)."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE_URL, timeout=20, verify=VERIFICAR_SSL)
    return s


def llamar_webmethod(session, metodo, parametros=None):
    """Llama a un PageMethod de la guía de horarios y devuelve la lista/
    diccionario ya decodificado (deshace el doble-JSON típico de ASP.NET)."""
    url = f"{BASE_URL}/{metodo}"
    body = json.dumps(parametros) if parametros is not None else "{}"
    r = session.post(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
        verify=VERIFICAR_SSL,
    )
    r.raise_for_status()
    envoltorio = r.json()
    contenido = envoltorio.get("d")
    if not contenido or contenido in ("false", "NO DATOS"):
        return []
    return json.loads(contenido)


def texto(valor):
    """Convierte cualquier valor (str, int, float, None) a texto limpio.
    El TEC a veces manda campos como número (ej. IDE_GRUPO=1 en vez de '01'),
    lo que rompía .strip() directo sobre esos valores."""
    if valor is None:
        return ""
    return str(valor).strip()


@app.route("/")
def index():
    return send_from_directory(".", "Permutador_Horarios.html")


@app.route("/api/opciones")
def api_opciones():
    try:
        encontrado, valor = con_cache(("tec_opciones",))
        if encontrado:
            return jsonify(valor)

        s = nueva_sesion()
        escuelas = llamar_webmethod(s, "cargaEscuelas")
        modalidades = llamar_webmethod(s, "cargaModalidadPeriodos")
        guardar_cache(("tec_modalidades",), modalidades)
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar al TEC: {e}"}), 502

    anio_actual = datetime.now().year
    anios = [
        {"value": str(a), "text": str(a)}
        for a in (anio_actual + 1, anio_actual, anio_actual - 1)
    ]
    # Estos periodos vienen fijos en el propio HTML del TEC (no por AJAX)
    periodos = [{"value": str(p), "text": str(p)} for p in (1, 2, 3, 4, 5, 6, 7, 12)]

    resultado = {
        "escuelas": [
            {"value": str(e.get("IDE_DEPTO", "")), "text": f'{e.get("IDE_DEPTO","")} - {e.get("DSC_DEPTO","")}'}
            for e in escuelas
        ],
        "anios": anios,
        "periodos": periodos,
        "modalidades": [
            {"value": str(m.get("IDE_MODALIDAD", "")), "text": m.get("NOMBRE", "")}
            for m in modalidades
        ],
    }
    guardar_cache(("tec_opciones",), resultado)
    return jsonify(resultado)


@app.route("/api/cursos")
def api_cursos():
    escuela = request.args.get("escuela", "").strip()
    anio = request.args.get("anio", "").strip()
    periodo = request.args.get("periodo", "").strip()
    modalidad_id = request.args.get("modalidad", "").strip()

    if not escuela or not anio:
        return jsonify({"error": "Falta escuela o año"}), 400

    try:
        s = nueva_sesion()

        clave_cache = ("tec_filas", escuela, anio)
        encontrado, filas = con_cache(clave_cache)
        if not encontrado:
            filas = llamar_webmethod(s, "getdatosEscuelaAno", {"escuela": escuela, "ano": anio})
            guardar_cache(clave_cache, filas)

        modalidad_texto = ""
        if modalidad_id:
            encontrado_mod, modalidades = con_cache(("tec_modalidades",))
            if not encontrado_mod:
                modalidades = llamar_webmethod(s, "cargaModalidadPeriodos")
                guardar_cache(("tec_modalidades",), modalidades)
            for m in modalidades:
                if texto(m.get("IDE_MODALIDAD")) == modalidad_id:
                    modalidad_texto = texto(m.get("NOMBRE")).upper()
                    break
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar al TEC: {e}"}), 502

    if not isinstance(filas, list):
        return jsonify([])

    try:
        agrupado = {}
        for fila in filas:
            if periodo and texto(fila.get("IDE_PER_MOD")) != periodo:
                continue
            if modalidad_texto and texto(fila.get("DSC_MODALIDAD")).upper() != modalidad_texto:
                continue

            clave = (
                texto(fila.get("IDE_MATERIA")), texto(fila.get("IDE_GRUPO")),
                texto(fila.get("DSC_SEDE")), texto(fila.get("TIPO_CURSO")), texto(fila.get("IDE_PER_MOD")),
            )
            if clave not in agrupado:
                agrupado[clave] = {
                    "codigo": texto(fila.get("IDE_MATERIA")),
                    "nombre": texto(fila.get("DSC_MATERIA")),
                    "sede": texto(fila.get("DSC_SEDE")),
                    "grupo": texto(fila.get("IDE_GRUPO")),
                    "profesor": texto(fila.get("NOM_PROFESOR")),
                    "modalidad": texto(fila.get("DSC_MODALIDAD")),
                    "periodo": texto(fila.get("IDE_PER_MOD")),
                    "slots": [],
                }

            dia_original = texto(fila.get("NOM_DIA")).upper()
            dia = DIA_MAP.get(dia_original, dia_original[:1])
            inicio = texto(fila.get("HINICIO"))
            fin = texto(fila.get("HFIN"))
            if dia and inicio and fin:
                agrupado[clave]["slots"].append({"dia": dia, "inicio": inicio, "fin": fin, "aula": ""})
    except Exception as e:
        return jsonify({"error": f"Error procesando datos del TEC: {e}"}), 500

    return jsonify(list(agrupado.values()))


# ==========================================================================
# ================================ UCR ====================================
# ==========================================================================
# A diferencia del TEC, la Guía de Horarios de la UCR NO tiene una API JSON:
# es un formulario clásico de ASP.NET WebForms con postback completo (cada
# combo dispara __doPostBack y recarga la página entera con el HTML
# actualizado). Además, solo muestra el detalle de UN curso ("sigla") a la
# vez: para traer todos los cursos de una escuela hay que iterar el combo
# cboSigla y hacer un postback por cada uno. Por eso /api/ucr/cursos puede
# tardar más que su equivalente del TEC — hace 1 + N peticiones (N = # de
# cursos en la escuela elegida).
#
# Cadena de combos (cada uno depende del anterior):
#   cboGuia -> cboCiclo -> cboRecinto (= sede) -> cboEscuela -> cboSigla

UCR_BASE = "https://guiahorarios.ucr.ac.cr/ggh/"

UCR_DIA_MAP = {
    "L": "L", "K": "K", "M": "M", "J": "J", "V": "V", "S": "S", "D": "D",
}


def nueva_sesion_ucr():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def campos_ocultos_ucr(soup):
    """Extrae todos los <input type=hidden> de la página (viewstate,
    eventvalidation, y cualquier otro campo de estado que la UCR use)."""
    campos = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        nombre = inp.get("name")
        if nombre:
            campos[nombre] = inp.get("value", "")
    return campos


def postback_ucr(session, soup_actual, event_target, campos_extra):
    """Hace un postback de ASP.NET WebForms: toma los campos ocultos de la
    página actual, agrega los que estamos seleccionando, y devuelve el
    soup de la página resultante."""
    campos = campos_ocultos_ucr(soup_actual)
    campos["__EVENTTARGET"] = event_target
    campos["__EVENTARGUMENT"] = ""
    campos.update(campos_extra)
    r = session.post(UCR_BASE, data=campos, timeout=30, verify=VERIFICAR_SSL)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def opciones_select_ucr(soup, select_id):
    sel = soup.find(id=select_id)
    if not sel:
        return []
    out = []
    for opt in sel.find_all("option"):
        valor = (opt.get("value") or "").strip()
        if not valor:
            continue
        out.append({"value": valor, "text": opt.get_text(strip=True)})
    return out


def obtener_pagina_consulta_ucr(session):
    """El GET inicial a la UCR devuelve la página de bienvenida, con un
    botón de submit (id='btnConsultar', texto 'Consultar guía de horarios
    acá') que hay que enviar para llegar al formulario real con los
    combos. Si no encontramos cboGuia, buscamos ese botón y reenviamos
    el formulario con su nombre/valor incluido (así es como lo hace un
    <input type=submit> de ASP.NET, sin pasar por __doPostBack)."""
    r = session.get(UCR_BASE, timeout=20, verify=VERIFICAR_SSL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.find(id="cboGuia"):
        return soup

    boton = None
    for candidato in soup.find_all("input", {"type": ["submit", "button"]}):
        texto = (candidato.get("value") or "")
        id_ = (candidato.get("id") or "")
        if "consultar" in texto.lower() or "consultar" in id_.lower():
            boton = candidato
            break

    if boton is not None:
        campos = campos_ocultos_ucr(soup)
        nombre_boton = boton.get("name") or boton.get("id")
        campos[nombre_boton] = boton.get("value", "")
        r2 = session.post(UCR_BASE, data=campos, timeout=30, verify=VERIFICAR_SSL)
        r2.raise_for_status()
        soup = BeautifulSoup(r2.text, "html.parser")
        if soup.find(id="cboGuia"):
            return soup

    # Respaldo: si no había botón de submit, probamos con un enlace tipo
    # __doPostBack (LinkButton) que mencione "consultar" en su id.
    enlace = None
    for a in soup.find_all("a", id=True):
        if "onsult" in a["id"].lower():
            enlace = a
            break
    if enlace is not None:
        soup = postback_ucr(session, soup, enlace["id"], {})

    return soup


def cadena_ucr(session, valores_en_orden):
    """valores_en_orden: dict con las claves en el orden de dependencia,
    ej. {'cboGuia': '1_1', 'cboCiclo': '2_2026'}. Ejecuta un postback por
    cada una, acumulando los campos previos (igual que hace el navegador),
    y devuelve el soup final."""
    soup = obtener_pagina_consulta_ucr(session)
    acumulado = {}
    for campo, valor in valores_en_orden.items():
        acumulado[campo] = valor
        soup = postback_ucr(session, soup, campo, acumulado)
    return soup


def parsear_curso_ucr(card_div, sede_texto):
    """Convierte un <div class="card mb-2"> (un curso completo, con todos
    sus grupos) en la misma forma que usa el frontend: lista de
    {codigo, nombre, sede, grupo, profesor, slots}."""
    h5 = card_div.find("h5", class_="card-title")
    titulo = h5.get_text(strip=True) if h5 else ""
    if " - " in titulo:
        codigo, nombre = titulo.split(" - ", 1)
    else:
        codigo, nombre = titulo, ""

    tabla_grupos = card_div.find("table", class_="table-hover")
    if not tabla_grupos:
        return []

    grupos = {}
    grupo_actual = None
    tipo_columna = None  # 'modalidad' (Modalidad/Profesor) u 'horario' (Edificio/Aula/Horario)

    for fila in tabla_grupos.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) < 2:
            continue  # filas espaciadoras (colspan) sin datos útiles

        encabezado = celdas[1].get_text(strip=True)

        if encabezado == "Modalidad":
            texto_grupo = celdas[0].get_text(strip=True)
            grupo_actual = re.sub(r"(?i)^grupo", "", texto_grupo).strip()
            if grupo_actual not in grupos:
                grupos[grupo_actual] = {"profesor": "", "modalidad": "", "slots": []}
            tipo_columna = "modalidad"
            continue

        if encabezado == "Edificio":
            tipo_columna = "horario"
            continue

        if grupo_actual is None or len(celdas) < 4:
            continue

        if tipo_columna == "modalidad":
            grupos[grupo_actual]["modalidad"] = celdas[1].get_text(strip=True)
            grupos[grupo_actual]["profesor"] = celdas[2].get_text(strip=True)
        elif tipo_columna == "horario":
            edificio = celdas[1].get_text(strip=True)
            aula = celdas[2].get_text(strip=True)
            horario_texto = celdas[3].get_text(" ", strip=True)
            aula_completa = f"{edificio} {aula}".strip()
            for dias, ini, fin in re.findall(
                r"([LKMJVSD]+)\s+(\d{1,2}:\d{2})\s*a\s*(\d{1,2}:\d{2})", horario_texto
            ):
                for letra in dias:
                    grupos[grupo_actual]["slots"].append({
                        "dia": UCR_DIA_MAP.get(letra, letra),
                        "inicio": ini,
                        "fin": fin,
                        "aula": aula_completa,
                    })

    resultado = []
    for numero, datos in grupos.items():
        resultado.append({
            "codigo": codigo.strip(),
            "nombre": nombre.strip(),
            "sede": sede_texto,
            "grupo": numero,
            "profesor": datos["profesor"],
            "modalidad": datos["modalidad"],
            "slots": datos["slots"],
        })
    return resultado


@app.route("/api/ucr/debug")
def api_ucr_debug():
    """Endpoint temporal de diagnóstico: muestra qué trae realmente el GET
    inicial a la UCR (título, selects presentes, enlaces con id) para
    entender por qué cboGuia no aparece."""
    try:
        s = nueva_sesion_ucr()
        r = s.get(UCR_BASE, timeout=20, verify=VERIFICAR_SSL)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        return jsonify({
            "status_code": r.status_code,
            "url_final": r.url,
            "titulo": soup.title.get_text(strip=True) if soup.title else None,
            "selects_encontrados": [s.get("id") for s in soup.find_all("select")],
            "enlaces_con_id": [{"id": a.get("id"), "texto": a.get_text(strip=True)} for a in soup.find_all("a", id=True)],
            "botones_con_id": [{"id": b.get("id"), "texto": b.get_text(strip=True) or b.get("value")} for b in soup.find_all(["button", "input"], id=True)],
            "primeros_2000_chars": r.text[:2000],
        })
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502


@app.route("/api/ucr/guias")
def api_ucr_guias():
    try:
        s = nueva_sesion_ucr()
        soup = obtener_pagina_consulta_ucr(s)
        return jsonify(opciones_select_ucr(soup, "cboGuia"))
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502


@app.route("/api/ucr/ciclos")
def api_ucr_ciclos():
    guia = request.args.get("guia", "")
    if not guia:
        return jsonify({"error": "Falta guia"}), 400
    try:
        s = nueva_sesion_ucr()
        soup = cadena_ucr(s, {"cboGuia": guia})
        return jsonify(opciones_select_ucr(soup, "cboCiclo"))
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502


@app.route("/api/ucr/recintos")
def api_ucr_recintos():
    guia = request.args.get("guia", "")
    ciclo = request.args.get("ciclo", "")
    if not guia or not ciclo:
        return jsonify({"error": "Falta guia o ciclo"}), 400
    try:
        s = nueva_sesion_ucr()
        soup = cadena_ucr(s, {"cboGuia": guia, "cboCiclo": ciclo})
        return jsonify(opciones_select_ucr(soup, "cboRecinto"))
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502


@app.route("/api/ucr/escuelas")
def api_ucr_escuelas():
    guia = request.args.get("guia", "")
    ciclo = request.args.get("ciclo", "")
    recinto = request.args.get("recinto", "")
    if not guia or not ciclo or not recinto:
        return jsonify({"error": "Falta guia, ciclo o recinto"}), 400
    try:
        s = nueva_sesion_ucr()
        soup = cadena_ucr(s, {"cboGuia": guia, "cboCiclo": ciclo, "cboRecinto": recinto})
        return jsonify(opciones_select_ucr(soup, "cboEscuela"))
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502


@app.route("/api/ucr/cursos")
def api_ucr_cursos():
    guia = request.args.get("guia", "")
    ciclo = request.args.get("ciclo", "")
    recinto = request.args.get("recinto", "")
    escuela = request.args.get("escuela", "")
    if not all([guia, ciclo, recinto, escuela]):
        return jsonify({"error": "Falta guia, ciclo, recinto o escuela"}), 400

    clave_cache = ("ucr_cursos", guia, ciclo, recinto, escuela)
    encontrado, valor = con_cache(clave_cache)
    if encontrado:
        return jsonify(valor)

    try:
        s = nueva_sesion_ucr()
        soup = cadena_ucr(s, {
            "cboGuia": guia, "cboCiclo": ciclo,
            "cboRecinto": recinto, "cboEscuela": escuela,
        })

        recinto_sel = soup.find(id="cboRecinto")
        recinto_texto = ""
        if recinto_sel:
            opt = recinto_sel.find("option", value=recinto)
            recinto_texto = opt.get_text(strip=True) if opt else ""

        siglas = opciones_select_ucr(soup, "cboSigla")
    except Exception as e:
        return jsonify({"error": f"No se pudo contactar a la UCR: {e}"}), 502

    todos_los_cursos = []
    campos_base = campos_ocultos_ucr(soup)
    campos_base.update({
        "cboGuia": guia, "cboCiclo": ciclo,
        "cboRecinto": recinto, "cboEscuela": escuela,
    })

    for sigla in siglas:
        try:
            campos = dict(campos_base)
            campos["cboSigla"] = sigla["value"]
            campos["__EVENTTARGET"] = "cboSigla"
            campos["__EVENTARGUMENT"] = ""
            r = s.post(UCR_BASE, data=campos, timeout=30, verify=VERIFICAR_SSL)
            r.raise_for_status()
            soup_curso = BeautifulSoup(r.text, "html.parser")

            for card in soup_curso.select("div.card.mb-2"):
                todos_los_cursos.extend(parsear_curso_ucr(card, recinto_texto))

            # los campos ocultos (viewstate) cambian en cada respuesta;
            # los actualizamos para la siguiente iteración del bucle
            campos_base = campos_ocultos_ucr(soup_curso)
            campos_base.update({
                "cboGuia": guia, "cboCiclo": ciclo,
                "cboRecinto": recinto, "cboEscuela": escuela,
            })
        except Exception:
            continue  # si un curso puntual falla, seguimos con el resto

    guardar_cache(clave_cache, todos_los_cursos)
    return jsonify(todos_los_cursos)


if __name__ == "__main__":
    app.run(debug=True)
