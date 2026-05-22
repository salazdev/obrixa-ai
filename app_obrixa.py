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
# LOGIN
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
        usuario  = st.text_input("Usuario", placeholder="obrixa_admin")
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
# CONFIG
# ---------------------------
OPENAI_KEY   = os.getenv("OPENAI_KEY")
DB_URL       = os.getenv("DB_URL", "postgresql://postgres.zomdvxmiqqwpxhxklpeb:P3lXGNCb4jYlo4rZ@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
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
        data = file.read(); file.seek(0)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t: texto += t + "\n"
    except Exception as e:
        st.error(f"Error leyendo PDF: {e}")
    return texto

def extraer_tabla_precios_pdf(file):
    productos, precios = [], []
    try:
        data = file.read(); file.seek(0)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pagina in pdf.pages:
                for tabla in (pagina.extract_tables() or []):
                    for fila in tabla:
                        if not fila: continue
                        fc = [c for c in fila if c not in (None, "", " ")]
                        if len(fc) < 2: continue
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
        data = file.read(); file.seek(0)
        return pd.read_excel(io.BytesIO(data))
    except Exception as e:
        st.error(f"Error leyendo Excel: {e}")
        return pd.DataFrame()

def leer_imagen_con_ia(file):
    try:
        data = file.read(); file.seek(0)
        ext  = file.name.split(".")[-1].lower()
        mime = "image/jpeg" if ext in ["jpg","jpeg"] else f"image/{ext}"
        b64  = base64.b64encode(data).decode("utf-8")
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":[
                {"type":"image_url","image_url":{"url":f"data:{mime};base64,{b64}"}},
                {"type":"text","text":"Extrae todo el texto: productos, precios, especificaciones tecnicas."}
            ]}], max_tokens=1500)
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
        conn = get_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM embeddings WHERE fuente = %s", (fuente,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"Error borrando: {e}")

def guardar_documento(texto, fuente, producto, proveedor, tipo="precio"):
    try:
        try:
            resp = openai_client.embeddings.create(model="text-embedding-ada-002", input=texto[:8000])
            embedding = resp.data[0].embedding
        except Exception as e:
            print(f"Warning embedding: {e}"); embedding = None
        conn = get_conn(); cur = conn.cursor()
        if embedding:
            cur.execute(
                "INSERT INTO embeddings (contenido, fuente, producto, proveedor, tipo, embedding) VALUES (%s,%s,%s,%s,%s,%s::vector)",
                (texto, fuente, producto, proveedor, tipo, embedding))
        else:
            cur.execute(
                "INSERT INTO embeddings (contenido, fuente, producto, proveedor, tipo) VALUES (%s,%s,%s,%s,%s)",
                (texto, fuente, producto, proveedor, tipo))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        print(f"INSERT ERROR: {e}"); st.error(f"Error guardando: {e}"); return False

def guardar_precio(producto, precio, proveedor, moneda="COP"):
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO precios_materiales (material, descripcion, precio)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (proveedor.lower(), producto, precio))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        print(f"PRECIO ERROR: {e}"); st.error(f"Error guardando precio: {e}"); return False

# ---------------------------
# SUPABASE: BUSCAR
# ---------------------------
def quitar_tildes(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def buscar_documentos(pregunta: str, tipo: str = None):
    try:
        resp = openai_client.embeddings.create(model="text-embedding-ada-002", input=pregunta[:8000])
        query_vector = resp.data[0].embedding
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if tipo:
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings WHERE embedding IS NOT NULL AND tipo = %s
                ORDER BY embedding <=> %s::vector LIMIT 10
            """, (query_vector, tipo, query_vector))
        else:
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector LIMIT 10
            """, (query_vector, query_vector))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close(); return rows
    except Exception as e:
        st.warning(f"Error busqueda semantica: {e}")
        return buscar_documentos_keywords(pregunta, tipo)

def buscar_documentos_keywords(pregunta: str, tipo: str = None):
    try:
        stopwords = {"que","como","cual","para","esto","esta","con","los","las","del","una","por"}
        palabras = [p for p in pregunta.split() if len(p) >= 2 and p.lower() not in stopwords]
        if not palabras: palabras = pregunta.split()[:3]
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        todos = []; vistos = set()
        for palabra in palabras[:4]:
            for variante in [palabra, quitar_tildes(palabra)]:
                if tipo:
                    cur.execute("SELECT * FROM embeddings WHERE contenido ILIKE %s AND tipo = %s LIMIT 8", (f"%{variante}%", tipo))
                else:
                    cur.execute("SELECT * FROM embeddings WHERE contenido ILIKE %s LIMIT 8", (f"%{variante}%",))
                for r in cur.fetchall():
                    if r["id"] not in vistos:
                        vistos.add(r["id"]); todos.append(dict(r))
        cur.close(); conn.close(); return todos[:10]
    except Exception as e:
        st.warning(f"Error busqueda keywords: {e}"); return []

