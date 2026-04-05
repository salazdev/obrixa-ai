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

app = FastAPI(title="OBRIXA AI API", description="API de materiales de construccion colombianos", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def add_ngrok_header(request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

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
    stopwords = {"que", "como", "cual", "para", "esto", "esta", "con", "los", "las", "del", "una", "por", "cuales", "son", "tiene", "hay", "dame", "dime", "cuanto", "cuesta", "kilos", "metros", "precio", "vale", "costo"}
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
                cur.execute("SELECT * FROM embeddings WHERE contenido ILIKE %s AND tipo = %s LIMIT 8", (f"%{variante}%", tipo))
            else:
                cur.execute("SELECT * FROM embeddings WHERE contenido ILIKE %s LIMIT 8", (f"%{variante}%",))
            for r in cur.fetchall():
                if r["id"] not in vistos:
                    vistos.add(r["id"])
                    todos.append(dict(r))
    cur.close()
    conn.close()
    return todos[:10]

def responder_con_ia(contexto: str, pregunta: str, modo: str = "general") -> str:
    if modo == "ficha":
        system = "Eres experto en materiales de construccion colombianos. Presenta la ficha tecnica del producto de forma clara y concisa. Incluye: caracteristicas principales, dimensiones clave y datos tecnicos importantes. Maximo 5 puntos. Usa formato simple sin markdown. Responde en espanol."
    else:
        system = "Eres experto en materiales de construccion colombianos. Usa el contexto para responder con precios, unidades y especificaciones. Si hay tablas de precios en el contexto, extrae y muestra los valores. Se conciso y directo. Responde en espanol."
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}
        ],
        max_tokens=500
    )
    return resp.choices[0].message.content

def calcular_material(categoria, area=0, largo=0, ancho=0, grosor=0, cobertura=0, precio_unitario=0, rendimiento=1, traslapo=0, num_manos=1):
    if categoria == "pintura":
        area_total = area * num_manos
        galones = math.ceil(area_total / cobertura) if cobertura > 0 else 0
        return {"area_m2": round(area, 2), "manos": num_manos, "galones_necesarios": galones, "precio_unitario": precio_unitario, "precio_total": round(galones * precio_unitario, 2)}
    elif categoria in ["teja", "ladrillo"]:
        au = largo * ancho
        act = area * (1 + traslapo)
        cant = math.ceil(act / au) if au > 0 else 0
        return {"area_m2": round(area, 2), "cantidad": cant, "precio_unitario": precio_unitario, "precio_total": round(cant * precio_unitario, 2)}
    elif categoria == "cemento":
        vol = area * grosor
        cant = math.ceil(vol * rendimiento)
        return {"area_m2": round(area, 2), "volumen_m3": round(vol, 3), "cantidad_sacos": cant, "precio_unitario": precio_unitario, "precio_total": round(cant * precio_unitario, 2)}
    elif categoria == "acero":
        cant = math.ceil(largo / 12)
        return {"longitud_m": largo, "varillas_12m": cant, "precio_unitario": precio_unitario, "precio_total": round(cant * precio_unitario, 2)}
    return {}

# ─────────────────────────────────────────────
# Detecta el material que menciona el cliente
# ─────────────────────────────────────────────
def detectar_material(texto: str):
    t = quitar_tildes(texto.lower())
    if any(x in t for x in ["teja", "techo", "cubierta"]):
        return "teja"
    if any(x in t for x in ["pintura", "pintar", "pintar", "galon", "galón"]):
        return "pintura"
    if any(x in t for x in ["cemento", "mortero", "pega"]):
        return "cemento"
    if any(x in t for x in ["hierro", "acero", "varilla", "fierro"]):
        return "acero"
    if any(x in t for x in ["ladrillo", "bloque"]):
        return "ladrillo"
    return None

