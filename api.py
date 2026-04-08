from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2
import psycopg2.extras
import pdfplumber
import unicodedata
import math
import io
from openai import OpenAI
from fastapi import Request
from dotenv import load_dotenv
import os
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_KEY")
DB_URL = os.getenv("DB_URL", "postgresql://postgres.zomdvxmiqqwpxhxklpeb:RxNVnNQo6bWMbbqN@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

openai_client = OpenAI(api_key=OPENAI_KEY)

app = FastAPI(
    title="OBRIXA AI API",
    description="API de materiales de construccion colombianos",
    version="1.0.0"
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def add_ngrok_header(request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

class ConsultaRequest(BaseModel):
    pregunta: str
    modo: Optional[str] = "general"
    telefono: Optional[str] = None
    nombre: Optional[str] = None

class CotizarRequest(BaseModel):
    categoria: str
    area: Optional[float] = 0
    largo: Optional[float] = 0
    ancho: Optional[float] = 0
    grosor: Optional[float] = 0
    cobertura: Optional[float] = 0
    precio_unitario: Optional[float] = 0
    rendimiento: Optional[float] = 1
    traslapo: Optional[float] = 0
    num_manos: Optional[int] = 1

class WhatsAppRequest(BaseModel):
    mensaje: str
    telefono: str
    nombre: Optional[str] = "Cliente"


# ─────────────────────────────────────────────
# HELPERS DE BASE DE DATOS
# ─────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DB_URL)

def quitar_tildes(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def registrar_cliente(telefono: str, nombre: str = None):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clientes (telefono, nombre, primer_contacto, ultimo_contacto, total_consultas)
            VALUES (%s, %s, now(), now(), 1)
            ON CONFLICT (telefono) DO UPDATE SET
                ultimo_contacto = now(),
                total_consultas = clientes.total_consultas + 1,
                nombre = COALESCE(EXCLUDED.nombre, clientes.nombre)
        """, (telefono, nombre))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error registrando cliente: {e}")

def get_sesion(telefono: str):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM sesiones WHERE telefono = %s", (telefono,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except:
        return None

def set_sesion(telefono: str, estado: str, material: str = None, datos: dict = {}):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sesiones (telefono, estado, material, datos, actualizado)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (telefono) DO UPDATE SET
                estado = EXCLUDED.estado,
                material = EXCLUDED.material,
                datos = EXCLUDED.datos,
                actualizado = now()
        """, (telefono, estado, material, psycopg2.extras.Json(datos)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error sesion: {e}")

def borrar_sesion(telefono: str):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM sesiones WHERE telefono = %s", (telefono,))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

def get_precios_material(material: str):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM precios_materiales WHERE material = %s ORDER BY precio", (material,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except:
        return []

def buscar_documentos(pregunta: str, tipo: str = None):
    stopwords = {
        "que", "como", "cual", "para", "esto", "esta", "con", "los", "las",
        "del", "una", "por", "cuales", "son", "tiene", "hay", "dame", "dime",
        "cuanto", "cuesta", "kilos", "metros", "precio", "vale", "costo"
    }
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


# ─────────────────────────────────────────────
# HELPERS DE LÓGICA
# ─────────────────────────────────────────────

def responder_con_ia(contexto: str, pregunta: str, modo: str = "general") -> str:
    if modo == "ficha":
        system = (
            "Eres experto en materiales de construccion colombianos. "
            "Presenta la ficha tecnica del producto de forma clara y concisa. "
            "Incluye: caracteristicas principales, dimensiones clave y datos tecnicos importantes. "
            "Maximo 5 puntos. Usa formato simple sin markdown. Responde en espanol."
        )
    else:
        system = (
            "Eres experto en materiales de construccion colombianos. "
            "Usa el contexto para responder con precios, unidades y especificaciones. "
            "Si hay tablas de precios en el contexto, extrae y muestra los valores. "
            "Se conciso y directo. Responde en espanol."
        )
    resp_ia = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}
        ],
        max_tokens=500
    )
    return resp_ia.choices[0].message.content

def calcular_material(
    categoria, area=0, largo=0, ancho=0, grosor=0,
    cobertura=0, precio_unitario=0, rendimiento=1, traslapo=0, num_manos=1
):
    if categoria == "pintura":
        area_total = area * num_manos
        galones = math.ceil(area_total / cobertura) if cobertura > 0 else 0
        return {
            "area_m2": round(area, 2),
            "manos": num_manos,
            "galones_necesarios": galones,
            "precio_unitario": precio_unitario,
            "precio_total": round(galones * precio_unitario, 2)
        }
    elif categoria in ["teja", "ladrillo"]:
        au = largo * ancho
        act = area * (1 + traslapo)
        cant = math.ceil(act / au) if au > 0 else 0
        return {
            "area_m2": round(area, 2),
            "cantidad": cant,
            "precio_unitario": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2)
        }
    elif categoria == "cemento":
        vol = area * grosor
        cant = math.ceil(vol * rendimiento)
        return {
            "area_m2": round(area, 2),
            "volumen_m3": round(vol, 3),
            "cantidad_sacos": cant,
            "precio_unitario": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2)
        }
    elif categoria == "acero":
        cant = math.ceil(largo / 12)
        return {
            "longitud_m": largo,
            "varillas_12m": cant,
            "precio_unitario": precio_unitario,
            "precio_total": round(cant * precio_unitario, 2)
        }
    return {}

