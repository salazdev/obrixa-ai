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

# ---------------------------
# LOGIN — ACCESO PROTEGIDO
# ---------------------------
APP_USER     = os.getenv("APP_USER", "obrixa_admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "Obrixa2024!")

def login():
    st.markdown("""
        <div style='max-width:400px; margin:80px auto; padding:40px;
             background:#0D1B2A; border-radius:16px; border:1px solid #1E88E5;'>
            <h2 style='color:#F5A623; text-align:center; margin-bottom:8px;'>🏗️ OBRIXA AI</h2>
            <p style='color:#6A90B0; text-align:center; margin-bottom:24px;'>Panel de gestión interno</p>
        </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        st.markdown("### Acceso")
        usuario = st.text_input("Usuario", placeholder="obrixa_admin")
        password = st.text_input("Contraseña", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("Ingresar", use_container_width=True)

        if submitted:
            if usuario == APP_USER and password == APP_PASSWORD:
                st.session_state["autenticado"] = True
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")

def check_auth():
    if "autenticado" not in st.session_state or not st.session_state["autenticado"]:
        login()
        st.stop()

check_auth()

# ---------------------------
# A PARTIR DE AQUÍ — APP PROTEGIDA
# ---------------------------

# ✅ CORRECCIÓN: os.getenv() recibe el NOMBRE de la variable, no el valor
OPENAI_KEY   = os.getenv("OPENAI_KEY")
DB_URL       = os.getenv("DB_URL", "postgresql://postgres.zomdvxmiqqwpxhxklpeb:RxNVnNQo6bWMbbqN@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://zomdvxmiqqwpxhxklpeb.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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

def guardar_documento(texto, fuente, producto, proveedor, tipo="precio"):
    """
    Guarda fragmento en Supabase y genera embedding automáticamente.
    """
    try:
        # Generar embedding vectorial
        try:
            resp = openai_client.embeddings.create(
                model="text-embedding-ada-002",
                input=texto[:8000]
            )
            embedding = resp.data[0].embedding
        except Exception as e:
            print(f"Warning embedding: {e}")
            embedding = None

        conn = get_conn()
        cur  = conn.cursor()
        if embedding:
            cur.execute(
                "INSERT INTO embeddings (contenido, fuente, producto, proveedor, tipo, embedding) VALUES (%s,%s,%s,%s,%s,%s::vector)",
                (texto, fuente, producto, proveedor, tipo, embedding)
            )
        else:
            cur.execute(
                "INSERT INTO embeddings (contenido, fuente, producto, proveedor, tipo) VALUES (%s,%s,%s,%s,%s)",
                (texto, fuente, producto, proveedor, tipo)
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
            INSERT INTO precios_materiales (material, descripcion, precio)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (proveedor.lower(), producto, precio))
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

def buscar_documentos(pregunta: str, tipo: str = None):
    """Búsqueda semántica con pgvector — encuentra por significado, no solo palabras exactas."""
    try:
        # Generar embedding de la pregunta
        resp = openai_client.embeddings.create(
            model="text-embedding-ada-002",
            input=pregunta[:8000]
        )
        query_vector = resp.data[0].embedding

        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if tipo:
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings
                WHERE embedding IS NOT NULL AND tipo = %s
                ORDER BY embedding <=> %s::vector
                LIMIT 10
            """, (query_vector, tipo, query_vector))
        else:
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT 10
            """, (query_vector, query_vector))

        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        st.warning(f"Error busqueda semantica: {e}")
        # Fallback a búsqueda por palabras clave si falla pgvector
        return buscar_documentos_keywords(pregunta, tipo)

def buscar_documentos_keywords(pregunta: str, tipo: str = None):
    """Búsqueda por palabras clave — fallback cuando pgvector no está disponible."""
    try:
        stopwords = {"que", "como", "cual", "para", "esto", "esta", "con",
             "los", "las", "del", "una", "por", "cuales", "son",
             "tiene", "hay", "dame", "dime", "cuanto", "cuesta",
             "kilos", "metros", "precio", "vale", "costo"}
        palabras = [p for p in pregunta.split() if len(p) >= 2 and p.lower() not in stopwords]
        if not palabras:
            palabras = pregunta.split()[:3]

        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        todos = []
        vistos = set()
        for palabra in palabras[:4]:
            for variante in [palabra, quitar_tildes(palabra)]:
                if tipo:
                    cur.execute(
                        "SELECT * FROM embeddings WHERE contenido ILIKE %s AND tipo = %s LIMIT 8",
                        (f"%{variante}%", tipo)
                    )
                else:
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
        st.warning(f"Error busqueda keywords: {e}")
        return []

def buscar_todos_fichas(pregunta: str = None, limite: int = 60):
    """Búsqueda semántica en todas las fichas técnicas."""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if pregunta:
            # Búsqueda semántica ordenada por similitud
            resp = openai_client.embeddings.create(
                model="text-embedding-ada-002",
                input=pregunta[:8000]
            )
            query_vector = resp.data[0].embedding
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings
                WHERE tipo = 'ficha_tecnica' AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (query_vector, query_vector, limite))
        else:
            cur.execute(
                "SELECT contenido, producto, fuente FROM embeddings WHERE tipo = 'ficha_tecnica' LIMIT %s",
                (limite,)
            )

        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        st.warning(f"Error busqueda fichas: {e}")
        return []

def es_pregunta_recomendacion(pregunta: str) -> bool:
    """Detecta si el usuario busca una recomendación de producto."""
    keywords = [
        "que pintura", "qué pintura", "cual pintura", "cuál pintura",
        "recomienda", "recomendas", "para pintar", "para proteger",
        "mejor para", "cual usar", "cuál usar", "que usar", "qué usar",
        "que producto", "qué producto", "para mamposteria", "para fachada",
        "para metal", "para madera", "para piso", "para techo",
        "para exterior", "para interior", "para humedad", "anticorrosivo",
        "impermeabilizar", "sellador", "que me sirve", "qué me sirve"
    ]
    t = pregunta.lower()
    return any(k in t for k in keywords)

def buscar_precios(nombre):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM precios_materiales WHERE descripcion ILIKE %s LIMIT 30",
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
        cur.execute("SELECT id, fuente, producto, proveedor, tipo, created_at FROM embeddings ORDER BY created_at DESC LIMIT 100")
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
    if modo == "ficha":
        system = (
            "Eres experto en pinturas y materiales de construccion colombianos de Sherwin-Williams. "
            "Presenta la ficha tecnica del producto de forma clara. "
            "Incluye: usos recomendados, superficies compatibles, rendimiento en m2 por galon, "
            "tiempo de secado, dilucion, numero de manos y advertencias importantes. "
            "Responde en espanol."
        )
    elif modo == "recomendacion":
        system = (
            "Eres un asesor experto en pinturas Sherwin-Williams para Colombia. "
            "El cliente te hace una pregunta sobre que producto usar. "
            "Analiza TODAS las fichas tecnicas disponibles en el contexto y recomienda "
            "el producto MAS ADECUADO para la necesidad del cliente. "
            "SIEMPRE menciona el nombre exacto del producto recomendado. "
            "Explica por que ese producto es el ideal, sus caracteristicas clave, "
            "rendimiento en m2 por galon, y como aplicarlo. "
            "Si hay mas de un producto adecuado, mencionalos en orden de recomendacion. "
            "Responde en espanol de forma clara y profesional."
        )
    else:
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
# COTIZADOR — Solo Tejas y Pinturas SW
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
    elif categoria == "teja":
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
    return {}

def exportar_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Datos")
    return buf.getvalue()

# ---------------------------
# BASE DE DATOS PINTURAS SW
# ---------------------------
PINTURAS_SW = {
    "SuperPaint Exterior": {
        "cobertura_m2_galon": 33, "acabado": "mate/satinado/semibrillante",
        "dilucion": "hasta 10% agua", "tiempo_secado_tacto": "2 horas",
        "tiempo_repinte": "4 horas", "manos_recomendadas": 2,
        "usos": "Exterior - fachadas - madera - estuco - fibrocemento - ladrillo",
        "superficie": "madera, ladrillo, estuco, fibrocemento, acero, galvanizado, OSB, PVC"
    },
    "SuperPaint Interior": {
        "cobertura_m2_galon": 33, "acabado": "mate/satinado/semibrillante",
        "dilucion": "hasta 10% agua", "tiempo_secado_tacto": "30 minutos",
        "tiempo_repinte": "2-4 horas", "manos_recomendadas": 2,
        "usos": "Interior - muros - cielorrasos - drywall",
        "superficie": "drywall, estuco, ladrillo, fibrocemento, OSB, cielos rasos"
    },
    "Elastomerica": {
        "cobertura_m2_galon": 17, "acabado": "mate",
        "dilucion": "no dilуir", "tiempo_secado_tacto": "4 horas",
        "tiempo_repinte": "24 horas", "manos_recomendadas": 2,
        "usos": "Impermeabilizante - fachadas - techos - exterior",
        "superficie": "concreto, estuco, mamposteria, fibrocemento"
    },
    "Otra Sherwin-Williams": {
        "cobertura_m2_galon": 32, "acabado": "variable",
        "dilucion": "segun ficha", "tiempo_secado_tacto": "variable",
        "tiempo_repinte": "variable", "manos_recomendadas": 2,
        "usos": "Consultar ficha tecnica",
        "superficie": "Consultar ficha tecnica"
    }
}

# ==============================================================
# UI PRINCIPAL
# ==============================================================
st.title("🏗️ OBRIXA AI — Panel de gestión")

with st.sidebar:
    st.header("⚙️ Configuracion")
    moneda_display = st.selectbox("Moneda", ["COP", "USD", "EUR", "MXN"])
    tasas = obtener_tasas()
    st.caption(f"USD → COP: ${tasas.get('COP', 4100):,.0f}")
    st.divider()
    # Botón cerrar sesión
    if st.button("🔒 Cerrar sesión"):
        st.session_state["autenticado"] = False
        st.rerun()
    st.divider()
    st.subheader("📄 Documentos cargados")
    _df_side = listar_documentos()
    if _df_side.empty:
        st.caption("Sin documentos aun.")
    else:
        _res = _df_side[["fuente", "producto", "proveedor", "tipo"]].drop_duplicates("fuente")
        fichas = len(_res[_res["tipo"] == "ficha_tecnica"]) if "tipo" in _res.columns else 0
        precios = len(_res[_res["tipo"] == "precio"]) if "tipo" in _res.columns else 0
        st.caption(f"{len(_df_side)} fragmentos | {fichas} fichas | {precios} listas de precios")
        st.dataframe(_res, use_container_width=True, hide_index=True)
    if st.button("🔄 Refrescar lista"):
        st.rerun()

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📁 Cargar Documentos",
    "🔍 Consultar",
    "🧮 Cotizador",
    "💰 Precios",
    "🌐 Precios Web"
])

# ==============================================================
# TAB 1: CARGAR
# ==============================================================
with tab1:
    st.subheader("Carga fichas tecnicas y listas de precios")
    st.caption("Formatos soportados: PDF · Excel XLSX · Imagen JPG/PNG/WEBP")

    col1, col2 = st.columns(2)
    with col1:
        archivo = st.file_uploader(
            "Selecciona archivo",
            type=["pdf", "xlsx", "xls", "png", "jpg", "jpeg", "webp"]
        )
        producto_input  = st.text_input("Producto / categoria", placeholder="SuperPaint Exterior")
        proveedor_input = st.text_input("Proveedor", placeholder="Sherwin-Williams")

        # ✅ Selector de tipo para clasificar correctamente
        tipo_contenido = st.radio(
            "Tipo de contenido",
            ["ficha_tecnica", "precio"],
            format_func=lambda x: "📋 Ficha técnica" if x == "ficha_tecnica" else "💰 Lista de precios"
        )

    with col2:
        if archivo:
            ext = archivo.name.split(".")[-1].lower()
            st.success(f"✅ Archivo listo: {archivo.name}")
            if ext in ["jpg", "jpeg", "png", "webp"]:
                st.image(archivo, use_container_width=True)
                archivo.seek(0)
                borrar_documento(archivo.name)

    if st.button("⬆️ Procesar y guardar en Supabase", type="primary"):
        if archivo is None:
            st.warning("Selecciona un archivo.")
        elif not producto_input or not proveedor_input:
            st.warning("Escribe producto y proveedor.")
        else:
            ext = archivo.name.split(".")[-1].lower()
            with st.spinner(f"Procesando {archivo.name}..."):

                if ext in ["xlsx", "xls"]:
                    df_ex = leer_excel(archivo)
                    if df_ex.empty:
                        st.error("No se pudo leer el Excel.")
                    else:
                        st.dataframe(df_ex.head(20), use_container_width=True)
                        ok = 0
                        for _, row in df_ex.iterrows():
                            txt = " | ".join(f"{c}: {v}" for c, v in row.items() if pd.notna(v))
                            if guardar_documento(txt, archivo.name, producto_input, proveedor_input, tipo_contenido):
                                ok += 1
                        st.success(f"✅ {ok} filas guardadas desde Excel")

                elif ext in ["jpg", "jpeg", "png", "webp"]:
                    st.info("Enviando a GPT-4o Vision...")
                    txt_img = leer_imagen_con_ia(archivo)
                    if txt_img:
                        with st.expander("Texto extraido"):
                            st.write(txt_img)
                        chunks = dividir_texto(txt_img)
                        ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, tipo_contenido))
                        st.success(f"✅ {ok} fragmentos guardados desde imagen")

                elif ext == "pdf":
                    if tipo_contenido == "precio":
                        df_p = extraer_tabla_precios_pdf(archivo)
                        if not df_p.empty:
                            st.dataframe(df_p, use_container_width=True)
                            for _, row in df_p.iterrows():
                                guardar_precio(row["producto"], row["precio"], proveedor_input)
                        archivo.seek(0)
                        txt_pdf = leer_pdf(archivo)
                        if txt_pdf:
                            chunks = dividir_texto(txt_pdf)
                            ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, "precio"))
                            st.success(f"✅ {len(df_p) if not df_p.empty else 0} precios + {ok} fragmentos guardados")
                    else:
                        txt = leer_pdf(archivo)
                        if not txt.strip():
                            # ── PDF escaneado — usar GPT-4o Vision automáticamente ──
                            st.warning("⚠️ PDF escaneado detectado. Procesando con Vision IA...")
                            try:
                                import fitz  # PyMuPDF
                                archivo.seek(0)
                                pdf_bytes = archivo.read()
                                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                                texto_total = ""
                                for num_pagina, pagina in enumerate(doc):
                                    st.info(f"Procesando página {num_pagina + 1} de {len(doc)}...")
                                    # Convertir página a imagen
                                    mat = fitz.Matrix(2, 2)  # zoom 2x para mejor calidad
                                    pix = pagina.get_pixmap(matrix=mat)
                                    img_bytes = pix.tobytes("jpeg")
                                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                                    # Enviar a GPT-4o Vision
                                    resp = openai_client.chat.completions.create(
                                        model="gpt-4o-mini",
                                        messages=[{"role": "user", "content": [
                                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                            {"type": "text", "text": f"Extrae TODO el texto de esta ficha tecnica de {producto_input}. Incluye: nombre del producto, caracteristicas, usos, especificaciones tecnicas, rendimiento, tiempos de secado, superficies compatibles. No omitas ninguna informacion."}
                                        ]}],
                                        max_tokens=2000
                                    )
                                    texto_pagina = resp.choices[0].message.content
                                    texto_total += f"\n\n--- Pagina {num_pagina + 1} ---\n{texto_pagina}"

                                if texto_total.strip():
                                    with st.expander("Ver texto extraido por Vision IA"):
                                        st.text(texto_total[:3000])
                                    chunks = dividir_texto(texto_total)
                                    ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, tipo_contenido))
                                    st.success(f"✅ {ok} fragmentos guardados desde PDF escaneado via Vision IA")
                                else:
                                    st.error("No se pudo extraer texto del PDF escaneado.")
                            except ImportError:
                                st.error("Instala PyMuPDF: pip install pymupdf")
                            except Exception as e:
                                st.error(f"Error procesando PDF escaneado: {e}")
                        else:
                            chunks = dividir_texto(txt)
                            ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, "ficha_tecnica"))
                            st.success(f"✅ {ok} de {len(chunks)} fragmentos guardados como ficha_tecnica")
                            with st.expander("Ver texto extraido"):
                                st.text(txt[:3000])

# ==============================================================
# TAB 2: CONSULTAR
# ==============================================================
with tab2:
    st.subheader("🔍 Consulta inteligente sobre tus documentos")

    total = contar_documentos()
    if total > 0:
        st.caption(f"Fragmentos disponibles en Supabase: {total}")
    else:
        st.info("Aun no hay documentos. Carga archivos en la pestaña Cargar Documentos.")

    col_q1, col_q2 = st.columns([3, 1])
    with col_q1:
        pregunta = st.text_input(
            "¿Que necesitas saber?",
            placeholder="¿Qué pintura uso para mampostería exterior? ¿Cuántos m2 rinde el KEM LÁTEX?"
        )
    with col_q2:
        tipo_busqueda = st.selectbox("Tipo", ["Todos", "ficha_tecnica", "precio"])
        modo_ia = st.radio("Modo IA", ["general", "ficha", "recomendacion"], horizontal=True)

    if st.button("🔍 Consultar", type="primary"):
        if not pregunta:
            st.warning("Escribe una pregunta.")
        elif total == 0:
            st.warning("Carga documentos primero.")
        else:
            tipo_filtro = None if tipo_busqueda == "Todos" else tipo_busqueda

            # ── Detección automática de preguntas de recomendación ──
            es_recomendacion = es_pregunta_recomendacion(pregunta) or modo_ia == "recomendacion"

            with st.spinner("Buscando en Supabase..."):
                if es_recomendacion:
                    # Búsqueda semántica en todas las fichas
                    resultados = buscar_todos_fichas(pregunta=pregunta, limite=15)
                    modo_usado = "recomendacion"
                    st.info("🎯 Modo recomendación — búsqueda semántica en todas las fichas")
                else:
                    resultados = buscar_documentos(pregunta, tipo=tipo_filtro)
                    modo_usado = modo_ia

            if not resultados:
                st.warning("No encontre informacion. Intenta con otras palabras.")
            else:
                contexto = "\n\n".join([r["contenido"] for r in resultados])
                col_ctx, col_res = st.columns(2)
                with col_ctx:
                    with st.expander(f"📄 {len(resultados)} fragmentos consultados"):
                        for r in resultados:
                            st.caption(f"📌 {r.get('producto','')} | {r.get('proveedor','')} | Tipo: {r.get('tipo','')}")
                            st.text(r["contenido"][:400])
                            st.divider()
                with col_res:
                    st.markdown("### 💡 Respuesta")
                    st.write(responder_con_ia(contexto, pregunta, modo_usado))

# ==============================================================
# TAB 3: COTIZADOR — Solo Tejas y Pinturas SW
# ==============================================================
with tab3:
    st.subheader("🧮 Cotizador — Tejas y Pinturas Sherwin-Williams")

    categoria = st.selectbox("Tipo de material", ["pintura", "teja"])

    col_a, col_b = st.columns(2)
    with col_a:
        largo = ancho = area = cobertura = precio_unitario = traslapo = 0.0
        num_manos = 2

        if categoria == "pintura":
            marca = st.selectbox("Producto Sherwin-Williams", list(PINTURAS_SW.keys()))
            info  = PINTURAS_SW.get(marca, {})

            if info:
                st.info(
                    f"**Superficies:** {info.get('superficie','')}\n\n"
                    f"**Acabado:** {info.get('acabado','')} | "
                    f"**Dilucion:** {info.get('dilucion','')} | "
                    f"**Secado:** {info.get('tiempo_secado_tacto','')} | "
                    f"**Repinte:** {info.get('tiempo_repinte','')} | "
                    f"**Manos sugeridas:** {info.get('manos_recomendadas','')}"
                )

            area = st.number_input("Area a pintar (m2)", value=20.0, min_value=0.1)
            num_manos = st.number_input(
                f"Numero de manos (sugeridas: {info.get('manos_recomendadas', 2)})",
                value=int(info.get("manos_recomendadas", 2)),
                min_value=1, max_value=4
            )
            cobertura = st.number_input(
                f"Rendimiento m2/galon (referencia: {info.get('cobertura_m2_galon', 32)})",
                value=float(info.get("cobertura_m2_galon", 32))
            )
            precio_unitario = st.number_input(f"Precio por galon ({moneda_display})", value=80000.0)

            # Consultar fichas técnicas del producto seleccionado
            if st.button("📋 Ver ficha técnica completa"):
                with st.spinner("Buscando ficha tecnica..."):
                    resultados_ficha = buscar_documentos(marca, tipo="ficha_tecnica")
                if resultados_ficha:
                    contexto_ficha = "\n\n".join([r["contenido"] for r in resultados_ficha])
                    st.markdown("### 📋 Ficha Técnica")
                    st.write(responder_con_ia(contexto_ficha, f"ficha tecnica de {marca}", "ficha"))
                else:
                    st.info(f"No hay ficha tecnica cargada para {marca}. Cargala en la pestaña Cargar Documentos.")

        elif categoria == "teja":
            area     = st.number_input("Area total del techo (m2)", value=30.0)
            largo    = st.number_input("Largo de la teja (m)", value=11.80)
            ancho    = st.number_input("Ancho de la teja (m)", value=1.075)
            traslapo = st.number_input("Traslapo / desperdicio (%)", value=10.0) / 100
            precio_unitario = st.number_input(f"Precio por teja ({moneda_display})", value=0.0)
            st.caption("Referencia: Teja UPVC/Policarbonato JMUNDIAL — largo 11.80m x ancho 1.075m")

    with col_b:
        if st.button("🧮 Calcular", type="primary"):
            res = calcular_material(
                categoria=categoria, area=area, largo=largo, ancho=ancho,
                cobertura=cobertura, precio_unitario=precio_unitario,
                traslapo=traslapo, num_manos=int(num_manos)
            )
            if res:
                st.markdown("#### 📊 Resultado")
                pt = res.get("precio_total", 0)
                if moneda_display != "COP":
                    pt = convertir_precio(pt, "COP", moneda_display)
                for k, v in res.items():
                    if k == "precio_total":
                        st.metric("PRECIO TOTAL ESTIMADO", f"{moneda_display} {pt:,.0f}")
                    else:
                        st.write(f"**{k.replace('_',' ').title()}:** {v}")

                df_r = pd.DataFrame([{**res, "material": categoria, "producto": marca if categoria == "pintura" else "Teja", "moneda": moneda_display}])
                st.download_button(
                    "📥 Exportar Excel",
                    data=exportar_excel(df_r),
                    file_name=f"cotizacion_{categoria}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

# ==============================================================
# TAB 4: PRECIOS
# ==============================================================
with tab4:
    st.subheader("💰 Consulta de precios guardados")
    buscar_prod = st.text_input("Buscar producto", placeholder="teja, superpaint, elastomerica...")
    if st.button("🔍 Buscar precios"):
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
                "📥 Exportar Excel",
                data=exportar_excel(df_p2),
                file_name="precios.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

# ==============================================================
# TAB 5: SHERWIN-WILLIAMS
# ==============================================================
with tab5:
    st.subheader("🎨 Buscar producto Sherwin-Williams")
    st.caption("Busca informacion de cualquier producto SW directamente desde su sitio web.")

    col_sw1, col_sw2 = st.columns(2)
    with col_sw1:
        url_sw = st.text_input(
            "URL directa del producto (opcional)",
            placeholder="https://www.sherwin-williams.com/painting-contractors/products/..."
        )
        nombre_sw = st.text_input(
            "Nombre del producto",
            placeholder="SuperPaint Exterior, Duration, Emerald..."
        )
    with col_sw2:
        guardar_resultado = st.checkbox("Guardar resultado en Supabase como ficha_tecnica")
        proveedor_sw = st.text_input("Proveedor", value="Sherwin-Williams")

    if st.button("🔍 Buscar y traducir propiedades", type="primary"):
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

            st.markdown("### 📋 Propiedades del producto")
            st.write(resultado)

            if guardar_resultado and resultado:
                guardar_documento(
                    resultado,
                    f"SW - {nombre_sw}",
                    nombre_sw,
                    proveedor_sw,
                    "ficha_tecnica"
                )
                st.success("✅ Guardado en Supabase como ficha_tecnica. Ya puedes consultarlo desde Consultar.")