def buscar_todos_fichas(pregunta: str = None, limite: int = 60):
    try:
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if pregunta:
            resp = openai_client.embeddings.create(model="text-embedding-ada-002", input=pregunta[:8000])
            query_vector = resp.data[0].embedding
            cur.execute("""
                SELECT id, contenido, fuente, producto, proveedor, tipo,
                       1 - (embedding <=> %s::vector) AS similitud
                FROM embeddings WHERE tipo = 'ficha_tecnica' AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector LIMIT %s
            """, (query_vector, query_vector, limite))
        else:
            cur.execute("SELECT contenido, producto, fuente FROM embeddings WHERE tipo = 'ficha_tecnica' LIMIT %s", (limite,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close(); return rows
    except Exception as e:
        st.warning(f"Error busqueda fichas: {e}"); return []

def es_pregunta_recomendacion(pregunta: str) -> bool:
    keywords = ["que pintura","qué pintura","cual pintura","cuál pintura","recomienda","recomendas",
                "para pintar","para proteger","mejor para","cual usar","cuál usar","que usar","qué usar",
                "que producto","qué producto","para mamposteria","para fachada","para metal","para madera",
                "para piso","para techo","para exterior","para interior","para humedad","anticorrosivo",
                "impermeabilizar","sellador","que me sirve","qué me sirve"]
    t = pregunta.lower()
    return any(k in t for k in keywords)

def buscar_precios(nombre):
    try:
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM precios_materiales WHERE descripcion ILIKE %s LIMIT 30", (f"%{nombre}%",))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

def listar_productos_sw():
    try:
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT DISTINCT descripcion, precio_g1, precio_g2, precio_g3, precio_g4
            FROM precios_materiales
            WHERE material = 'pintura' AND proveedor = 'Sherwin-Williams' AND precio_g1 > 0
            ORDER BY descripcion
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close(); return rows
    except Exception as e:
        print(f"Error listando SW: {e}"); return []

def listar_documentos():
    try:
        conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, fuente, producto, proveedor, tipo, created_at FROM embeddings ORDER BY created_at DESC LIMIT 100")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

def contar_documentos():
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM embeddings")
        count = cur.fetchone()[0]; cur.close(); conn.close(); return count
    except Exception:
        return 0


# ---------------------------
# IA
# ---------------------------
def responder_con_ia(contexto, pregunta, modo="general"):
    if modo == "ficha":
        system = ("Eres experto en pinturas y materiales de construccion colombianos de Sherwin-Williams. "
                  "Presenta la ficha tecnica del producto de forma clara. Incluye: usos recomendados, "
                  "superficies compatibles, rendimiento en m2 por galon, tiempo de secado, dilucion, "
                  "numero de manos y advertencias importantes. Responde en espanol.")
    elif modo == "recomendacion":
        system = ("Eres un asesor experto en pinturas Sherwin-Williams para Colombia. "
                  "Analiza TODAS las fichas tecnicas disponibles y recomienda el producto MAS ADECUADO. "
                  "SIEMPRE menciona el nombre exacto del producto recomendado. Responde en espanol.")
    else:
        system = ("Eres experto en materiales de construccion colombianos. "
                  "Usa el contexto para responder con precios, unidades y especificaciones. Responde en espanol.")
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system},
                      {"role":"user","content":f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}],
            max_tokens=1000)
        return resp.choices[0].message.content
    except Exception as e:
        return f"Error IA: {e}"

# ---------------------------
# SHERWIN-WILLIAMS SCRAPING
# ---------------------------
def scrape_sherwin_producto(url, nombre_producto):
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup(["script","style","nav","footer","header"]): tag.decompose()
            texto = soup.get_text(separator="\n", strip=True)
            if len(texto) > 200: return texto[:8000]
    except Exception as e:
        print(f"Scraping error: {e}")
    return f"USAR_CONOCIMIENTO_IA:{nombre_producto}"

def traducir_y_extraer_con_ia(texto, nombre_producto):
    try:
        if texto.startswith("USAR_CONOCIMIENTO_IA:"):
            prompt = f"Eres experto en pinturas Sherwin-Williams. Proporciona informacion detallada en espanol sobre '{nombre_producto}'. Incluye: descripcion, usos, cobertura por galon, tiempo de secado, dilucion, acabado, numero de manos, temperatura de aplicacion, advertencias."
        else:
            prompt = f"Del siguiente texto, extrae TODAS las propiedades de '{nombre_producto}'. Traduce al espanol.\n\nTexto:\n{texto}"
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], max_tokens=1200)
        return resp.choices[0].message.content
    except Exception as e:
        return f"Error IA: {e}"

def scrape_precios(url, sel_prod=None, sel_precio=None):
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10); r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        productos, precios = [], []
        if sel_prod and sel_precio:
            for p, pr in zip(soup.select(sel_prod), soup.select(sel_precio)):
                productos.append(p.get_text(strip=True)); precios.append(pr.get_text(strip=True))
        else:
            for m in re.findall(r'([A-Za-záéíóúÁÉÍÓÚñÑ][^\n$]{5,60})\s*[\$COP]*\s*([\d\.,]+)', soup.get_text())[:30]:
                productos.append(m[0].strip()); precios.append(m[1].strip())
        return pd.DataFrame({"producto": productos, "precio": precios, "fuente": url})
    except Exception as e:
        st.error(f"Error scraping: {e}"); return pd.DataFrame()

# ---------------------------
# COTIZADOR
# ---------------------------
def calcular_material(categoria, area=0, largo=0, ancho=0, grosor=0,
                      cobertura=0, precio_unitario=0, rendimiento=1, traslapo=0, num_manos=1):
    if categoria == "pintura":
        area_total = area * num_manos
        galones = math.ceil(area_total / cobertura) if cobertura > 0 else 0
        return {"area_m2": round(area,2), "manos": num_manos,
                "area_total_con_manos": round(area_total,2), "cobertura_por_galon_m2": cobertura,
                "galones_necesarios": galones, "precio_unitario": precio_unitario,
                "precio_total": round(galones * precio_unitario, 2), "unidad": "galones"}
    elif categoria == "teja":
        au = largo * ancho; act = area * (1 + traslapo)
        cant = math.ceil(act / au) if au > 0 else 0
        return {"area_m2": round(area,2), "traslapo_%": traslapo*100,
                "area_con_traslapo_m2": round(act,2), "area_por_unidad_m2": round(au,4),
                "cantidad": cant, "precio_unitario": precio_unitario,
                "precio_total": round(cant * precio_unitario, 2), "unidad": "unidades"}
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
        "dilucion": "no diluir", "tiempo_secado_tacto": "4 horas",
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

