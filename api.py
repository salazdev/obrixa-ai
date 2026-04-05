from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2
import psycopg2.extras
import pdfplumber
import pandas as pd
import requests
import unicodedata
import math
import io
import base64
from openai import OpenAI
from bs4 import BeautifulSoup
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi import Request
from fastapi.responses import Response
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_ngrok_header(request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

# ---------------------------
# MODELOS
# ---------------------------
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

# ---------------------------
# DB
# ---------------------------
def get_conn():
    return psycopg2.connect(DB_URL)

def quitar_tildes(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

# ---------------------------
# FUNCIONES CORE
# ---------------------------
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
        cur.execute(
            "SELECT * FROM precios_materiales WHERE material = %s ORDER BY precio",
            (material,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except:
        return []

def get_precio_especifico(material: str, descripcion: str):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM precios_materiales WHERE material = %s AND descripcion ILIKE %s LIMIT 1",
            (material, f"%{descripcion}%")
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except:
        return None

def buscar_documentos(pregunta: str, tipo: str = None):
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
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}
        ],
        max_tokens=500
    )
    return resp.choices[0].message.content

def calcular_material(categoria, area=0, largo=0, ancho=0, grosor=0,
                      cobertura=0, precio_unitario=0, rendimiento=1,
                      traslapo=0, num_manos=1):
    if categoria == "pintura":
        area_total = area * num_manos
        galones = math.ceil(area_total / cobertura) if cobertura > 0 else 0
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
        au = largo * ancho
        act = area * (1 + traslapo)
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
        vol = area * grosor
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

# ---------------------------
# ENDPOINTS
# ---------------------------
@app.get("/")
def root():
    return {
        "mensaje": "OBRIXA AI API funcionando",
        "version": "1.0.0",
        "endpoints": ["/consultar", "/cotizar", "/precios", "/health"]
    }

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

        # Registrar cliente automáticamente
        if telefono:
            registrar_cliente(telefono, nombre)

        # Verificar si hay sesión de cotización activa
        sesion = get_sesion(telefono) if telefono else None

        if sesion:
            estado = sesion["estado"]
            material = sesion["material"]
            datos = sesion["datos"] or {}

        if estado == "esperando_material":
            if any(x in mensaje_lower for x in ["teja", "1", "uno"]):
                precios = get_precios_material("teja")
                opciones = "\n".join([f"{i+1}. {p['descripcion']} - ${p['precio']:,.0f}" for i, p in enumerate(precios)])
                set_sesion(telefono, "esperando_tipo_teja", "teja", {"precios": [{"descripcion": p["descripcion"], "precio": float(p["precio"])} for p in precios]})
                return {"respuesta": f"🏗️ Tenemos estas tejas disponibles:\n\n{opciones}\n\n¿Cuál necesitas? Escribe el número.", "fragmentos_encontrados": 0, "fuentes": []}
            elif any(x in mensaje_lower for x in ["pintura", "2", "dos"]):
                set_sesion(telefono, "esperando_area", "pintura", {})
                return {"respuesta": "🎨 Perfecto. ¿Cuántos m² vas a pintar?", "fragmentos_encontrados": 0, "fuentes": []}
            elif any(x in mensaje_lower for x in ["cemento", "3", "tres"]):
                set_sesion(telefono, "esperando_area", "cemento", {})
                return {"respuesta": "🏚️ Perfecto. ¿Cuántos m² vas a cubrir con cemento?", "fragmentos_encontrados": 0, "fuentes": []}
            elif any(x in mensaje_lower for x in ["hierro", "acero", "varilla", "4", "cuatro"]):
                set_sesion(telefono, "esperando_longitud", "acero", {})
                return {"respuesta": "⚙️ Perfecto. ¿Cuántos metros lineales de hierro necesitas?", "fragmentos_encontrados": 0, "fuentes": []}
            elif any(x in mensaje_lower for x in ["ladrillo", "5", "cinco"]):
                precios = get_precios_material("ladrillo")
            if precios:
                precio_ladrillo = precios[0]["precio"]
                set_sesion(telefono, "esperando_area", "ladrillo", {"precio_unitario": float(precio_ladrillo)})
                return {"respuesta": f"🧱 Ladrillo 38x14x5 — Precio: ${precio_ladrillo:,.0f}/unidad\n\n¿Cuántos m² de muro vas a construir?", "fragmentos_encontrados": 0, "fuentes": []}
            else:
                return {"respuesta": "Por favor elige uno de estos materiales:\n\n1️⃣ Teja\n2️⃣ Pintura\n3️⃣ Cemento\n4️⃣ Hierro/Varilla\n5️⃣ Ladrillo", "fragmentos_encontrados": 0, "fuentes": []}}

            elif estado == "esperando_tipo_teja":
                precios = datos.get("precios", [])
                try:
                    idx = int(''.join(filter(str.isdigit, mensaje_lower))) - 1
                    if 0 <= idx < len(precios):
                        teja = precios[idx]
                        set_sesion(telefono, "esperando_area", "teja", {"precio_unitario": teja["precio"], "descripcion": teja["descripcion"]})
                        return {"respuesta": f"✅ *{teja['descripcion']}* seleccionada.\nPrecio: ${teja['precio']:,.0f}/unidad\n\n¿Cuántos m² tiene el techo que vas a cubrir?", "fragmentos_encontrados": 0, "fuentes": []}
                    else:
                        return {"respuesta": f"Por favor elige un número entre 1 y {len(precios)}.", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe el número de la teja que necesitas. Ejemplo: *1*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_area":
                try:
                    area = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["area"] = area
                    if material == "teja":
                        precio = datos.get("precio_unitario", 0)
                        descripcion = datos.get("descripcion", "Teja")
                        resultado = calcular_material("teja", area=area, largo=11.80, ancho=1.075, precio_unitario=precio, traslapo=0.1)
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización {descripcion}*\n\nÁrea: {resultado['area_m2']} m²\nCantidad: {resultado['cantidad']} tejas\nPrecio unitario: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
        elif material == "pintura":
            set_sesion(telefono, "esperando_manos", material, datos)
            return {"respuesta": f"✅ {area} m² anotado.\n\n¿Cuántas manos de pintura vas a aplicar?", "fragmentos_encontrados": 0, "fuentes": []}
        elif material == "cemento":
            set_sesion(telefono, "esperando_grosor", material, datos)
            return {"respuesta": f"✅ {area} m² anotado.\n\n¿Cuál es el grosor en metros? (ejemplo: 0.10 para 10cm)", "fragmentos_encontrados": 0, "fuentes": []}
        elif material == "ladrillo":
            precio = datos.get("precio_unitario", 0)
            resultado = calcular_material("ladrillo", area=area, largo=0.38, ancho=0.14, precio_unitario=precio, traslapo=0.05)
            borrar_sesion(telefono)
            return {"respuesta": f"🧮 *Cotización Ladrillo 38x14x5*\n\nÁrea: {resultado['area_m2']} m²\nLadrillos: {resultado['cantidad']} unidades\nPrecio unitario: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
    except:
        return {"respuesta": "Por favor escribe solo el número de m². Ejemplo: *50*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_longitud":
                try:
                    longitud = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["largo"] = longitud
                    set_sesion(telefono, "esperando_precio_acero", material, datos)
                    return {"respuesta": f"✅ {longitud} metros anotado.\n\n¿Cuál es el precio por varilla de 12m?", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe solo el número de metros. Ejemplo: *100*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_manos":
                try:
                    manos = int(''.join(filter(str.isdigit, mensaje_lower)))
                    datos["num_manos"] = manos
                    set_sesion(telefono, "esperando_rendimiento", material, datos)
                    return {"respuesta": f"✅ {manos} manos anotado.\n\n¿Cuál es el rendimiento del galón de pintura en m²? (ejemplo: 40)", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe solo el número de manos. Ejemplo: *2*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_rendimiento":
                try:
                    rendimiento = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["cobertura"] = rendimiento
                    set_sesion(telefono, "esperando_precio_pintura", material, datos)
                    return {"respuesta": f"✅ {rendimiento} m²/galón anotado.\n\n¿Cuál es el precio por galón de pintura?", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe solo el número. Ejemplo: *40*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_grosor":
                try:
                    grosor = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["grosor"] = grosor
                    set_sesion(telefono, "esperando_rendimiento_cemento", material, datos)
                    return {"respuesta": f"✅ {grosor}m de grosor anotado.\n\n¿Cuántos sacos de cemento rinde por m³? (normalmente 7)", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe el grosor en metros. Ejemplo: *0.10*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado == "esperando_rendimiento_cemento":
                try:
                    rendimiento = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["rendimiento"] = rendimiento
                    set_sesion(telefono, "esperando_precio_cemento", material, datos)
                    return {"respuesta": f"✅ {rendimiento} sacos/m³ anotado.\n\n¿Cuál es el precio por saco de cemento?", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe solo el número. Ejemplo: *7*", "fragmentos_encontrados": 0, "fuentes": []}

            elif estado in ["esperando_precio_teja", "esperando_precio_pintura", "esperando_precio_cemento", "esperando_precio_acero", "esperando_precio_ladrillo"]:
                try:
                    precio = float(''.join(filter(lambda x: x.isdigit() or x == '.', mensaje_lower)))
                    datos["precio_unitario"] = precio
                    if material == "teja":
                        resultado = calcular_material("teja", area=datos["area"], largo=11.80, ancho=1.075, precio_unitario=precio, traslapo=0.1)
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización Teja UPVC*\n\nÁrea: {resultado['area_m2']} m²\nCantidad: {resultado['cantidad']} tejas\nPrecio unitario: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "pintura":
                        resultado = calcular_material("pintura", area=datos["area"], cobertura=datos["cobertura"], precio_unitario=precio, num_manos=datos["num_manos"])
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización Pintura*\n\nÁrea: {resultado['area_m2']} m²\nManos: {resultado['manos']}\nGalones: {resultado['galones_necesarios']}\nPrecio/galón: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "cemento":
                        resultado = calcular_material("cemento", area=datos["area"], grosor=datos["grosor"], rendimiento=datos["rendimiento"], precio_unitario=precio)
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización Cemento*\n\nÁrea: {resultado['area_m2']} m²\nVolumen: {resultado['volumen_m3']} m³\nSacos: {resultado['cantidad_sacos']}\nPrecio/saco: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "acero":
                        resultado = calcular_material("acero", largo=datos["largo"], precio_unitario=precio)
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización Hierro*\n\nLongitud: {resultado['longitud_m']} m\nVarillas 12m: {resultado['varillas_12m']}\nPrecio/varilla: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
                    elif material == "ladrillo":
                        resultado = calcular_material("ladrillo", area=datos["area"], largo=0.38, ancho=0.14, precio_unitario=precio, traslapo=0.05)
                        borrar_sesion(telefono)
                        return {"respuesta": f"🧮 *Cotización Ladrillo*\n\nÁrea: {resultado['area_m2']} m²\nLadrillos: {resultado['cantidad']} unidades\nPrecio unitario: ${precio:,.0f}\nTotal estimado: ${resultado['precio_total']:,.0f}\n\n¿Deseas confirmar el pedido? Responde *SI* para continuar.", "fragmentos_encontrados": 0, "fuentes": []}
                except:
                    return {"respuesta": "Por favor escribe solo el precio en números. Ejemplo: *350000*", "fragmentos_encontrados": 0, "fuentes": []}

        # Detectar solicitud de ficha técnica
        fichas = ["ficha técnica", "ficha tecnica", "necesito la ficha", "datos tecnicos", "datos técnicos"]
        es_ficha = any(f in mensaje_lower for f in fichas)
        if es_ficha:
            resultados = buscar_documentos(req.pregunta, tipo="ficha_tecnica")
            if not resultados:
                return {"respuesta": "📋 Con gusto te envío la ficha técnica.\n\n¿De qué producto necesitas la ficha técnica? Puedes preguntarme por:\n\n• Teja UPVC\n• Teja Policarbonato\n• WPC Interior/Exterior\n• Piso Deck / Piso SPC\n• Cielo Raso\n\nEscribe el nombre del producto. 👇", "fragmentos_encontrados": 0, "fuentes": []}
            contexto = "\n\n".join([r["contenido"] for r in resultados])
            respuesta = responder_con_ia(contexto, req.pregunta, "ficha")
            fuentes = list(set([r.get("fuente", "") for r in resultados]))
            return {"respuesta": respuesta, "fragmentos_encontrados": len(resultados), "fuentes": fuentes}

        # Detectar solicitud de cotización
        cotizar_keywords = ["cotizar", "cotización", "cotizacion", "cuanto sale", "cuánto sale", "necesito calcular"]
        if any(k in mensaje_lower for k in cotizar_keywords):
            if telefono:
                set_sesion(telefono, "esperando_material", None, {})
            return {"respuesta": "🏗️ Con gusto te ayudo a cotizar.\n\n¿Qué material necesitas?\n\n1️⃣ Teja\n2️⃣ Pintura\n3️⃣ Cemento\n4️⃣ Hierro/Varilla\n5️⃣ Ladrillo", "fragmentos_encontrados": 0, "fuentes": []}

        # Detectar saludo inicial
        saludos = ["hola", "buenos", "buenas", "buen dia", "buen día", "consultar precios", "quiero consultar"]
        if any(s in mensaje_lower for s in saludos):
            return {"respuesta": "¡Hola! 👋 Bienvenido a *OBRIXA AI*. Con mucho gusto te ayudo.\n\n¿Qué necesitas hoy?\n\n🔍 Consultar *precios*\n📋 Ver *ficha técnica*\n🧮 *Cotizar* materiales", "fragmentos_encontrados": 0, "fuentes": []}

        # Buscar producto en precios
        resultados = buscar_documentos(req.pregunta, tipo="precio")
        if not resultados:
            return {"respuesta": "No encontré información sobre ese producto. 🔍\n\nIntenta con palabras como: *teja, cemento, acero, piso, cielo raso, WPC*.", "fragmentos_encontrados": 0, "fuentes": []}
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
            categoria=req.categoria,
            area=req.area,
            largo=req.largo,
            ancho=req.ancho,
            grosor=req.grosor,
            cobertura=req.cobertura,
            precio_unitario=req.precio_unitario,
            rendimiento=req.rendimiento,
            traslapo=req.traslapo,
            num_manos=req.num_manos
        )
        if not resultado:
            raise HTTPException(status_code=400, detail="Categoria no valida")
        return {"cotizacion": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/precios/{nombre_producto}")
def buscar_precios(nombre_producto: str):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM precios WHERE producto ILIKE %s LIMIT 20",
            (f"%{nombre_producto}%",)
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"productos": rows, "total": len(rows)}
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
            except Exception:
                pass
        conn.commit()
        cur.close()
        conn.close()
        return {"mensaje": "PDF procesado correctamente", "fragmentos_guardados": ok, "archivo": archivo.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