def detectar_material(texto: str):
    t = quitar_tildes(texto.lower())
    if any(x in t for x in ["teja", "techo", "cubierta"]):
        return "teja"
    if any(x in t for x in ["pintura", "pintar", "galon", "galón"]):
        return "pintura"
    if any(x in t for x in ["cemento", "mortero", "pega"]):
        return "cemento"
    if any(x in t for x in ["hierro", "acero", "varilla", "fierro"]):
        return "acero"
    if any(x in t for x in ["ladrillo", "bloque", "brick"]):
        return "ladrillo"
    return None

def extraer_numero(texto: str):
    clean = ''.join(c for c in texto if c.isdigit() or c in '.,')
    clean = clean.replace(',', '.')
    try:
        return float(clean)
    except:
        return None


# ─────────────────────────────────────────────
# TEXTOS DE MENÚ REUTILIZABLES
# ─────────────────────────────────────────────

MENU_PRINCIPAL = (
    "Hola! Bienvenido a *OBRIXA AI*.\n\n"
    "¿Que necesitas hoy?\n\n"
    "1. Consultar precios\n"
    "2. Ver ficha tecnica\n"
    "3. Cotizar materiales\n\n"
    "Escribe el numero o el nombre de la opcion."
)

MENU_MATERIALES = (
    "Con gusto te ayudo a cotizar.\n\n"
    "¿Que material necesitas?\n\n"
    "1. Teja\n"
    "2. Pintura\n"
    "3. Cemento\n"
    "4. Hierro / Varilla\n"
    "5. Ladrillo\n\n"
    "Escribe el numero o el nombre del material."
)

MENU_PRECIOS = (
    "¿Sobre que producto quieres consultar precios?\n\n"
    "1. Teja\n"
    "2. Pintura\n"
    "3. Cemento\n"
    "4. Hierro / Varilla\n"
    "5. Ladrillo\n"
    "6. Todos los productos\n\n"
    "Escribe el numero o el nombre."
)

MENU_FICHAS = (
    "¿De que producto necesitas la ficha tecnica?\n\n"
    "1. Teja UPVC\n"
    "2. Teja Policarbonato\n"
    "3. WPC Interior/Exterior\n"
    "4. Piso Deck / Piso SPC\n"
    "5. Cielo Raso\n\n"
    "Escribe el nombre del producto."
)