# ---------------------------
# BASE DE DATOS COLORES RAL
# ---------------------------
COLORES_RAL = {
    "RAL1000": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL1001": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1002": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1003": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1004": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1005": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1006": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1007": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1011": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1012": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1013": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL1014": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL1015": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL1016": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1017": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1018": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1019": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1020": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1021": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1023": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1024": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL1027": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1028": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1032": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1033": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL1034": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2000": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL2001": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL2002": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL2003": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2004": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2008": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2009": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2010": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2011": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL2012": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3000": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3001": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3002": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3003": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3004": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3005": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3007": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3009": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3011": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3012": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL3013": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3014": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3015": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL3016": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL3017": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3018": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3020": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3022": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3027": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL3031": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4001": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4002": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL4003": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4004": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4005": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4006": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4007": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4008": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL4009": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL4010": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL5000": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5001": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5002": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL5003": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5004": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5005": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL5007": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL5008": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL5009": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5010": {"grupo": 4, "base": "UD", "observaciones": "CAMBIA A GP 3 CON TINTE GIS"},
    "RAL5011": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5012": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5013": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5014": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL5015": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5017": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5018": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL5019": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5020": {"grupo": 4, "base": "UD", "observaciones": "CAMBIA A GP 3 CON TINTE GIS"},
    "RAL5021": {"grupo": 3, "base": "UD", "observaciones": "CAMBIA A GP 2 CON TINTE GIS"},
    "RAL5022": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL5023": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL5024": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL6000": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6001": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6002": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6003": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6004": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6005": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6006": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6007": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6008": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6009": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6010": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6011": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6012": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6013": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6014": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6015": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6016": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6017": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6018": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6019": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL6020": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6021": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6022": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6024": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6025": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6026": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6027": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL6028": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6029": {"grupo": 4, "base": "UD", "observaciones": ""},
    "RAL6032": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL6033": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL6034": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7000": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7001": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7002": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7003": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7004": {"grupo": 1, "base": "UD", "observaciones": ""},
    "RAL7005": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7006": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7008": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7009": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7010": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7011": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7012": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7013": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7015": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7016": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7021": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7022": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7023": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7024": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7026": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7030": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7031": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7032": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7033": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7034": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7035": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7036": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7037": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7038": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7039": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL7040": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7042": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7043": {"grupo": 2, "base": "WHITE", "observaciones": ""},
    "RAL7044": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7045": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7046": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL7047": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL8000": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8001": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8002": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8003": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8004": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8007": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8008": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8011": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8012": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8014": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8015": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8016": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8017": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8019": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8022": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8023": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL8024": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8025": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL8028": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL9001": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL9002": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL9004": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL9005": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL9006": {"grupo": 2, "base": "WHITE", "observaciones": ""},
    "RAL9010": {"grupo": 1, "base": "WHITE", "observaciones": ""},
    "RAL9011": {"grupo": 2, "base": "UD", "observaciones": ""},
    "RAL9017": {"grupo": 3, "base": "UD", "observaciones": ""},
    "RAL9018": {"grupo": 1, "base": "WHITE", "observaciones": ""}
}

