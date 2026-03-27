import streamlit as st
import pdfplumber
import pandas as pd
import requests
import psycopg2
import psycopg2.extras
import math
import re
import io
import base64
import unicodedata
from bs4 import BeautifulSoup
from openai import OpenAI

# ---------------------------
# CONFIGURACION
# ---------------------------
st.set_page_config(
    page_title="OBRIXA AI",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded"
)

from dotenv import load_dotenv
import os
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_KEY")
DB_URL      = os.getenv("postgresql://postgres.zomdvxmiqqwpxhxklpeb:ObrixaSalaz2024@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
SUPABASE_URL = os.getenv("zomdvxmiqqwpxhxklpeb")
SUPABASE_KEY = os.getenv("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvbWR2eG1pcXF3cHhoeGtscGViIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM4NzIwODksImV4cCI6MjA4OTQ0ODA4OX0.xLRYnXIvVl6nl6UvnL2z5A4aSvrU8b_pMpt5NMe0qAk")

@st.cache_resource
def get_openai():
    return OpenAI(api_key=OPENAI_KEY)

openai_client = get_openai()

def get_conn():
    return psycopg2.connect(DB_URL)

# ---------------------------
# TASAS DE CAMBIO
# ---------------------------
@st.cache_data(ttl=3600)
def obtener_tasas():
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        return r.json().get("rates", {})
    except Exception:
        return {"COP": 4100, "USD": 1, "EUR": 0.92, "MXN": 17.2}

def convertir_precio(valor, origen, destino):
    tasas = obtener_tasas()
    if origen not in tasas or destino not in tasas:
        return valor
    return (valor / tasas[origen]) * tasas[destino]

# ---------------------------
# LECTURA DE ARCHIVOS
# ---------------------------
def leer_pdf(file):
    texto = ""
    try:
        data = file.read()
        file.seek(0)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t:
                    texto += t + "\n"
    except Exception as e:
        st.error(f"Error leyendo PDF: {e}")
    return texto

def extraer_tabla_precios_pdf(file):
    productos, precios = [], []
    try:
        data = file.read()
        file.seek(0)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pagina in pdf.pages:
                for tabla in (pagina.extract_tables() or []):
                    for fila in tabla:
                        if not fila:
                            continue
                        fc = [c for c in fila if c not in (None, "", " ")]
                        if len(fc) < 2:
                            continue
                        prod = fc[0].replace("\n", " ").strip()
                        prec = fc[-1].replace(".", "").replace(",", "").strip()
                        if prec.isdigit():
                            productos.append(prod)
                            precios.append(int(prec))
    except Exception as e:
        st.error(f"Error extrayendo tabla: {e}")
    return pd.DataFrame({"producto": productos, "precio": precios})

def leer_excel(file):
    try:
        data = file.read()
        file.seek(0)
        return pd.read_excel(io.BytesIO(data))
    except Exception as e:
        st.error(f"Error leyendo Excel: {e}")
        return pd.DataFrame()

def leer_imagen_con_ia(file):
    try:
        data = file.read()
        file.seek(0)
        ext  = file.name.split(".")[-1].lower()
        mime = "image/jpeg" if ext in ["jpg", "jpeg"] else f"image/{ext}"
        b64  = base64.b64encode(data).decode("utf-8")
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": "Extrae todo el texto: productos, precios, especificaciones tecnicas."}
            ]}],
            max_tokens=1500
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"Error procesando imagen: {e}")
        return ""

def dividir_texto(texto, size=1500):
    return [texto[i:i+size] for i in range(0, len(texto), size)]

# ---------------------------
# SUPABASE: GUARDAR
# ---------------------------
def borrar_documento(fuente):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM embeddings WHERE fuente = %s", (fuente,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error borrando: {e}")

def guardar_documento(texto, fuente, producto, proveedor):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO embeddings (contenido, fuente, producto, proveedor) VALUES (%s,%s,%s,%s)",
            (texto, fuente, producto, proveedor)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"INSERT ERROR: {e}")
        st.error(f"Error guardando: {e}")
        return False