MENU_POST = (
    "Perfecto! ¿Que mas necesitas?\n\n"
    "1. Consultar precios\n"
    "2. Ver ficha tecnica\n"
    "3. Cotizar materiales\n\n"
    "Escribe el numero o la opcion."
)

DESPEDIDA = (
    "Gracias por contactar a *OBRIXA AI*. "
    "Fue un placer ayudarte. "
    "Cuando necesites materiales de construccion, aqui estamos. "
    "Hasta pronto!"
)

def r(texto):
    """Atajo para devolver respuesta estándar."""
    return {"respuesta": texto, "fragmentos_encontrados": 0, "fuentes": []}


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"mensaje": "OBRIXA AI API funcionando", "version": "1.0.0"}

@app.get("/health")
def health():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM embeddings")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"status": "ok", "fragmentos_en_db": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/consultar")
def consultar(req: ConsultaRequest):
    try:
        msg = req.pregunta.lower().strip()
        telefono = req.telefono or ""
        nombre = req.nombre or "Cliente"

        if telefono:
            registrar_cliente(telefono, nombre)

        # ═══════════════════════════════════════════════════════
        # BLOQUE 1 — COMANDOS GLOBALES
        # Tienen prioridad absoluta. Interrumpen cualquier sesión.
        # ═══════════════════════════════════════════════════════

        # Saludo → menú principal
        saludos = ["hola", "buenos", "buenas", "buen dia", "buen día",
                   "hi", "hey", "inicio", "menu", "menú", "start"]
        if any(s in msg for s in saludos):
            borrar_sesion(telefono)
            return r(MENU_PRINCIPAL)

        # Confirmación positiva → menú post-cotización
        if msg in ["si", "sí", "si!", "sí!", "claro", "dale", "ok", "okay"]:
            borrar_sesion(telefono)
            return r(MENU_POST)

        # Confirmación negativa → despedida
        if msg in ["no", "no gracias", "listo", "gracias", "hasta luego", "chao", "bye"]:
            borrar_sesion(telefono)
            return r(DESPEDIDA)

        # Opción 1 → menú de precios por producto
        if msg in ["1", "1.", "consultar precios", "consultar", "precios", "ver precios"]:
            borrar_sesion(telefono)
            return r(MENU_PRECIOS)

        # Opción 2 → menú de fichas técnicas
        if msg in ["2", "2.", "ficha", "ficha tecnica", "ficha técnica", "ver ficha", "fichas"]:
            borrar_sesion(telefono)
            return r(MENU_FICHAS)

        # Opción 3 → inicio del cotizador
        if msg in ["3", "3.", "cotizar", "cotizacion", "cotización", "quiero cotizar",
                   "me cotizas", "cuanto sale", "cuánto sale", "necesito calcular"]:
            borrar_sesion(telefono)
            set_sesion(telefono, "esperando_material", None, {})
            return r(MENU_MATERIALES)

        # Opción 6 → todos los precios
        if msg in ["6", "6.", "todos", "todos los productos"]:
            resultados = buscar_documentos("precios materiales construccion", tipo="precio")
            if resultados:
                contexto = "\n\n".join([r_["contenido"] for r_ in resultados])
                respuesta = responder_con_ia(contexto, "lista de precios disponibles", "general")
                fuentes = list(set([r_.get("fuente", "") for r_ in resultados]))
                return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}
            return r("No encontre precios disponibles en este momento.")

        # ═══════════════════════════════════════════════════════
        # BLOQUE 2 — BÚSQUEDA DE PRECIOS POR PRODUCTO
        # Cuando el usuario escribe un nombre de material
        # sin sesión activa (viene del MENU_PRECIOS)
        # ═══════════════════════════════════════════════════════

        sesion = get_sesion(telefono) if telefono else None
        mat_detectado = detectar_material(req.pregunta)

        if mat_detectado and not sesion:
            termino = "hierro" if mat_detectado == "acero" else mat_detectado
            resultados = buscar_documentos(termino, tipo="precio")
            if resultados:
                contexto = "\n\n".join([r_["contenido"] for r_ in resultados])
                respuesta = responder_con_ia(contexto, req.pregunta, "general")
                fuentes = list(set([r_.get("fuente", "") for r_ in resultados]))
                return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}
            return r(f"No encontre precios de {termino} en este momento.\n\nEscribe *1* para ver todos los productos disponibles.")

        # ═══════════════════════════════════════════════════════
        # BLOQUE 3 — FICHA TÉCNICA POR NOMBRE
        # ═══════════════════════════════════════════════════════

        fichas_kw = ["ficha técnica", "ficha tecnica", "necesito la ficha",
                     "datos tecnicos", "datos técnicos"]
        if any(f in msg for f in fichas_kw):
            resultados = buscar_documentos(req.pregunta, tipo="ficha_tecnica")
            if not resultados:
                return r(MENU_FICHAS)
            contexto = "\n\n".join([r_["contenido"] for r_ in resultados])
            respuesta = responder_con_ia(contexto, req.pregunta, "ficha")
            fuentes = list(set([r_.get("fuente", "") for r_ in resultados]))
            return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

        # Búsqueda de ficha por nombre de producto (sin la frase "ficha tecnica")
        fichas_nombres = ["teja upvc", "policarbonato", "wpc", "piso deck", "piso spc", "cielo raso"]
        if any(n in msg for n in fichas_nombres):
            resultados = buscar_documentos(req.pregunta, tipo="ficha_tecnica")
            if resultados:
                contexto = "\n\n".join([r_["contenido"] for r_ in resultados])
                respuesta = responder_con_ia(contexto, req.pregunta, "ficha")
                fuentes = list(set([r_.get("fuente", "") for r_ in resultados]))
                return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

        # ═══════════════════════════════════════════════════════
        # BLOQUE 4 — MÁQUINA DE ESTADOS (COTIZADOR)
        # ═══════════════════════════════════════════════════════

        if sesion:
            estado = sesion["estado"]
            material = sesion["material"]
            datos = sesion["datos"] or {}

            # ── Estado: esperando que el cliente diga el material ──
            if estado == "esperando_material":
                if msg in ["1", "1.", "teja", "techo", "cubierta"]:
                    mat = "teja"
                elif msg in ["2", "2.", "pintura", "pintar"]:
                    mat = "pintura"
                elif msg in ["3", "3.", "cemento", "mortero"]:
                    mat = "cemento"
                elif msg in ["4", "4.", "hierro", "varilla", "acero", "fierro"]:
                    mat = "acero"
                elif msg in ["5", "5.", "ladrillo", "bloque", "brick"]:
                    mat = "ladrillo"
                else:
                    mat = mat_detectado

                if mat == "teja":
                    precios = get_precios_material("teja")
                    if precios:
                        opciones = "\n".join([
                            f"{i+1}. {p['descripcion']} - ${p['precio']:,.0f}/und"
                            for i, p in enumerate(precios)
                        ])
                        set_sesion(telefono, "esperando_tipo_teja", "teja", {
                            "precios": [
                                {"descripcion": p["descripcion"], "precio": float(p["precio"])}
                                for p in precios
                            ]
                        })
                        return r(f"Tenemos estas tejas disponibles:\n\n{opciones}\n\n¿Cual necesitas? Escribe el numero.")
                    else:
                        borrar_sesion(telefono)
                        return r("Por el momento no tenemos precios de teja. Contactanos para cotizar.")

                elif mat == "ladrillo":
                    precios = get_precios_material("ladrillo")
                    opciones_fijas = [
                        {"descripcion": "Brick Liso 11 - Liso Arena (23x11x6.5 cm)", "precio": 0, "rendimiento": 56},
                        {"descripcion": "Brick Liso 11 - Liso Castor (23x11x6.5 cm)", "precio": 0, "rendimiento": 56},
                        {"descripcion": "Brick Liso 11 - Rustico Arena (23x11x6.5 cm)", "precio": 0, "rendimiento": 56},
                    ]
                    for p in reversed(precios):
                        opciones_fijas.insert(0, {
                            "descripcion": p["descripcion"],
                            "precio": float(p["precio"]),
                            "rendimiento": 56
                        })
                    opciones_txt = "\n".join([
                        f"{i+1}. {o['descripcion']}" +
                        (f" - ${o['precio']:,.0f}/und" if o["precio"] > 0 else " - Precio a consultar")
                        for i, o in enumerate(opciones_fijas)
                    ])
                    set_sesion(telefono, "esperando_tipo_ladrillo", "ladrillo", {"opciones": opciones_fijas})
                    return r(f"Tenemos estos ladrillos disponibles:\n\n{opciones_txt}\n\n¿Cual necesitas? Escribe el numero.")

                elif mat == "pintura":
                    precios = get_precios_material("pintura")
                    if precios:
                        precio_p = float(precios[0]["precio"])
                        descripcion = precios[0].get("descripcion", "Pintura")
                        set_sesion(telefono, "esperando_area", "pintura", {
                            "precio_unitario": precio_p,
                            "cobertura": 40.0,
                            "descripcion": descripcion,
                            "num_manos": 2
                        })
                        return r(f"*{descripcion}*\nPrecio: ${precio_p:,.0f}/galon | Rendimiento: 40 m2/galon\n\n¿Cuantos m2 vas a pintar?")
                    else:
                        set_sesion(telefono, "esperando_area", "pintura", {"num_manos": 2, "cobertura": 40.0})
                        return r("Pintura anotado.\n\n¿Cuantos m2 vas a pintar?")

                elif mat == "cemento":
                    precios = get_precios_material("cemento")
                    if precios:
                        precio_c = float(precios[0]["precio"])
                        descripcion = precios[0].get("descripcion", "Cemento")
                        set_sesion(telefono, "esperando_area", "cemento", {
                            "precio_unitario": precio_c,
                            "rendimiento": 7.0,
                            "grosor": 0.10,
                            "descripcion": descripcion
                        })
                        return r(f"*{descripcion}*\nPrecio: ${precio_c:,.0f}/saco | Rendimiento: 7 sacos/m3\n\n¿Cuantos m2 vas a cubrir?\n(Grosor por defecto: 10 cm)")
                    else:
                        set_sesion(telefono, "esperando_area", "cemento", {"rendimiento": 7.0, "grosor": 0.10})
                        return r("Cemento anotado.\n\n¿Cuantos m2 vas a cubrir?")

                elif mat == "acero":
                    precios = get_precios_material("acero")
                    if precios:
                        precio_a = float(precios[0]["precio"])
                        descripcion = precios[0].get("descripcion", "Varilla de hierro 12m")
                        set_sesion(telefono, "esperando_longitud", "acero", {
                            "precio_unitario": precio_a,
                            "descripcion": descripcion
                        })
                        return r(f"*{descripcion}*\nPrecio: ${precio_a:,.0f}/varilla de 12m\n\n¿Cuantos metros lineales de hierro necesitas?")
                    else:
                        set_sesion(telefono, "esperando_longitud", "acero", {})
                        return r("Hierro anotado.\n\n¿Cuantos metros lineales necesitas?")

                else:
                    return r("No reconoci el producto.\n\n" + MENU_MATERIALES)

            # ── Estado: selección de tipo de teja ──
            elif estado == "esperando_tipo_teja":
                precios = datos.get("precios", [])
                num = extraer_numero(msg)
                if num is not None:
                    idx = int(num) - 1
                    if 0 <= idx < len(precios):
                        teja = precios[idx]
                        set_sesion(telefono, "esperando_area", "teja", {
                            "precio_unitario": teja["precio"],
                            "descripcion": teja["descripcion"]
                        })
                        return r(
                            f"*{teja['descripcion']}* seleccionada.\n"
                            f"Precio: ${teja['precio']:,.0f}/und\n\n"
                            f"¿Cuantos m2 tiene el techo que vas a cubrir?"
                        )
                    else:
                        return r(f"Por favor elige un numero entre 1 y {len(precios)}.")
                else:
                    return r("Escribe el numero de la teja. Ejemplo: *1*")

            # ── Estado: selección de tipo de ladrillo ──
            elif estado == "esperando_tipo_ladrillo":
                opciones = datos.get("opciones", [])
                num = extraer_numero(msg)
                if num is not None:
                    idx = int(num) - 1
                    if 0 <= idx < len(opciones):
                        ladrillo = opciones[idx]
                        set_sesion(telefono, "esperando_area", "ladrillo", {
                            "precio_unitario": ladrillo["precio"],
                            "descripcion": ladrillo["descripcion"],
                            "rendimiento": ladrillo.get("rendimiento", 56)
                        })
                        precio_txt = f"${ladrillo['precio']:,.0f}/und" if ladrillo["precio"] > 0 else "Precio a consultar"
                        return r(
                            f"*{ladrillo['descripcion']}* seleccionado.\n"
                            f"Rendimiento: {ladrillo.get('rendimiento', 56)} und/m2 | {precio_txt}\n\n"
                            f"¿Cuantos m2 de muro vas a construir?"
                        )
                    else:
                        return r(f"Por favor elige un numero entre 1 y {len(opciones)}.")
                else:
                    return r("Escribe el numero del ladrillo. Ejemplo: *1*")

            # ── Estado: recibe el área ──
            elif estado == "esperando_area":
                num = extraer_numero(msg)
                if num is None:
                    return r("Por favor escribe solo el numero de m2. Ejemplo: *50*")

                area = num
                precio = datos.get("precio_unitario", 0)
                descripcion = datos.get("descripcion", (material or "").capitalize())

                if material == "teja":
                    resultado = calcular_material(
                        "teja", area=area, largo=11.80, ancho=1.075,
                        precio_unitario=precio, traslapo=0.1
                    )
                    borrar_sesion(telefono)
                    return r(
                        f"*Cotizacion {descripcion}*\n\n"
                        f"Area: {resultado['area_m2']} m2\n"
                        f"Cantidad: {resultado['cantidad']} tejas\n"
                        f"Precio unitario: ${precio:,.0f}\n"
                        f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                        f"Deseas continuar? Responde *SI* para ver mas opciones."
                    )

                elif material == "ladrillo":
                    rendimiento_lad = datos.get("rendimiento", 56)
                    cantidad = math.ceil(area * rendimiento_lad * 1.05)
                    borrar_sesion(telefono)
                    if precio > 0:
                        total = round(cantidad * precio, 2)
                        return r(
                            f"*Cotizacion {descripcion}*\n"
                            f"Proveedor: Terras de San Marino\n\n"
                            f"Area de muro: {area} m2\n"
                            f"Rendimiento: {rendimiento_lad} und/m2\n"
                            f"Ladrillos necesarios: {cantidad} unidades (incluye 5% desperdicio)\n"
                            f"Precio unitario: ${precio:,.0f}\n"
                            f"Total estimado: ${total:,.0f}\n\n"
                            f"Deseas continuar? Responde *SI* para ver mas opciones."
                        )
                    else:
                        return r(
                            f"*Cotizacion {descripcion}*\n"
                            f"Proveedor: Terras de San Marino\n\n"
                            f"Area de muro: {area} m2\n"
                            f"Rendimiento: {rendimiento_lad} und/m2\n"
                            f"Ladrillos necesarios: {cantidad} unidades (incluye 5% desperdicio)\n"
                            f"Precio: A consultar con el proveedor\n\n"
                            f"¿Deseas continuar? Responde *SI* para ver mas opciones."
                        )

                elif material == "pintura":
                    cobertura = datos.get("cobertura", 40)
                    num_manos = datos.get("num_manos", 2)
                    if precio > 0:
                        resultado = calcular_material(
                            "pintura", area=area, cobertura=cobertura,
                            precio_unitario=precio, num_manos=num_manos
                        )
                        borrar_sesion(telefono)
                        return r(
                            f"*Cotizacion {descripcion}*\n\n"
                            f"Area: {resultado['area_m2']} m2\n"
                            f"Manos: {resultado['manos']}\n"
                            f"Galones necesarios: {resultado['galones_necesarios']}\n"
                            f"Precio/galon: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas continuar? Responde *SI* para ver mas opciones."
                        )
                    else:
                        datos["area"] = area
                        set_sesion(telefono, "esperando_precio_pintura", material, datos)
                        return r(f"{area} m2 anotado.\n\n¿Cual es el precio por galon de pintura?")

                elif material == "cemento":
                    grosor = datos.get("grosor", 0.10)
                    rendimiento = datos.get("rendimiento", 7)
                    if precio > 0:
                        resultado = calcular_material(
                            "cemento", area=area, grosor=grosor,
                            rendimiento=rendimiento, precio_unitario=precio
                        )
                        borrar_sesion(telefono)
                        return r(
                            f"*Cotizacion {descripcion}*\n\n"
                            f"Area: {resultado['area_m2']} m2\n"
                            f"Grosor: {grosor*100:.0f} cm\n"
                            f"Volumen: {resultado['volumen_m3']} m3\n"
                            f"Sacos necesarios: {resultado['cantidad_sacos']}\n"
                            f"Precio/saco: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas continuar? Responde *SI* para ver mas opciones."
                        )
                    else:
                        datos["area"] = area
                        set_sesion(telefono, "esperando_precio_cemento", material, datos)
                        return r(f"{area} m2 anotado.\n\n¿Cual es el precio por saco de cemento?")

                else:
                    borrar_sesion(telefono)
                    return r("No reconoci el material. Escribe *3* para volver a cotizar.")

            # ── Estado: recibe metros lineales (hierro) ──
            elif estado == "esperando_longitud":
                num = extraer_numero(msg)
                if num is None:
                    return r("Por favor escribe solo el numero de metros. Ejemplo: *100*")
                longitud = num
                precio = datos.get("precio_unitario", 0)
                descripcion = datos.get("descripcion", "Varilla de hierro 12m")
                if precio > 0:
                    resultado = calcular_material("acero", largo=longitud, precio_unitario=precio)
                    borrar_sesion(telefono)
                    return r(
                        f"*Cotizacion {descripcion}*\n\n"
                        f"Longitud total: {resultado['longitud_m']} m\n"
                        f"Varillas de 12m: {resultado['varillas_12m']} unidades\n"
                        f"Precio/varilla: ${precio:,.0f}\n"
                        f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                        f"Deseas continuar? Responde *SI* para ver mas opciones."
                    )
                else:
                    datos["largo"] = longitud
                    set_sesion(telefono, "esperando_precio_acero", material, datos)
                    return r(f"{longitud} metros anotado.\n\n¿Cual es el precio por varilla de 12m?")

            # ── Estado: pedir precio cuando no está en DB ──
            elif estado in ["esperando_precio_pintura", "esperando_precio_cemento", "esperando_precio_acero"]:
                num = extraer_numero(msg)
                if num is None:
                    return r("Por favor escribe solo el precio en numeros. Ejemplo: *350000*")
                precio = num
                if material == "pintura":
                    resultado = calcular_material(
                        "pintura", area=datos["area"],
                        cobertura=datos.get("cobertura", 40),
                        precio_unitario=precio,
                        num_manos=datos.get("num_manos", 2)
                    )
                    borrar_sesion(telefono)
                    return r(
                        f"*Cotizacion Pintura*\n\n"
                        f"Area: {resultado['area_m2']} m2\n"
                        f"Manos: {resultado['manos']}\n"
                        f"Galones: {resultado['galones_necesarios']}\n"
                        f"Precio/galon: ${precio:,.0f}\n"
                        f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                        f"Deseas continuar? Responde *SI* para ver mas opciones."
                    )
                elif material == "cemento":
                    resultado = calcular_material(
                        "cemento", area=datos["area"],
                        grosor=datos.get("grosor", 0.10),
                        rendimiento=datos.get("rendimiento", 7),
                        precio_unitario=precio
                    )
                    borrar_sesion(telefono)
                    return r(
                        f"*Cotizacion Cemento*\n\n"
                        f"Area: {resultado['area_m2']} m2\n"
                        f"Volumen: {resultado['volumen_m3']} m3\n"
                        f"Sacos: {resultado['cantidad_sacos']}\n"
                        f"Precio/saco: ${precio:,.0f}\n"
                        f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                        f"Deseas continuar? Responde *SI* para ver mas opciones."
                    )
                elif material == "acero":
                    resultado = calcular_material("acero", largo=datos["largo"], precio_unitario=precio)
                    borrar_sesion(telefono)
                    return r(
                        f"*Cotizacion Hierro*\n\n"
                        f"Longitud: {resultado['longitud_m']} m\n"
                        f"Varillas 12m: {resultado['varillas_12m']}\n"
                        f"Precio/varilla: ${precio:,.0f}\n"
                        f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                        f"Deseas continuar? Responde *SI* para ver mas opciones."
                    )

        # ═══════════════════════════════════════════════════════
        # BLOQUE 5 — BÚSQUEDA GENERAL (fallback final)
        # ═══════════════════════════════════════════════════════
        resultados = buscar_documentos(req.pregunta, tipo="precio")
        if resultados:
            contexto = "\n\n".join([r_["contenido"] for r_ in resultados])
            respuesta = responder_con_ia(contexto, req.pregunta, req.modo)
            fuentes = list(set([r_.get("fuente", "") for r_ in resultados]))
            return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

        return r(
            "No encontre informacion sobre ese producto.\n\n"
            "Intenta con: teja, cemento, acero, piso, cielo raso, WPC.\n\n"
            "O escribe:\n1. Consultar precios\n2. Ver ficha tecnica\n3. Cotizar materiales"
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cotizar")
def cotizar(req: CotizarRequest):
    try:
        resultado = calcular_material(
            categoria=req.categoria, area=req.area, largo=req.largo,
            ancho=req.ancho, grosor=req.grosor, cobertura=req.cobertura,
            precio_unitario=req.precio_unitario, rendimiento=req.rendimiento,
            traslapo=req.traslapo, num_manos=req.num_manos
        )
        if not resultado:
            raise HTTPException(status_code=400, detail="Categoria no valida")
        return {"cotizacion": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cargar-pdf")
async def cargar_pdf(
    archivo: UploadFile = File(...),
    producto: str = Form(...),
    proveedor: str = Form(...)
):
    try:
        contenido = await archivo.read()
        texto = ""
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t:
                    texto += t + "\n"
        if not texto.strip():
            raise HTTPException(status_code=400, detail="No se pudo leer el PDF")
        lineas = [l.strip() for l in texto.splitlines() if l.strip()]
        chunks = []
        for i in range(0, len(lineas), 10):
            grupo = lineas[i:i+10]
            chunk = "\n".join(grupo)
            if len(chunk) > 20:
                chunks.append(chunk)
        conn = get_conn()
        cur = conn.cursor()
        ok = 0
        for chunk in chunks:
            try:
                cur.execute(
                    "INSERT INTO embeddings (contenido, fuente, producto, proveedor) VALUES (%s,%s,%s,%s)",
                    (chunk, archivo.filename, producto, proveedor)
                )
                ok += 1
            except:
                pass
        conn.commit()
        cur.close()
        conn.close()
        return {"mensaje": "PDF procesado correctamente", "fragmentos_guardados": ok, "archivo": archivo.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