NOMBRES_RAL = {
    "RAL1000": "Verde beige",
    "RAL1001": "Beige",
    "RAL1002": "Amarillo arena",
    "RAL1003": "Amarillo senales",
    "RAL1004": "Amarillo dorado",
    "RAL1005": "Amarillo miel",
    "RAL1006": "Amarillo maiz",
    "RAL1007": "Amarillo narciso",
    "RAL1011": "Beige pardo",
    "RAL1012": "Amarillo limon",
    "RAL1013": "Blanco perla",
    "RAL1014": "Marfil",
    "RAL1015": "Marfil claro",
    "RAL1016": "Amarillo azufre",
    "RAL1017": "Amarillo azafran",
    "RAL1018": "Amarillo zinc",
    "RAL1019": "Gris beige",
    "RAL1020": "Amarillo oliva",
    "RAL1021": "Amarillo colza",
    "RAL1023": "Amarillo trafico",
    "RAL1024": "Amarillo ocre",
    "RAL1026": "Amarillo brillante",
    "RAL1027": "Amarillo curry",
    "RAL1028": "Amarillo melon",
    "RAL1032": "Amarillo retama",
    "RAL1033": "Amarillo dalia",
    "RAL1034": "Amarillo pastel",
    "RAL2000": "Naranja amarillento",
    "RAL2001": "Naranja rojizo",
    "RAL2002": "Naranja bermellon",
    "RAL2003": "Naranja pastel",
    "RAL2004": "Naranja puro",
    "RAL2008": "Naranja salmon claro",
    "RAL2009": "Naranja trafico",
    "RAL2010": "Naranja senales",
    "RAL2011": "Naranja intenso",
    "RAL2012": "Naranja salmon",
    "RAL3000": "Rojo llamas",
    "RAL3001": "Rojo senales",
    "RAL3002": "Rojo carmin",
    "RAL3003": "Rojo rubi",
    "RAL3004": "Rojo purpura",
    "RAL3005": "Rojo vino",
    "RAL3007": "Rojo negro",
    "RAL3009": "Rojo oxido",
    "RAL3011": "Rojo pardo",
    "RAL3012": "Rojo beige",
    "RAL3013": "Rojo tomate",
    "RAL3014": "Rojo antiguo",
    "RAL3015": "Rosa claro",
    "RAL3016": "Rojo coral",
    "RAL3017": "Rosa",
    "RAL3018": "Rojo fresa",
    "RAL3020": "Rojo trafico",
    "RAL3022": "Rojo salmon",
    "RAL3027": "Rojo frambuesa",
    "RAL3031": "Rojo oriente",
    "RAL4001": "Lila rojizo",
    "RAL4002": "Rojo violeta",
    "RAL4003": "Violeta erica",
    "RAL4004": "Burdeos",
    "RAL4005": "Lila azulado",
    "RAL4006": "Violeta trafico",
    "RAL4007": "Violeta purpura",
    "RAL4008": "Violeta senales",
    "RAL4009": "Violeta pastel",
    "RAL4010": "Violeta telemagenta",
    "RAL5000": "Violeta azulado",
    "RAL5001": "Verde azulado",
    "RAL5002": "Azul ultramar",
    "RAL5003": "Azul zafiro",
    "RAL5004": "Azul negro",
    "RAL5005": "Azul senales",
    "RAL5007": "Azul brillante",
    "RAL5008": "Azul grisaceo",
    "RAL5009": "Azul agua",
    "RAL5010": "Azul genciana",
    "RAL5011": "Azul acero",
    "RAL5012": "Azul claro",
    "RAL5013": "Azul cobalto",
    "RAL5014": "Azul paloma",
    "RAL5015": "Azul cielo",
    "RAL5017": "Azul trafico",
    "RAL5018": "Azul turquesa",
    "RAL5019": "Azul capri",
    "RAL5020": "Azul oceano",
    "RAL5021": "Azul agua",
    "RAL5022": "Azul noche",
    "RAL5023": "Azul lejano",
    "RAL5024": "Azul pastel",
    "RAL6000": "Verde patina",
    "RAL6001": "Verde esmeralda",
    "RAL6002": "Verde hoja",
    "RAL6003": "Verde oliva",
    "RAL6004": "Verde azulado",
    "RAL6005": "Verde musgo",
    "RAL6006": "Verde grisaceo",
    "RAL6007": "Verde botella",
    "RAL6008": "Verde parduzco",
    "RAL6009": "Verde abeto",
    "RAL6010": "Verde hierba",
    "RAL6011": "Verde reseda",
    "RAL6012": "Verde negro",
    "RAL6013": "Verde cana",
    "RAL6014": "Verde amarillento",
    "RAL6015": "Verde negro oliva",
    "RAL6016": "Verde turquesa",
    "RAL6017": "Verde mayo",
    "RAL6018": "Verde amarillo",
    "RAL6019": "Verde blanco",
    "RAL6020": "Verde cromo",
    "RAL6021": "Verde palido",
    "RAL6022": "Verde oliva parduzco",
    "RAL6024": "Verde trafico",
    "RAL6025": "Verde helecho",
    "RAL6026": "Verde opalo",
    "RAL6027": "Verde claro",
    "RAL6028": "Verde pino",
    "RAL6029": "Verde menta",
    "RAL6032": "Verde senales",
    "RAL6033": "Verde turquesa menta",
    "RAL6034": "Verde turquesa pastel",
    "RAL7000": "Gris ardilla",
    "RAL7001": "Gris plata",
    "RAL7002": "Gris oliva",
    "RAL7003": "Gris musgo",
    "RAL7004": "Gris senales",
    "RAL7005": "Gris raton",
    "RAL7006": "Gris beige",
    "RAL7008": "Gris caqui",
    "RAL7009": "Gris verdoso",
    "RAL7010": "Gris lona",
    "RAL7011": "Gris hierro",
    "RAL7012": "Gris basalto",
    "RAL7013": "Gris parduzco",
    "RAL7015": "Gris pizarra",
    "RAL7016": "Gris antracita",
    "RAL7021": "Gris negro",
    "RAL7022": "Gris sombra",
    "RAL7023": "Gris concreto",
    "RAL7024": "Gris grafito",
    "RAL7026": "Gris granito",
    "RAL7030": "Gris piedra",
    "RAL7031": "Gris azulado",
    "RAL7032": "Gris guijarro",
    "RAL7033": "Gris cemento",
    "RAL7034": "Gris amarillento",
    "RAL7035": "Gris claro",
    "RAL7036": "Gris platino",
    "RAL7037": "Gris polvo",
    "RAL7038": "Gris agata",
    "RAL7039": "Gris cuarzo",
    "RAL7040": "Gris ventana",
    "RAL7042": "Gris trafico A",
    "RAL7043": "Gris trafico B",
    "RAL7044": "Gris seda",
    "RAL7045": "Gris telgris 1",
    "RAL7046": "Gris telgris 2",
    "RAL7047": "Gris telgris 4",
    "RAL8000": "Pardo verdoso",
    "RAL8001": "Pardo ocre",
    "RAL8002": "Pardo senales",
    "RAL8003": "Pardo arcilla",
    "RAL8004": "Pardo cobre",
    "RAL8007": "Pardo ciervo",
    "RAL8008": "Pardo oliva",
    "RAL8011": "Pardo tierra",
    "RAL8012": "Pardo rojo",
    "RAL8014": "Pardo sepia",
    "RAL8015": "Pardo castano",
    "RAL8016": "Pardo caoba",
    "RAL8017": "Pardo chocolate",
    "RAL8019": "Gris pardo",
    "RAL8022": "Negro parduzco",
    "RAL8023": "Pardo anaranjado",
    "RAL8024": "Pardo beige",
    "RAL8025": "Pardo palido",
    "RAL8028": "Pardo tierra oscuro",
    "RAL9001": "Blanco crema",
    "RAL9002": "Blanco grisaceo",
    "RAL9004": "Negro senales",
    "RAL9005": "Negro intenso",
    "RAL9006": "Blanco aluminio",
    "RAL9010": "Blanco puro",
    "RAL9011": "Negro grafito",
    "RAL9017": "Negro trafico",
    "RAL9018": "Blanco papiro"
}