def guardar_precio(producto, precio, proveedor, moneda="COP"):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO precios (producto, precio, proveedor, moneda)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (producto, proveedor) DO UPDATE
            SET precio = EXCLUDED.precio, moneda = EXCLUDED.moneda
        """, (producto, precio, proveedor, moneda))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"PRECIO ERROR: {e}")
        st.error(f"Error guardando precio: {e}")
        return False

# ---------------------------
# SUPABASE: BUSCAR
# ---------------------------
def quitar_tildes(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

def buscar_documentos(pregunta):
    try:
        stopwords = {"que", "como", "cual", "para", "esto", "esta", "con",
             "los", "las", "del", "una", "por", "cuales", "son",
             "tiene", "hay", "dame", "dime", "cuanto", "cuesta",
             "kilos", "metros", "precio", "vale", "costo"}
        palabras = [p for p in pregunta.split() if len(p) >= 2 and p.lower() not in stopwords]
        if not palabras:
            palabras = pregunta.split()[:3]

        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        todos  = []
        vistos = set()
        for palabra in palabras[:4]:
            for variante in [palabra, quitar_tildes(palabra)]:
                cur.execute(
                    "SELECT * FROM embeddings WHERE contenido ILIKE %s LIMIT 8",
                    (f"%{variante}%",)
                )
                for r in cur.fetchall():
                    if r["id"] not in vistos:
                        vistos.add(r["id"])
                        todos.append(dict(r))
        cur.close()
        conn.close()
        return todos[:10]
    except Exception as e:
        st.warning(f"Error busqueda: {e}")
        return []

def buscar_precios(nombre):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM precios WHERE producto ILIKE %s LIMIT 30",
            (f"%{nombre}%",)
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

def listar_documentos():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, fuente, producto, proveedor, created_at FROM embeddings ORDER BY created_at DESC LIMIT 100")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

def contar_documentos():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM embeddings")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception:
        return 0

# ---------------------------
# IA
# ---------------------------
def responder_con_ia(contexto, pregunta, modo="general"):
    system = (
        "Eres experto en materiales de construccion colombianos. "
        "Usa el contexto para responder con precios, unidades y especificaciones. "
        "Si hay tablas de precios en el contexto, extrae y muestra los valores. "
        "Responde en espanol."
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}
            ],
            max_tokens=1000
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"Error IA: {e}"

# ---------------------------
# SHERWIN-WILLIAMS
# ---------------------------
def scrape_sherwin_producto(url, nombre_producto):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-US,es;q=0.9,en;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            texto = soup.get_text(separator="\n", strip=True)
            if len(texto) > 200:
                return texto[:8000]
    except Exception as e:
        print(f"Scraping error: {e}")
    return f"USAR_CONOCIMIENTO_IA:{nombre_producto}"

def traducir_y_extraer_con_ia(texto, nombre_producto):
    try:
        if texto.startswith("USAR_CONOCIMIENTO_IA:"):
            prompt = f"""Eres un experto en pinturas y recubrimientos de Sherwin-Williams.
Proporciona informacion detallada en espanol sobre el producto '{nombre_producto}'.
Incluye obligatoriamente:
- Descripcion general
- Usos recomendados y superficies compatibles
- Cobertura por galon (m2)
- Tiempo de secado al tacto y tiempo de repinte
- Dilucion recomendada
- Acabado (mate, satinado, brillante)
- Numero de manos recomendadas
- Temperatura de aplicacion
- Limpieza de herramientas
- Advertencias importantes
Si no tienes datos exactos, indicalo claramente."""
        else:
            prompt = f"""Del siguiente texto de una pagina web, extrae TODAS las propiedades
y caracteristicas del producto '{nombre_producto}'.
Traduce todo al espanol de forma clara y organizada.
Incluye: descripcion, usos, superficies compatibles, cobertura, tiempo de secado,
dilucion, acabado, y cualquier especificacion tecnica.