# ─────────────────────────────────────────────
# Extrae el primer número flotante de un texto
# ─────────────────────────────────────────────
def extraer_numero(texto: str):
    clean = ''.join(c for c in texto if c.isdigit() or c in '.,' )
    clean = clean.replace(',', '.')
    try:
        return float(clean)
    except:
        return None

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
        mensaje_lower = req.pregunta.lower().strip()
        telefono = req.telefono or ""
        nombre = req.nombre or "Cliente"

        if telefono:
            registrar_cliente(telefono, nombre)

        sesion = get_sesion(telefono) if telefono else None

        # ═══════════════════════════════════════
        # MÁQUINA DE ESTADOS — COTIZADOR
        # ═══════════════════════════════════════
        if sesion:
            estado = sesion["estado"]
            material = sesion["material"]
            datos = sesion["datos"] or {}

            # ── PASO 1: El cliente dijo qué producto quiere ──
            if estado == "esperando_material":
                mat = detectar_material(req.pregunta)

                if mat == "teja":
                    precios = get_precios_material("teja")
                    if precios:
                        opciones = "\n".join([f"{i+1}. {p['descripcion']} - ${p['precio']:,.0f}" for i, p in enumerate(precios)])
                        set_sesion(telefono, "esperando_tipo_teja", "teja", {
                            "precios": [{"descripcion": p["descripcion"], "precio": float(p["precio"])} for p in precios]
                        })
                        return {"respuesta": f"🏗️ Tenemos estas tejas disponibles:\n\n{opciones}\n\n¿Cuál necesitas? Escribe el número.", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        borrar_sesion(telefono)
                        return {"respuesta": "Por el momento no tenemos precios de teja disponibles. Escríbenos al WhatsApp para cotizar directamente.", "fragmentos_encontrados": 0, "fuentes": []}

                elif mat == "ladrillo":
                    precios = get_precios_material("ladrillo")
                    if precios:
                        precio_ladrillo = float(precios[0]["precio"])
                        descripcion = precios[0].get("descripcion", "Ladrillo 38x14x5")
                        set_sesion(telefono, "esperando_area", "ladrillo", {
                            "precio_unitario": precio_ladrillo,
                            "descripcion": descripcion
                        })
                        return {"respuesta": f"🧱 *{descripcion}*\nPrecio: ${precio_ladrillo:,.0f}/unidad\n\n¿Cuántos m² de muro vas a construir?", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        borrar_sesion(telefono)
                        return {"respuesta": "Por el momento no tenemos precios de ladrillo disponibles. Contáctanos para cotizar.", "fragmentos_encontrados": 0, "fuentes": []}

                elif mat == "pintura":
                    precios = get_precios_material("pintura")
                    if precios:
                        precio_pintura = float(precios[0]["precio"])
                        cobertura = float(precios[0].get("rendimiento", 40) or 40)
                        descripcion = precios[0].get("descripcion", "Pintura")
                        set_sesion(telefono, "esperando_area", "pintura", {
                            "precio_unitario": precio_pintura,
                            "cobertura": cobertura,
                            "descripcion": descripcion,
                            "num_manos": 2  # valor por defecto
                        })
                        return {"respuesta": f"🎨 *{descripcion}*\nPrecio: ${precio_pintura:,.0f}/galón | Rendimiento: {cobertura} m²/galón\n\n¿Cuántos m² vas a pintar?", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        # Sin precio en DB, pedir área y luego precio
                        set_sesion(telefono, "esperando_area", "pintura", {"num_manos": 2, "cobertura": 40})
                        return {"respuesta": "🎨 Pintura anotado.\n\n¿Cuántos m² vas a pintar?", "fragmentos_encontrados": 0, "fuentes": []}

                elif mat == "cemento":
                    precios = get_precios_material("cemento")
                    if precios:
                        precio_cemento = float(precios[0]["precio"])
                        rendimiento = float(precios[0].get("rendimiento", 7) or 7)
                        descripcion = precios[0].get("descripcion", "Cemento")
                        set_sesion(telefono, "esperando_area", "cemento", {
                            "precio_unitario": precio_cemento,
                            "rendimiento": rendimiento,
                            "descripcion": descripcion,
                            "grosor": 0.10  # grosor por defecto 10cm
                        })
                        return {"respuesta": f"🏚️ *{descripcion}*\nPrecio: ${precio_cemento:,.0f}/saco | Rendimiento: {rendimiento} sacos/m³\n\n¿Cuántos m² vas a cubrir?\n_(Grosor por defecto: 10 cm. Si es diferente, dímelo antes.)_", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        set_sesion(telefono, "esperando_area", "cemento", {"rendimiento": 7, "grosor": 0.10})
                        return {"respuesta": "🏚️ Cemento anotado.\n\n¿Cuántos m² vas a cubrir?", "fragmentos_encontrados": 0, "fuentes": []}

                elif mat == "acero":
                    precios = get_precios_material("acero")
                    if precios:
                        precio_acero = float(precios[0]["precio"])
                        descripcion = precios[0].get("descripcion", "Varilla de hierro 12m")
                        set_sesion(telefono, "esperando_longitud", "acero", {
                            "precio_unitario": precio_acero,
                            "descripcion": descripcion
                        })
                        return {"respuesta": f"⚙️ *{descripcion}*\nPrecio: ${precio_acero:,.0f}/varilla de 12m\n\n¿Cuántos metros lineales de hierro necesitas?", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        set_sesion(telefono, "esperando_longitud", "acero", {})
                        return {"respuesta": "⚙️ Hierro anotado.\n\n¿Cuántos metros lineales necesitas?", "fragmentos_encontrados": 0, "fuentes": []}

                else:
                    # No reconoció el material — mostrar opciones
                    return {"respuesta": "No reconocí el producto. ¿Qué necesitas cotizar?\n\n🏗️ Teja\n🎨 Pintura\n🏚️ Cemento\n⚙️ Hierro / Varilla\n🧱 Ladrillo\n\nEscribe el nombre del material.", "fragmentos_encontrados": 0, "fuentes": []}

            # ── PASO 2: Selección de tipo de teja ──
            elif estado == "esperando_tipo_teja":
                precios = datos.get("precios", [])
                num = extraer_numero(mensaje_lower)
                if num is not None:
                    idx = int(num) - 1
                    if 0 <= idx < len(precios):
                        teja = precios[idx]
                        set_sesion(telefono, "esperando_area", "teja", {
                            "precio_unitario": teja["precio"],
                            "descripcion": teja["descripcion"]
                        })
                        return {"respuesta": f"✅ *{teja['descripcion']}* seleccionada.\nPrecio: ${teja['precio']:,.0f}/unidad\n\n¿Cuántos m² tiene el techo que vas a cubrir?", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        return {"respuesta": f"Por favor elige un número entre 1 y {len(precios)}.", "fragmentos_encontrados": 0, "fuentes": []}
                else:
                    return {"respuesta": "Por favor escribe el número de la teja. Ejemplo: *1*", "fragmentos_encontrados": 0, "fuentes": []}

            # ── PASO 3: Recibe el área y calcula ──
            elif estado == "esperando_area":
                num = extraer_numero(mensaje_lower)
                if num is not None:
                    area = num
                    precio = datos.get("precio_unitario", 0)
                    descripcion = datos.get("descripcion", material.capitalize())

                    if material == "teja":
                        resultado = calcular_material("teja", area=area, largo=11.80, ancho=1.075, precio_unitario=precio, traslapo=0.1)
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotización {descripcion}*\n\n"
                            f"Area: {resultado['area_m2']} m2\n"
                            f"Cantidad: {resultado['cantidad']} tejas\n"
                            f"Precio unitario: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}

                    elif material == "ladrillo":
                        resultado = calcular_material("ladrillo", area=area, largo=0.38, ancho=0.14, precio_unitario=precio, traslapo=0.05)
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotización {descripcion}*\n\n"
                            f"Area de muro: {resultado['area_m2']} m2\n"
                            f"Ladrillos necesarios: {resultado['cantidad']} unidades\n"
                            f"Precio unitario: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}

                    elif material == "pintura":
                        cobertura = datos.get("cobertura", 40)
                        num_manos = datos.get("num_manos", 2)
                        if precio > 0:
                            resultado = calcular_material("pintura", area=area, cobertura=cobertura, precio_unitario=precio, num_manos=num_manos)
                            borrar_sesion(telefono)
                            return {"respuesta": (
                                f"🧮 *Cotización {descripcion}*\n\n"
                                f"Area: {resultado['area_m2']} m2\n"
                                f"Manos: {resultado['manos']}\n"
                                f"Galones necesarios: {resultado['galones_necesarios']}\n"
                                f"Precio/galon: ${precio:,.0f}\n"
                                f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                                f"Deseas confirmar el pedido? Responde *SI* para continuar."
                            ), "fragmentos_encontrados": 0, "fuentes": []}
                        else:
                            # Sin precio en DB, pedirlo
                            datos["area"] = area
                            set_sesion(telefono, "esperando_precio_pintura", material, datos)
                            return {"respuesta": f"✅ {area} m2 anotado.\n\n¿Cuál es el precio por galón de pintura?", "fragmentos_encontrados": 0, "fuentes": []}

                    elif material == "cemento":
                        grosor = datos.get("grosor", 0.10)
                        rendimiento = datos.get("rendimiento", 7)
                        if precio > 0:
                            resultado = calcular_material("cemento", area=area, grosor=grosor, rendimiento=rendimiento, precio_unitario=precio)
                            borrar_sesion(telefono)
                            return {"respuesta": (
                                f"🧮 *Cotizacion {descripcion}*\n\n"
                                f"Area: {resultado['area_m2']} m2\n"
                                f"Grosor: {grosor*100:.0f} cm\n"
                                f"Volumen: {resultado['volumen_m3']} m3\n"
                                f"Sacos necesarios: {resultado['cantidad_sacos']}\n"
                                f"Precio/saco: ${precio:,.0f}\n"
                                f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                                f"Deseas confirmar el pedido? Responde *SI* para continuar."
                            ), "fragmentos_encontrados": 0, "fuentes": []}
                        else:
                            datos["area"] = area
                            set_sesion(telefono, "esperando_precio_cemento", material, datos)
                            return {"respuesta": f"✅ {area} m2 anotado.\n\n¿Cuál es el precio por saco de cemento?", "fragmentos_encontrados": 0, "fuentes": []}

                else:
                    return {"respuesta": "Por favor escribe solo el número de m2. Ejemplo: *50*", "fragmentos_encontrados": 0, "fuentes": []}

            # ── PASO 3b: Recibe metros lineales (hierro) ──
            elif estado == "esperando_longitud":
                num = extraer_numero(mensaje_lower)
                if num is not None:
                    longitud = num
                    precio = datos.get("precio_unitario", 0)
                    descripcion = datos.get("descripcion", "Varilla de hierro 12m")
                    if precio > 0:
                        resultado = calcular_material("acero", largo=longitud, precio_unitario=precio)
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotizacion {descripcion}*\n\n"
                            f"Longitud total: {resultado['longitud_m']} m\n"
                            f"Varillas de 12m: {resultado['varillas_12m']} unidades\n"
                            f"Precio/varilla: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        datos["largo"] = longitud
                        set_sesion(telefono, "esperando_precio_acero", material, datos)
                        return {"respuesta": f"✅ {longitud} metros anotado.\n\n¿Cuál es el precio por varilla de 12m?", "fragmentos_encontrados": 0, "fuentes": []}
                else:
                    return {"respuesta": "Por favor escribe solo el número de metros. Ejemplo: *100*", "fragmentos_encontrados": 0, "fuentes": []}

            # ── Fallback: pedir precio si no estaba en DB ──
            elif estado in ["esperando_precio_pintura", "esperando_precio_cemento", "esperando_precio_acero"]:
                num = extraer_numero(mensaje_lower)
                if num is not None:
                    precio = num
                    if material == "pintura":
                        resultado = calcular_material("pintura", area=datos["area"], cobertura=datos.get("cobertura", 40), precio_unitario=precio, num_manos=datos.get("num_manos", 2))
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotizacion Pintura*\n\n"
                            f"Area: {resultado['area_m2']} m2\n"
                            f"Manos: {resultado['manos']}\n"
                            f"Galones: {resultado['galones_necesarios']}\n"
                            f"Precio/galon: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "cemento":
                        resultado = calcular_material("cemento", area=datos["area"], grosor=datos.get("grosor", 0.10), rendimiento=datos.get("rendimiento", 7), precio_unitario=precio)
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotizacion Cemento*\n\n"
                            f"Area: {resultado['area_m2']} m2\n"
                            f"Volumen: {resultado['volumen_m3']} m3\n"
                            f"Sacos: {resultado['cantidad_sacos']}\n"
                            f"Precio/saco: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "acero":
                        resultado = calcular_material("acero", largo=datos["largo"], precio_unitario=precio)
                        borrar_sesion(telefono)
                        return {"respuesta": (
                            f"🧮 *Cotizacion Hierro*\n\n"
                            f"Longitud: {resultado['longitud_m']} m\n"
                            f"Varillas 12m: {resultado['varillas_12m']}\n"
                            f"Precio/varilla: ${precio:,.0f}\n"
                            f"Total estimado: ${resultado['precio_total']:,.0f}\n\n"
                            f"Deseas confirmar el pedido? Responde *SI* para continuar."
                        ), "fragmentos_encontrados": 0, "fuentes": []}
                else:
                    return {"respuesta": "Por favor escribe solo el precio en numeros. Ejemplo: *350000*", "fragmentos_encontrados": 0, "fuentes": []}

        # ═══════════════════════════════════════
        # FLUJO PRINCIPAL (sin sesión activa)
        # ═══════════════════════════════════════

        # ── Ficha técnica ──
        fichas = ["ficha técnica", "ficha tecnica", "necesito la ficha", "datos tecnicos", "datos técnicos"]
        if any(f in mensaje_lower for f in fichas):
            resultados = buscar_documentos(req.pregunta, tipo="ficha_tecnica")
            if not resultados:
                return {"respuesta": "📋 Con gusto te envio la ficha tecnica.\n\n¿De que producto necesitas la ficha?\n\n- Teja UPVC\n- Teja Policarbonato\n- WPC Interior/Exterior\n- Piso Deck / Piso SPC\n- Cielo Raso\n\nEscribe el nombre del producto.", "fragmentos_encontrados": 0, "fuentes": []}
            contexto = "\n\n".join([r["contenido"] for r in resultados])
            respuesta = responder_con_ia(contexto, req.pregunta, "ficha")
            fuentes = list(set([r.get("fuente", "") for r in resultados]))
            return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

        # ── Cotizar ──
        cotizar_keywords = ["cotizar", "cotización", "cotizacion", "cuanto sale", "cuánto sale", "necesito calcular", "me cotiza", "me cotizas", "quiero cotizar"]
        if any(k in mensaje_lower for k in cotizar_keywords):
            # Intentar detectar si ya mencionó el material en el mismo mensaje
            mat = detectar_material(req.pregunta)
            if mat and telefono:
                # Arrancar directo al paso del material detectado
                set_sesion(telefono, "esperando_material", None, {})
                # Reusar la misma lógica pasando de nuevo por sesion
                # Simular que ya tiene sesión esperando_material
                sesion_nueva = {"estado": "esperando_material", "material": None, "datos": {}}
                # Procesar en siguiente turno — mejor guardar sesion y responder con pregunta de area directamente
            if telefono:
                set_sesion(telefono, "esperando_material", None, {})
            return {"respuesta": "🏗️ Con gusto te ayudo a cotizar.\n\n¿Que material necesitas?\n\nEscribe el nombre: teja, pintura, cemento, hierro o ladrillo.", "fragmentos_encontrados": 0, "fuentes": []}

        # ── Saludos ──
        saludos = ["hola", "buenos", "buenas", "buen dia", "buen día", "consultar precios", "quiero consultar", "hi", "hey"]
        if any(s in mensaje_lower for s in saludos):
            return {"respuesta": "Hola! Bienvenido a *OBRIXA AI*. Con mucho gusto te ayudo.\n\n¿Que necesitas hoy?\n\nConsultar *precios*\nVer *ficha tecnica*\n*Cotizar* materiales", "fragmentos_encontrados": 0, "fuentes": []}

        # ── Búsqueda general en embeddings ──
        resultados = buscar_documentos(req.pregunta, tipo="precio")
        if not resultados:
            return {"respuesta": "No encontre informacion sobre ese producto.\n\nIntenta con: teja, cemento, acero, piso, cielo raso, WPC.", "fragmentos_encontrados": 0, "fuentes": []}
        contexto = "\n\n".join([r["contenido"] for r in resultados])
        respuesta = responder_con_ia(contexto, req.pregunta, req.modo)
        fuentes = list(set([r.get("fuente", "") for r in resultados]))
        return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

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
async def cargar_pdf(archivo: UploadFile = File(...), producto: str = Form(...), proveedor: str = Form(...)):
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