DESCRIPCIONES_GRUPO = {
    1: ("Economico",     "Colores claros o blancos. Poca cantidad de tinte.",        "🟢"),
    2: ("Estandar",      "Colores medios o neutros. Cantidad moderada de tinte.",    "🟡"),
    3: ("Premium",       "Colores intensos. Requieren bastante tinte y precision.",  "🟠"),
    4: ("Ultra Premium", "Colores muy intensos. Maxima cantidad de tinte.",          "🔴"),
}

DESCRIPCIONES_BASE = {
    "WHITE": "Base Blanca: para colores claros y pasteles.",
    "UD":    "Base Ultra Deep: para colores oscuros e intensos. Semi-transparente.",
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
    if st.button("🔒 Cerrar sesión"):
        st.session_state["autenticado"] = False; st.rerun()
    st.divider()
    st.subheader("📄 Documentos cargados")
    _df_side = listar_documentos()
    if _df_side.empty:
        st.caption("Sin documentos aun.")
    else:
        _res = _df_side[["fuente","producto","proveedor","tipo"]].drop_duplicates("fuente")
        fichas  = len(_res[_res["tipo"] == "ficha_tecnica"]) if "tipo" in _res.columns else 0
        precios = len(_res[_res["tipo"] == "precio"])       if "tipo" in _res.columns else 0
        st.caption(f"{len(_df_side)} fragmentos | {fichas} fichas | {precios} listas de precios")
        st.dataframe(_res, use_container_width=True, hide_index=True)
    if st.button("🔄 Refrescar lista"): st.rerun()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📁 Cargar Documentos", "🔍 Consultar", "🧮 Cotizador",
    "💰 Precios", "🌐 Precios Web", "🎨 Consulta RAL"
])

# ==============================================================
# TAB 1: CARGAR
# ==============================================================
with tab1:
    st.subheader("Carga fichas tecnicas y listas de precios")
    st.caption("Formatos soportados: PDF · Excel XLSX · Imagen JPG/PNG/WEBP")
    col1, col2 = st.columns(2)
    with col1:
        archivo = st.file_uploader("Selecciona archivo", type=["pdf","xlsx","xls","png","jpg","jpeg","webp"])
        producto_input  = st.text_input("Producto / categoria", placeholder="SuperPaint Exterior")
        proveedor_input = st.text_input("Proveedor", placeholder="Sherwin-Williams")
        tipo_contenido  = st.radio("Tipo de contenido", ["ficha_tecnica","precio"],
            format_func=lambda x: "📋 Ficha técnica" if x == "ficha_tecnica" else "💰 Lista de precios")
    with col2:
        if archivo:
            ext = archivo.name.split(".")[-1].lower()
            st.success(f"✅ Archivo listo: {archivo.name}")
            if ext in ["jpg","jpeg","png","webp"]:
                st.image(archivo, use_container_width=True); archivo.seek(0); borrar_documento(archivo.name)
    if st.button("⬆️ Procesar y guardar en Supabase", type="primary"):
        if archivo is None: st.warning("Selecciona un archivo.")
        elif not producto_input or not proveedor_input: st.warning("Escribe producto y proveedor.")
        else:
            ext = archivo.name.split(".")[-1].lower()
            with st.spinner(f"Procesando {archivo.name}..."):
                if ext in ["xlsx","xls"]:
                    df_ex = leer_excel(archivo)
                    if df_ex.empty: st.error("No se pudo leer el Excel.")
                    else:
                        st.dataframe(df_ex.head(20), use_container_width=True)
                        ok = sum(1 for _,row in df_ex.iterrows()
                                 if guardar_documento(" | ".join(f"{c}: {v}" for c,v in row.items() if pd.notna(v)),
                                                      archivo.name, producto_input, proveedor_input, tipo_contenido))
                        st.success(f"✅ {ok} filas guardadas desde Excel")
                elif ext in ["jpg","jpeg","png","webp"]:
                    st.info("Enviando a GPT-4o Vision...")
                    txt_img = leer_imagen_con_ia(archivo)
                    if txt_img:
                        with st.expander("Texto extraido"): st.write(txt_img)
                        chunks = dividir_texto(txt_img)
                        ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, tipo_contenido))
                        st.success(f"✅ {ok} fragmentos guardados desde imagen")
                elif ext == "pdf":
                    if tipo_contenido == "precio":
                        df_p = extraer_tabla_precios_pdf(archivo)
                        if not df_p.empty:
                            st.dataframe(df_p, use_container_width=True)
                            for _,row in df_p.iterrows(): guardar_precio(row["producto"], row["precio"], proveedor_input)
                        archivo.seek(0); txt_pdf = leer_pdf(archivo)
                        if txt_pdf:
                            chunks = dividir_texto(txt_pdf)
                            ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, "precio"))
                            st.success(f"✅ {len(df_p) if not df_p.empty else 0} precios + {ok} fragmentos guardados")
                    else:
                        txt = leer_pdf(archivo)
                        if not txt.strip():
                            st.warning("⚠️ PDF escaneado detectado. Procesando con Vision IA...")
                            try:
                                import fitz
                                archivo.seek(0); pdf_bytes = archivo.read()
                                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                                texto_total = ""
                                for num_pagina, pagina in enumerate(doc):
                                    st.info(f"Procesando página {num_pagina + 1} de {len(doc)}...")
                                    mat = fitz.Matrix(2,2); pix = pagina.get_pixmap(matrix=mat)
                                    img_bytes = pix.tobytes("jpeg"); b64 = base64.b64encode(img_bytes).decode("utf-8")
                                    resp = openai_client.chat.completions.create(
                                        model="gpt-4o-mini",
                                        messages=[{"role":"user","content":[
                                            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}},
                                            {"type":"text","text":f"Extrae TODO el texto de esta ficha tecnica de {producto_input}."}
                                        ]}], max_tokens=2000)
                                    texto_total += f"\n\n--- Pagina {num_pagina+1} ---\n{resp.choices[0].message.content}"
                                if texto_total.strip():
                                    with st.expander("Ver texto extraido"): st.text(texto_total[:3000])
                                    chunks = dividir_texto(texto_total)
                                    ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, tipo_contenido))
                                    st.success(f"✅ {ok} fragmentos guardados desde PDF escaneado")
                                else: st.error("No se pudo extraer texto.")
                            except ImportError: st.error("Instala PyMuPDF: pip install pymupdf")
                            except Exception as e: st.error(f"Error: {e}")
                        else:
                            chunks = dividir_texto(txt)
                            ok = sum(1 for c in chunks if guardar_documento(c, archivo.name, producto_input, proveedor_input, "ficha_tecnica"))
                            st.success(f"✅ {ok} de {len(chunks)} fragmentos guardados")
                            with st.expander("Ver texto extraido"): st.text(txt[:3000])