Texto:
{texto}"""

        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"Error IA: {e}"

# ---------------------------
# SCRAPING GENERAL
# ---------------------------
def scrape_precios(url, sel_prod=None, sel_precio=None):
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        productos, precios = [], []
        if sel_prod and sel_precio:
            for p, pr in zip(soup.select(sel_prod), soup.select(sel_precio)):
                productos.append(p.get_text(strip=True))
                precios.append(pr.get_text(strip=True))
        else:
            for m in re.findall(
                r'([A-Za-záéíóúÁÉÍÓÚñÑ][^\n$]{5,60})\s*[\$COP]*\s*([\d\.,]+)',
                soup.get_text()
            )[:30]:
                productos.append(m[0].strip())
                precios.append(m[1].strip())
        return pd.DataFrame({"producto": productos, "precio": precios, "fuente": url})
    except Exception as e:
        st.error(f"Error scraping: {e}")
        return pd.DataFrame()

# ---------------------------
# COTIZADOR
# ---------------------------
def calcular_material(categoria, area=0, largo=0, ancho=0, grosor=0,
                      cobertura=0, precio_unitario=0, rendimiento=1,
                      traslapo=0, num_manos=1):
    if categoria == "pintura":
        area_total = area * num_manos
        galones    = math.ceil(area_total / cobertura) if cobertura > 0 else 0
        return {
            "area_m2": round(area, 2),
            "manos": num_manos,
            "area_total_con_manos": round(area_total, 2),
            "cobertura_por_galon_m2": cobertura,
            "galones_necesarios": galones,
            "precio_unitario": precio_unitario,
            "precio_total": round(galones * precio_unitario, 2),
            "unidad": "galones"
        }
    elif categoria in ["teja", "baldosa", "ladrillo"]:
        au   = largo * ancho
        act  = area * (1 + traslapo)
        cant = math.ceil(act / au) if au > 0 else 0
        return {
            "area_m2": round(area, 2),
            "traslapo_%": traslapo * 100,
            "area_con_traslapo_m2": round(act, 2),
            "area_por_unidad_m2": round(au, 4),
            "cantidad": cant,
            "precio_unitario": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2),
            "unidad": "unidades"
        }
    elif categoria == "cemento":
        vol  = area * grosor
        cant = math.ceil(vol * rendimiento)
        return {
            "area_m2": round(area, 2),
            "grosor_m": grosor,
            "volumen_m3": round(vol, 3),
            "sacos_por_m3": rendimiento,
            "cantidad_sacos": cant,
            "precio_unitario": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2),
            "unidad": "sacos 50kg"
        }
    elif categoria == "acero":
        cant = math.ceil(largo / 12)
        peso = largo * ancho * grosor * 7850 if (ancho > 0 and grosor > 0) else 0
        return {
            "longitud_m": largo,
            "varillas_12m": cant,
            "peso_estimado_kg": round(peso, 2),
            "precio_unitario_varilla": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2),
            "unidad": "varillas"
        }
    return {}

def exportar_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Datos")
    return buf.getvalue()

# ---------------------------
# PINTURAS DB
# ---------------------------
PINTURAS_DB = {
    "Viniltex Sherwin-Williams": {
        "cobertura_m2_galon": 35, "acabado": "mate",
        "dilucion": "10-15% agua", "tiempo_secado_tacto": "30 min",
        "tiempo_repinte": "2 horas", "manos_recomendadas": 2,
        "usos": "Interior - muros - cielorrasos"
    },
    "Pintuco Vinilo Interior": {
        "cobertura_m2_galon": 30, "acabado": "mate",
        "dilucion": "15% agua", "tiempo_secado_tacto": "45 min",
        "tiempo_repinte": "2-3 horas", "manos_recomendadas": 2,
        "usos": "Interior - muros"
    },
    "Corona Exterior Acrilico": {
        "cobertura_m2_galon": 28, "acabado": "satinado",
        "dilucion": "10% agua", "tiempo_secado_tacto": "1 hora",
        "tiempo_repinte": "4 horas", "manos_recomendadas": 2,
        "usos": "Exterior - fachadas"
    }
}

# ==============================================================
# UI PRINCIPAL
# ==============================================================
st.title("Construccion OBRIXA AI")

with st.sidebar:
    st.header("Configuracion")
    moneda_display = st.selectbox("Moneda", ["COP", "USD", "EUR", "MXN"])
    tasas = obtener_tasas()
    st.caption(f"USD a COP: ${tasas.get('COP', 4100):,.0f}")
    st.divider()
    st.subheader("Documentos cargados")
    _df_side = listar_documentos()
    if _df_side.empty:
        st.caption("Sin documentos aun.")
    else:
        _res = _df_side[["fuente", "producto", "proveedor"]].drop_duplicates("fuente")
        st.caption(f"{len(_df_side)} fragmentos - {len(_res)} archivos")
        st.dataframe(_res, use_container_width=True, hide_index=True)
    if st.button("Refrescar lista"):
        st.rerun()

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Cargar Documentos",
    "Consultar",
    "Cotizador",
    "Precios",
    "Precios Web"
])

# ==============================================================
# TAB 1: CARGAR
# ==============================================================
with tab1:
    st.subheader("Carga fichas tecnicas, listas de precios e imagenes")
    st.caption("Formatos: PDF - Excel XLSX - Imagen JPG PNG WEBP")

    col1, col2 = st.columns(2)
    with col1:
        archivo = st.file_uploader(
            "Selecciona archivo",
            type=["pdf", "xlsx", "xls", "png", "jpg", "jpeg", "webp"]
        )
        producto_input  = st.text_input("Producto / categoria")
        proveedor_input = st.text_input("Proveedor")
        modo_carga = st.radio(
            "Tipo de contenido",
            ["Ficha tecnica / texto", "Lista de precios (tabla)"]
        )
    with col2:
        if archivo:
            ext = archivo.name.split(".")[-1].lower()
            st.success(f"Listo: {archivo.name}")
            if ext in ["jpg", "jpeg", "png", "webp"]:
                st.image(archivo, use_container_width=True)
                archivo.seek(0)
                # Borra versión anterior si existe
                borrar_documento(archivo.name)

    if st.button("Procesar y guardar en Supabase", type="primary"):
        if archivo is None:
            st.warning("Selecciona un archivo.")
        elif not producto_input or not proveedor_input:
            st.warning("Escribe producto y proveedor.")
        else:
            ext = archivo.name.split(".")[-1].lower()
            with st.spinner(f"Procesando {archivo.name}..."):

                # ── EXCEL ──
                if ext in ["xlsx", "xls"]:
                    df_ex = leer_excel(archivo)
                    if df_ex.empty:
                        st.error("No se pudo leer el Excel.")
                    else:
                        st.dataframe(df_ex.head(20), use_container_width=True)
                        ok = 0
                        for _, row in df_ex.iterrows():
                            txt = " | ".join(f"{c}: {v}" for c, v in row.items() if pd.notna(v))
                            if guardar_documento(txt, archivo.name, producto_input, proveedor_input):
                                ok += 1
                        cols_l = [c.lower() for c in df_ex.columns]
                        if "producto" in cols_l and "precio" in cols_l:
                            cp = df_ex.columns[cols_l.index("producto")]
                            cv = df_ex.columns[cols_l.index("precio")]
                            for _, row in df_ex.iterrows():
                                try:
                                    pv = str(row[cv]).replace(",", "").replace(".", "").strip()
                                    if pv.isdigit():
                                        guardar_precio(str(row[cp]), int(pv), proveedor_input)
                                except Exception:
                                    pass
                        st.success(f"OK: {ok} filas guardadas desde Excel")

                # ── IMAGEN ──
                elif ext in ["jpg", "jpeg", "png", "webp"]:
                    st.info("Enviando a GPT-4o Vision...")
                    txt_img = leer_imagen_con_ia(archivo)
                    if txt_img:
                        with st.expander("Texto extraido"):
                            st.write(txt_img)
                        chunks = dividir_texto(txt_img)
                        ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input))
                        st.success(f"OK: {ok} fragmentos guardados desde imagen")

                # ── PDF ──
                elif ext == "pdf":
                    if modo_carga == "Lista de precios (tabla)":
                        df_p = extraer_tabla_precios_pdf(archivo)
                        if df_p.empty:
                            st.warning("No se encontraron tablas. Prueba con Ficha tecnica.")
                        else:
                            st.dataframe(df_p, use_container_width=True)
                            for _, row in df_p.iterrows():
                                guardar_precio(row["producto"], row["precio"], proveedor_input)
                            archivo.seek(0)
                            txt_pdf = leer_pdf(archivo)
                            if txt_pdf:
                                chunks = dividir_texto(txt_pdf)
                                ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input))
                                st.success(f"OK: {len(df_p)} precios + {ok} fragmentos guardados")
                            else:
                                st.success(f"OK: {len(df_p)} precios guardados")
                            st.download_button(
                                "Descargar Excel",
                                data=exportar_excel(df_p),
                                file_name=f"precios_{proveedor_input}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            )
                    else:
                        txt = leer_pdf(archivo)
                        if not txt.strip():
                            st.error("No se pudo leer el PDF. Si es escaneado usa la imagen JPG/PNG.")
                        else:
                            chunks = dividir_texto(txt)
                            ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input))
                            st.success(f"OK: {ok} de {len(chunks)} fragmentos guardados")
                            with st.expander("Ver texto extraido"):
                                st.text(txt[:3000])

# ==============================================================
# TAB 2: CONSULTAR
# ==============================================================
with tab2:
    st.subheader("Consulta inteligente sobre tus documentos")

    total = contar_documentos()
    if total > 0:
        st.caption(f"Fragmentos disponibles: {total}")
    else:
        st.info("Aun no hay documentos. Carga archivos en Cargar Documentos.")

    pregunta = st.text_input(
        "Que necesitas saber?",
        placeholder="Precio teja colonial terracota? Caracteristicas Viniltex?"
    )
    modo_ia = st.radio("Modo", ["general", "precios"], horizontal=True)

    if st.button("Consultar", type="primary"):
        if not pregunta:
            st.warning("Escribe una pregunta.")
        elif total == 0:
            st.warning("Carga documentos primero.")
        else:
            with st.spinner("Buscando..."):
                resultados = buscar_documentos(pregunta)
            if not resultados:
                st.warning("No encontre informacion. Intenta con otras palabras.")
            else:
                contexto = "\n\n".join([r["contenido"] for r in resultados])
                col_ctx, col_res = st.columns(2)
                with col_ctx:
                    with st.expander(f"{len(resultados)} fragmentos encontrados"):
                        for r in resultados:
                            st.caption(f"{r.get('producto','')} - {r.get('proveedor','')} - {r.get('fuente','')}")
                            st.text(r["contenido"][:400])
                            st.divider()
                with col_res:
                    st.markdown("### Respuesta")
                    st.write(responder_con_ia(contexto, pregunta, modo_ia))

# ==============================================================
# TAB 3: COTIZADOR
# ==============================================================
with tab3:
    st.subheader("Cotizador de materiales")
    categoria = st.selectbox("Tipo de material", ["pintura", "teja", "baldosa", "cemento", "acero", "ladrillo"])
    largo = ancho = grosor = area = cobertura = rendimiento = traslapo = precio_unitario = 0.0
    num_manos = 1

    col_a, col_b = st.columns(2)
    with col_a:
        if categoria == "pintura":
            area      = st.number_input("Area (m2)", value=20.0, min_value=0.1)
            num_manos = st.number_input("Numero de manos", value=2, min_value=1, max_value=4)
            marca     = st.selectbox("Marca", list(PINTURAS_DB.keys()) + ["Otra"])
            info      = PINTURAS_DB.get(marca, {})
            cobertura = st.number_input(
                f"Cobertura m2/galon (sugerida: {info.get('cobertura_m2_galon', 30)})",
                value=float(info.get("cobertura_m2_galon", 30))
            )
            if info:
                st.info(
                    f"Acabado: {info['acabado']} | Dilucion: {info['dilucion']} | "
                    f"Secado: {info['tiempo_secado_tacto']} | Repinte: {info['tiempo_repinte']} | "
                    f"Usos: {info['usos']}"
                )
            precio_unitario = st.number_input(f"Precio por galon ({moneda_display})", value=80000.0)

        elif categoria in ["teja", "baldosa", "ladrillo"]:
            area     = st.number_input("Area total (m2)", value=30.0)
            largo    = st.number_input("Largo unidad (m)", value=0.40)
            ancho    = st.number_input("Ancho unidad (m)", value=0.20)
            traslapo = st.number_input("Traslapo / desperdicio (%)", value=10.0) / 100
            precio_unitario = st.number_input(f"Precio por unidad ({moneda_display})", value=1500.0)

        elif categoria == "cemento":
            area        = st.number_input("Area (m2)", value=20.0)
            grosor      = st.number_input("Grosor mezcla (m)", value=0.10)
            rendimiento = st.number_input("Sacos 50kg por m3", value=7.0)
            precio_unitario = st.number_input(f"Precio por saco ({moneda_display})", value=35000.0)

        elif categoria == "acero":
            largo   = st.number_input("Longitud total (m)", value=50.0)
            ancho   = st.number_input("Ancho seccion (m)", value=0.012)
            grosor  = st.number_input("Grosor seccion (m)", value=0.012)
            precio_unitario = st.number_input(f"Precio varilla 12m ({moneda_display})", value=45000.0)

    with col_b:
        if st.button("Calcular", type="primary"):
            res = calcular_material(
                categoria=categoria, area=area, largo=largo, ancho=ancho,
                grosor=grosor, cobertura=cobertura, precio_unitario=precio_unitario,
                rendimiento=rendimiento, traslapo=traslapo, num_manos=int(num_manos)
            )
            if res:
                st.markdown("#### Resultado")
                pt = res.get("precio_total", 0)
                if moneda_display != "COP":
                    pt = convertir_precio(pt, "COP", moneda_display)
                for k, v in res.items():
                    if k == "precio_total":
                        st.metric("PRECIO TOTAL", f"{moneda_display} {pt:,.0f}")
                    else:
                        st.write(f"**{k.replace('_',' ').title()}:** {v}")
                df_r = pd.DataFrame([{**res, "material": categoria, "moneda": moneda_display}])
                st.download_button(
                    "Exportar Excel",
                    data=exportar_excel(df_r),
                    file_name=f"cotizacion_{categoria}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

# ==============================================================
# TAB 4: PRECIOS
# ==============================================================
with tab4:
    st.subheader("Consulta de precios guardados")
    buscar_prod = st.text_input("Buscar producto", placeholder="cemento, pintura, varilla...")
    if st.button("Buscar precios"):
        df_p2 = buscar_precios(buscar_prod)
        if df_p2.empty:
            st.info("No hay precios para ese producto.")
        else:
            if moneda_display != "COP":
                df_p2[f"precio_{moneda_display}"] = df_p2["precio"].apply(
                    lambda x: round(convertir_precio(float(x), "COP", moneda_display), 2)
                )
            st.dataframe(df_p2, use_container_width=True)
            st.download_button(
                "Exportar Excel",
                data=exportar_excel(df_p2),
                file_name="precios.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

# ==============================================================
# TAB 5: PRECIOS WEB
# ==============================================================
with tab5:
    st.subheader("Extraccion de precios desde paginas web")
    st.caption("Sitios con JavaScript como Sherwin-Williams pueden requerir URL directa del producto.")

    url_input  = st.text_input("URL", placeholder="https://ferreteriaX.com/precios")
    sel_prod   = st.text_input("Selector CSS producto (opcional)", placeholder=".product-name")
    sel_precio = st.text_input("Selector CSS precio (opcional)",   placeholder=".product-price")

    if st.button("Extraer precios", type="primary"):
        if not url_input:
            st.warning("Escribe una URL.")
        else:
            with st.spinner("Extrayendo..."):
                df_web = scrape_precios(url_input, sel_prod or None, sel_precio or None)
            if not df_web.empty:
                st.success(f"OK: {len(df_web)} productos encontrados")
                st.dataframe(df_web, use_container_width=True)
                prov_web = st.text_input("Proveedor", value="Web")
                if st.button("Guardar en Supabase"):
                    for _, row in df_web.iterrows():
                        p = str(row["precio"]).replace(".", "").replace(",", "").strip()
                        if p.isdigit():
                            guardar_precio(row["producto"], int(p), prov_web)
                    st.success("Guardado.")
                st.download_button(
                    "Exportar Excel",
                    data=exportar_excel(df_web),
                    file_name="precios_web.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

    st.divider()
    st.subheader("Buscar producto especifico en Sherwin-Williams")

    col_sw1, col_sw2 = st.columns(2)
    with col_sw1:
        url_sw = st.text_input(
            "URL directa del producto",
            placeholder="https://www.sherwin-williams.com/painting-contractors/products/minwax-waterbased-wood-staincanada",
            help="Pega la URL exacta de la pagina del producto"
        )
        nombre_sw = st.text_input(
            "Nombre del producto",
            placeholder="Minwax Water-Based Wood Stain"
        )
    with col_sw2:
        guardar_resultado = st.checkbox("Guardar resultado en Supabase")
        proveedor_sw = st.text_input("Proveedor", value="Sherwin-Williams")

    if st.button("Buscar y traducir propiedades", type="primary"):
        if not nombre_sw:
            st.warning("Escribe el nombre del producto.")
        else:
            with st.spinner("Accediendo a la pagina del producto..."):
                url_final = url_sw if url_sw else f"https://www.sherwin-williams.com/es-us/search#q={nombre_sw.replace(' ', '+')}"
                texto_pagina = scrape_sherwin_producto(url_final, nombre_sw)

            if texto_pagina.startswith("USAR_CONOCIMIENTO_IA"):
                st.warning("No se pudo leer la pagina. Respondiendo con conocimiento de GPT...")

            with st.spinner("Extrayendo y traduciendo propiedades..."):
                resultado = traducir_y_extraer_con_ia(texto_pagina, nombre_sw)

            st.markdown("### Propiedades del producto")
            st.write(resultado)

            if guardar_resultado and resultado:
                guardar_documento(
                    resultado,
                    f"SW - {nombre_sw}",
                    nombre_sw,
                    proveedor_sw
                )
                st.success("Guardado en Supabase. Ya puedes consultarlo desde Consultar.")