# ==============================================================
# TAB 2: CONSULTAR
# ==============================================================
with tab2:
    st.subheader("🔍 Consulta inteligente sobre tus documentos")
    total = contar_documentos()
    if total > 0: st.caption(f"Fragmentos disponibles en Supabase: {total}")
    else: st.info("Aun no hay documentos. Carga archivos en la pestaña Cargar Documentos.")
    col_q1, col_q2 = st.columns([3,1])
    with col_q1:
        pregunta = st.text_input("¿Que necesitas saber?", placeholder="¿Qué pintura uso para mampostería exterior?")
    with col_q2:
        tipo_busqueda = st.selectbox("Tipo", ["Todos","ficha_tecnica","precio"])
        modo_ia = st.radio("Modo IA", ["general","ficha","recomendacion"], horizontal=True)
    if st.button("🔍 Consultar", type="primary"):
        if not pregunta: st.warning("Escribe una pregunta.")
        elif total == 0: st.warning("Carga documentos primero.")
        else:
            tipo_filtro = None if tipo_busqueda == "Todos" else tipo_busqueda
            es_recomendacion = es_pregunta_recomendacion(pregunta) or modo_ia == "recomendacion"
            with st.spinner("Buscando en Supabase..."):
                if es_recomendacion:
                    resultados = buscar_todos_fichas(pregunta=pregunta, limite=15)
                    modo_usado = "recomendacion"
                    st.info("🎯 Modo recomendación — búsqueda semántica en todas las fichas")
                else:
                    resultados = buscar_documentos(pregunta, tipo=tipo_filtro)
                    modo_usado = modo_ia
            if not resultados: st.warning("No encontre informacion. Intenta con otras palabras.")
            else:
                contexto = "\n\n".join([r["contenido"] for r in resultados])
                col_ctx, col_res = st.columns(2)
                with col_ctx:
                    with st.expander(f"📄 {len(resultados)} fragmentos consultados"):
                        for r in resultados:
                            st.caption(f"📌 {r.get('producto','')} | {r.get('proveedor','')} | Tipo: {r.get('tipo','')}")
                            st.text(r["contenido"][:400]); st.divider()
                with col_res:
                    st.markdown("### 💡 Respuesta")
                    st.write(responder_con_ia(contexto, pregunta, modo_usado))

# ==============================================================
# TAB 3: COTIZADOR
# ==============================================================
with tab3:
    st.subheader("🧮 Cotizador — Tejas y Pinturas Sherwin-Williams")
    categoria = st.selectbox("Tipo de material", ["pintura","teja"])
    col_a, col_b = st.columns(2)
    with col_a:
        largo = ancho = area = cobertura = precio_unitario = traslapo = 0.0
        num_manos = 2
        if categoria == "pintura":
            marca = st.selectbox("Producto Sherwin-Williams", list(PINTURAS_SW.keys()))
            info  = PINTURAS_SW.get(marca, dict())
            if info:
                st.info(f"**Superficies:** {info.get('superficie','')}\n\n"
                        f"**Acabado:** {info.get('acabado','')} | **Dilucion:** {info.get('dilucion','')} | "
                        f"**Secado:** {info.get('tiempo_secado_tacto','')} | **Repinte:** {info.get('tiempo_repinte','')} | "
                        f"**Manos sugeridas:** {info.get('manos_recomendadas','')}")
            area = st.number_input("Area a pintar (m2)", value=20.0, min_value=0.1)
            _manos_ref = info.get("manos_recomendadas", 2) if isinstance(info, dict) else 2
            num_manos = st.number_input(f"Numero de manos (sugeridas: {_manos_ref})", value=int(_manos_ref), min_value=1, max_value=4)
            _cob_ref = info.get("cobertura_m2_galon", 32) if isinstance(info, dict) else 32
            cobertura = st.number_input(f"Rendimiento m2/galon (referencia: {_cob_ref})", value=float(_cob_ref))
            precio_unitario = st.number_input(f"Precio por galon ({moneda_display})", value=80000.0)
            if st.button("📋 Ver ficha técnica completa"):
                with st.spinner("Buscando ficha tecnica..."):
                    resultados_ficha = buscar_documentos(marca, tipo="ficha_tecnica")
                if resultados_ficha:
                    contexto_ficha = "\n\n".join([r["contenido"] for r in resultados_ficha])
                    st.markdown("### 📋 Ficha Técnica")
                    st.write(responder_con_ia(contexto_ficha, f"ficha tecnica de {marca}", "ficha"))
                else:
                    st.info(f"No hay ficha tecnica cargada para {marca}.")
        elif categoria == "teja":
            area    = st.number_input("Area total del techo (m2)", value=30.0)
            largo   = st.number_input("Largo de la teja (m)", value=11.80)
            ancho   = st.number_input("Ancho de la teja (m)", value=1.075)
            traslapo = st.number_input("Traslapo / desperdicio (%)", value=10.0) / 100
            precio_unitario = st.number_input(f"Precio por teja ({moneda_display})", value=0.0)
            st.caption("Referencia: Teja UPVC/Policarbonato JMUNDIAL — largo 11.80m x ancho 1.075m")
    with col_b:
        if st.button("🧮 Calcular", type="primary"):
            res = calcular_material(categoria=categoria, area=area, largo=largo, ancho=ancho,
                cobertura=cobertura, precio_unitario=precio_unitario, traslapo=traslapo, num_manos=int(num_manos))
            if res:
                st.markdown("#### 📊 Resultado")
                pt = res.get("precio_total", 0)
                if moneda_display != "COP": pt = convertir_precio(pt, "COP", moneda_display)
                for k, v in res.items():
                    if k == "precio_total": st.metric("PRECIO TOTAL ESTIMADO", f"{moneda_display} {pt:,.0f}")
                    else: st.write(f"**{k.replace('_',' ').title()}:** {v}")
                df_r = pd.DataFrame([{**res, "material": categoria,
                    "producto": marca if categoria == "pintura" else "Teja", "moneda": moneda_display}])
                st.download_button("📥 Exportar Excel", data=exportar_excel(df_r),
                    file_name=f"cotizacion_{categoria}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ==============================================================
# TAB 4: PRECIOS
# ==============================================================
with tab4:
    st.subheader("💰 Consulta de precios guardados")
    buscar_prod = st.text_input("Buscar producto", placeholder="teja, acrolon, industrial enamel...")
    if st.button("🔍 Buscar precios"):
        df_p2 = buscar_precios(buscar_prod)
        if df_p2.empty:
            st.info("No hay precios para ese producto.")
        else:
            # Mostrar columnas de grupos si existen
            cols_mostrar = ["descripcion", "precio", "grupo"]
            for g in ["precio_g1","precio_g2","precio_g3","precio_g4"]:
                if g in df_p2.columns: cols_mostrar.append(g)
            cols_existentes = [c for c in cols_mostrar if c in df_p2.columns]
            if moneda_display != "COP":
                df_p2[f"precio_{moneda_display}"] = df_p2["precio"].apply(
                    lambda x: round(convertir_precio(float(x), "COP", moneda_display), 2))
            st.dataframe(df_p2[cols_existentes] if cols_existentes else df_p2, use_container_width=True)
            st.download_button("📥 Exportar Excel", data=exportar_excel(df_p2),
                file_name="precios.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ==============================================================
# TAB 5: PRECIOS WEB
# ==============================================================
with tab5:
    st.subheader("🎨 Buscar producto Sherwin-Williams")
    st.caption("Busca informacion de cualquier producto SW directamente desde su sitio web.")
    col_sw1, col_sw2 = st.columns(2)
    with col_sw1:
        url_sw    = st.text_input("URL directa del producto (opcional)", placeholder="https://www.sherwin-williams.com/...")
        nombre_sw = st.text_input("Nombre del producto", placeholder="SuperPaint Exterior, Duration, Emerald...")
    with col_sw2:
        guardar_resultado = st.checkbox("Guardar resultado en Supabase como ficha_tecnica")
        proveedor_sw = st.text_input("Proveedor", value="Sherwin-Williams")
    if st.button("🔍 Buscar y traducir propiedades", type="primary"):
        if not nombre_sw: st.warning("Escribe el nombre del producto.")
        else:
            with st.spinner("Accediendo a la pagina del producto..."):
                url_final = url_sw if url_sw else f"https://www.sherwin-williams.com/es-us/search#q={nombre_sw.replace(' ','+')}"
                texto_pagina = scrape_sherwin_producto(url_final, nombre_sw)
            if texto_pagina.startswith("USAR_CONOCIMIENTO_IA"):
                st.warning("No se pudo leer la pagina. Respondiendo con conocimiento de GPT...")
            with st.spinner("Extrayendo y traduciendo propiedades..."):
                resultado = traducir_y_extraer_con_ia(texto_pagina, nombre_sw)
            st.markdown("### 📋 Propiedades del producto")
            st.write(resultado)
            if guardar_resultado and resultado:
                guardar_documento(resultado, f"SW - {nombre_sw}", nombre_sw, proveedor_sw, "ficha_tecnica")
                st.success("✅ Guardado en Supabase como ficha_tecnica.")


# ==============================================================
# TAB 6: CONSULTA RAL
# ==============================================================
with tab6:
    import math as _math
    st.subheader("🎨 Consulta de Color RAL — Sherwin-Williams")
    st.caption("Consulta grupo y base, calcula galones y genera el mensaje listo para el cliente.")

    col_r1, col_r2 = st.columns([1, 1])

    with col_r1:
        st.markdown("#### 🔍 Datos del pedido")
        ral_input = st.text_input("Codigo RAL", placeholder="Ej: RAL6005  o  6005").strip().upper()
        if ral_input and not ral_input.startswith("RAL"):
            ral_input = "RAL" + ral_input

        area_ral  = st.number_input("Area a pintar (m2)", min_value=1.0, value=50.0, step=5.0)
        manos_ral = st.number_input("Numero de manos", min_value=1, value=2, max_value=4)

        st.markdown("#### 🎨 Producto Sherwin-Williams")
        productos_sw = listar_productos_sw()
        if productos_sw:
            nombres_sw = [p["descripcion"] for p in productos_sw]
            producto_seleccionado = st.selectbox("Selecciona el producto SW", nombres_sw)
            prod_info = next((p for p in productos_sw if p["descripcion"] == producto_seleccionado), None)
        else:
            st.warning("No hay productos SW cargados en Supabase.")
            producto_seleccionado = None
            prod_info = None

        margen_ral = st.slider("Margen OBRIXA (%)", 0, 30, 10)

    with col_r2:
        if ral_input and ral_input in COLORES_RAL:
            info   = COLORES_RAL[ral_input]
            grupo  = info["grupo"]
            base   = info["base"]
            obs    = info["observaciones"]
            nombre = NOMBRES_RAL.get(ral_input, "Color personalizado")
            dg     = DESCRIPCIONES_GRUPO[grupo]
            db     = DESCRIPCIONES_BASE.get(base, base)

            # Precio real de Supabase segun grupo
            if prod_info:
                col_g = f"precio_g{grupo}"
                precio_base = float(prod_info.get(col_g, 0) or 0)
                if precio_base == 0:
                    for g in [1, 2, 3, 4]:
                        p = float(prod_info.get(f"precio_g{g}", 0) or 0)
                        if p > 0:
                            precio_base = p
                            break
            else:
                precio_base = 0

            precio_final = round(precio_base * (1 + margen_ral / 100))
            cobertura  = 33 if base == "WHITE" else 28
            area_total = area_ral * manos_ral
            galones    = _math.ceil(area_total / cobertura)
            p_total    = galones * precio_final

            obs_html = f'<p style="color:#F5A623;font-size:13px;margin:8px 0;">⚠️ {obs}</p>' if obs else ""
            st.markdown(f"""
            <div style="background:#1E2D3D;border-radius:12px;padding:20px;
                        border-left:4px solid #00C2FF;margin-bottom:16px;">
                <h3 style="color:#00C2FF;margin:0;">{ral_input}</h3>
                <p style="color:#E8F4FD;font-size:18px;margin:4px 0;">{nombre}</p>
                <hr style="border-color:#2A3F55;margin:12px 0;">
                <p style="color:#aaa;margin:4px 0;">
                    Base: <strong style="color:#fff;">{base}</strong> — {db}</p>
                <p style="color:#aaa;margin:4px 0;">
                    Grupo: <strong style="color:#fff;">{dg[2]} {grupo} — {dg[0]}</strong></p>
                <p style="color:#aaa;font-size:13px;margin:4px 0;">{dg[1]}</p>
                {obs_html}
            </div>
            """, unsafe_allow_html=True)

            st.markdown("#### 📊 Cotizacion")

            # Mostrar los 4 precios del producto
            if prod_info:
                with st.expander("Ver precios por grupo del producto seleccionado"):
                    c1, c2, c3, c4 = st.columns(4)
                    pg1 = float(prod_info.get("precio_g1", 0) or 0)
                    pg2 = float(prod_info.get("precio_g2", 0) or 0)
                    pg3 = float(prod_info.get("precio_g3", 0) or 0)
                    pg4 = float(prod_info.get("precio_g4", 0) or 0)
                    c1.metric("🟢 Grupo 1", f"${pg1:,.0f}" if pg1 > 0 else "N/A")
                    c2.metric("🟡 Grupo 2", f"${pg2:,.0f}" if pg2 > 0 else "N/A")
                    c3.metric("🟠 Grupo 3", f"${pg3:,.0f}" if pg3 > 0 else "N/A")
                    c4.metric("🔴 Grupo 4", f"${pg4:,.0f}" if pg4 > 0 else "N/A")

            ca, cb, cc = st.columns(3)
            ca.metric("Galones",        f"{galones} gal")
            cb.metric("Precio/galon",   f"${precio_final:,.0f}" if precio_final > 0 else "Consultar")
            cc.metric("Total estimado", f"${p_total:,.0f}"      if precio_final > 0 else "Consultar")

            obs_msg   = f"\n\n⚠️ Nota tecnica: {obs}" if obs else ""
            base_msg  = "blanca para tonos claros" if base == "WHITE" else "Ultra Deep para colores intensos"
            prod_txt  = f"\n🎨 Producto: {producto_seleccionado}" if producto_seleccionado else ""
            precio_txt = f"${precio_final:,.0f} COP" if precio_final > 0 else "Precio a consultar"
            total_txt  = f"${p_total:,.0f} COP"      if precio_final > 0 else "Precio a confirmar"

            mensaje = f"""Hola! Le comparto la cotizacion del color que solicito:

🎨 *{ral_input} — {nombre}*
📋 Grupo {grupo} ({dg[0]}) | Base {base}{prod_txt}

Para pintar *{area_ral:.0f} m2* con {manos_ral} mano(s) necesita:
• *{galones} galones* de pintura
• Precio por galon (Grupo {grupo}): *{precio_txt}*
• *Total estimado: {total_txt}*

Este color usa base {base} ({base_msg}), garantizando la fidelidad exacta del tono RAL.{obs_msg}

Desea que le generemos la cotizacion formal? OBRIXA"""

            st.markdown("#### 📱 Mensaje listo para el cliente")
            st.text_area("Copia y pega en WhatsApp:", value=mensaje, height=320)

        elif ral_input and ral_input not in COLORES_RAL:
            st.warning(f"El color {ral_input} no esta en la paleta SW. Verifica el codigo.")
            similares = [r for r in COLORES_RAL if r[:5] == ral_input[:5]]
            if similares:
                st.caption(f"Colores similares: {', '.join(similares[:8])}")
        else:
            st.info("👈 Ingresa el codigo RAL para ver grupo, base y generar el mensaje al cliente.")
            with st.expander("📋 Ver todos los 186 colores RAL disponibles"):
                df_ral = pd.DataFrame([
                    {"RAL": k, "Nombre": NOMBRES_RAL.get(k,"—"),
                     "Grupo": v["grupo"], "Base": v["base"],
                     "Observaciones": v["observaciones"]}
                    for k, v in COLORES_RAL.items()
                ])
                st.dataframe(df_ral, use_container_width=True, hide_index=True)
