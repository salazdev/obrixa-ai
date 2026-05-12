from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2
import psycopg2.extras
import math
import unicodedata
import requests as http_requests
from openai import OpenAI
from dotenv import load_dotenv
import os
import datetime

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACION
# ─────────────────────────────────────────────

OPENAI_KEY   = os.getenv("OPENAI_KEY")
DB_URL       = os.getenv("DB_URL", "postgresql://postgres.zomdvxmiqqwpxhxklpeb:P3lXGNCb4jYlo4rZ@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# n8n y Evolution API
N8N_WEBHOOK      = "https://obrixa-constructor-n8n.ranspp.easypanel.host/webhook/cotizacion-confirmada"
EVOLUTION_URL    = "https://obrixa-constructor-evolution-api.ranspp.easypanel.host"
EVOLUTION_KEY    = "429683C4C977415CAAFCCE10F7D57E11"
EVOLUTION_INST   = "obrixa-whatsapp"
TU_WHATSAPP      = "523318749058"  # Tu número sin + para Evolution API

openai_client = OpenAI(api_key=OPENAI_KEY)

app = FastAPI(title="OBRIXA AI API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def add_ngrok_header(request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

class WhatsAppRequest(BaseModel):
    mensaje: str
    telefono: str
    nombre: Optional[str] = "Cliente"


# ─────────────────────────────────────────────
# BASE DE DATOS PINTURAS SW (local, sin DB)
# ─────────────────────────────────────────────

PINTURAS_SW = {
    "1": {
        "nombre": "SuperPaint Exterior SW",
        "usos": "Fachadas, madera, estuco, fibrocemento, ladrillo, exterior",
        "cobertura": 33,
        "manos": 2,
        "secado": "2 horas al tacto, repinte en 4 horas",
        "dilucion": "Hasta 10% agua",
        "acabado": "Mate / Satinado / Semibrillante",
        "superficies": "Madera, ladrillo, estuco, fibrocemento, acero, galvanizado, OSB, PVC"
    },
    "2": {
        "nombre": "SuperPaint Interior SW",
        "usos": "Muros interiores, cielorrasos, drywall",
        "cobertura": 33,
        "manos": 2,
        "secado": "30 min al tacto, repinte en 2-4 horas",
        "dilucion": "Hasta 10% agua",
        "acabado": "Mate / Satinado / Semibrillante",
        "superficies": "Drywall, estuco, ladrillo, fibrocemento, OSB, cielos rasos"
    },
    "3": {
        "nombre": "Elastomerica SW",
        "usos": "Impermeabilizante para fachadas, techos y exterior",
        "cobertura": 17,
        "manos": 2,
        "secado": "4 horas al tacto, repinte en 24 horas",
        "dilucion": "No diluir",
        "acabado": "Mate",
        "superficies": "Concreto, estuco, mamposteria, fibrocemento"
    },
    "4": {
        "nombre": "Otro producto Sherwin-Williams",
        "usos": "Consultar ficha tecnica especifica",
        "cobertura": 32,
        "manos": 2,
        "secado": "Variable segun producto",
        "dilucion": "Segun ficha tecnica",
        "acabado": "Variable",
        "superficies": "Consultar ficha tecnica"
    }
}


# ─────────────────────────────────────────────
# HELPERS DB
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

def get_precios_teja():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM precios_materiales WHERE material = 'teja' ORDER BY precio")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except:
        return []

def get_precio_pintura(nombre: str):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT precio FROM precios_materiales WHERE material = 'pintura' AND descripcion ILIKE %s LIMIT 1",
            (f"%{nombre}%",)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return float(row["precio"]) if row and row["precio"] else 0
    except:
        return 0


# ─────────────────────────────────────────────
# HELPERS LOGICA
# ─────────────────────────────────────────────

def extraer_numero(texto: str):
    clean = ''.join(c for c in texto if c.isdigit() or c in '.,')
    clean = clean.replace(',', '.')
    try:
        return float(clean)
    except:
        return None

def r(texto):
    return {"respuesta": texto, "fragmentos_encontrados": 0, "fuentes": []}


# ─────────────────────────────────────────────
# NOTIFICACION N8N
# ─────────────────────────────────────────────

def notificar_n8n(telefono: str, nombre: str, sesion: dict):
    try:
        datos = sesion.get("datos", {}) or {}
        material = sesion.get("material", "No especificado")
        payload = {
            "evento": "cotizacion_confirmada",
            "telefono_cliente": telefono,
            "nombre_cliente": nombre,
            "material": material,
            "descripcion": datos.get("descripcion", material),
            "area": datos.get("area", 0),
            "precio_unitario": datos.get("precio_unitario", 0),
            "galones": datos.get("galones", 0),
            "total_estimado": datos.get("total_estimado", 0),
            "timestamp": str(datetime.datetime.now())
        }
        http_requests.post(N8N_WEBHOOK, json=payload, timeout=5)
        print(f"n8n notificado OK: {payload}")
    except Exception as e:
        print(f"Error notificando n8n: {e}")


# ─────────────────────────────────────────────
# TEXTOS DE MENU
# ─────────────────────────────────────────────

MENU_PRINCIPAL = (
    "Hola! Bienvenido a *OBRIXA* 🏗️\n\n"
    "Somos distribuidores de pinturas *Sherwin-Williams* y tejas *JMUNDIAL*.\n\n"
    "¿En que te puedo ayudar?\n\n"
    "1️⃣ Ver ficha tecnica de pinturas SW\n"
    "2️⃣ Cotizar pintura SW\n"
    "3️⃣ Cotizar teja JMUNDIAL\n\n"
    "Escribe el numero de la opcion."
)

MENU_PINTURAS = (
    "Estas son nuestras pinturas Sherwin-Williams:\n\n"
    "1️⃣ SuperPaint Exterior\n"
    "2️⃣ SuperPaint Interior\n"
    "3️⃣ Elastomerica (impermeabilizante)\n"
    "4️⃣ Otro producto SW\n\n"
    "Escribe el numero del producto."
)

MENU_POST = (
    "Perfecto! ¿Necesitas algo mas?\n\n"
    "1️⃣ Ver ficha tecnica de pinturas SW\n"
    "2️⃣ Cotizar pintura SW\n"
    "3️⃣ Cotizar teja JMUNDIAL\n\n"
    "Escribe el numero o escribe *chao* para terminar."
)

DESPEDIDA = (
    "Gracias por contactar a *OBRIXA* 🏗️\n"
    "Fue un placer ayudarte.\n"
    "Cuando necesites pinturas SW o tejas JMUNDIAL, aqui estamos. Hasta pronto!"
)


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"mensaje": "OBRIXA AI API v2.0 funcionando"}

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
def consultar(req: WhatsAppRequest):
    try:
        msg        = req.mensaje.strip()
        msg_lower  = quitar_tildes(msg.lower())
        telefono   = req.telefono or ""
        nombre     = req.nombre or "Cliente"

        if telefono:
            registrar_cliente(telefono, nombre)

        # ══════════════════════════════════════
        # BLOQUE 1 — COMANDOS GLOBALES
        # ══════════════════════════════════════

        # Saludo → menú principal
        saludos = ["hola", "buenos", "buenas", "buen dia", "hi", "hey",
                   "inicio", "menu", "start", "ola"]
        if any(s in msg_lower for s in saludos):
            borrar_sesion(telefono)
            return r(MENU_PRINCIPAL)

        # Despedida
        if any(s in msg_lower for s in ["no gracias", "listo", "gracias", "hasta luego", "chao", "bye", "adios"]):
            borrar_sesion(telefono)
            return r(DESPEDIDA)

        # Confirmacion positiva → notificar n8n
        if msg_lower in ["si", "si!", "sí", "sí!", "claro", "dale", "ok", "okay", "confirmo"]:
            sesion_actual = get_sesion(telefono)
            if sesion_actual and sesion_actual.get("material"):
                notificar_n8n(telefono, nombre, sesion_actual)
            borrar_sesion(telefono)
            return r(
                "✅ Perfecto! Un asesor de OBRIXA te contactara pronto para coordinar tu pedido.\n\n"
                + MENU_POST
            )

        # ══════════════════════════════════════
        # BLOQUE 2 — MENÚ PRINCIPAL (sin sesion)
        # ══════════════════════════════════════

        sesion = get_sesion(telefono)
        estado = sesion["estado"] if sesion else None

        if estado is None:
            if msg_lower in ["1", "ficha", "ficha tecnica", "ver ficha"]:
                set_sesion(telefono, "eligiendo_ficha", None, {})
                return r(
                    "📋 *Fichas tecnicas Sherwin-Williams*\n\n"
                    + MENU_PINTURAS
                )

            if msg_lower in ["2", "cotizar pintura", "pintura", "cotizar sw"]:
                set_sesion(telefono, "eligiendo_pintura_cotizar", None, {})
                return r(
                    "🎨 *Cotizador de pinturas SW*\n\n"
                    + MENU_PINTURAS
                )

            if msg_lower in ["3", "teja", "cotizar teja", "jmundial"]:
                tejas = get_precios_teja()
                if tejas:
                    opciones = "\n".join([
                        f"{i+1}️⃣ {t['descripcion']} — ${float(t['precio']):,.0f}/und"
                        for i, t in enumerate(tejas)
                    ])
                    set_sesion(telefono, "eligiendo_teja", "teja", {
                        "tejas": [{"descripcion": t["descripcion"], "precio": float(t["precio"])} for t in tejas]
                    })
                    return r(f"🏗️ *Tejas JMUNDIAL disponibles:*\n\n{opciones}\n\nEscribe el numero de la teja.")
                else:
                    return r("En este momento no tenemos precios de teja disponibles. Escribe *hola* para volver al menu.")

            # Si escribe algo no reconocido sin sesion
            return r(MENU_PRINCIPAL)

        # ══════════════════════════════════════
        # BLOQUE 3 — MAQUINA DE ESTADOS
        # ══════════════════════════════════════

        datos = sesion.get("datos", {}) or {}

        # ── Eligiendo pintura para FICHA TECNICA ──
        if estado == "eligiendo_ficha":
            pintura = PINTURAS_SW.get(msg_lower.strip(), None)
            # Buscar por numero
            for k, v in PINTURAS_SW.items():
                if msg_lower.strip() == k:
                    pintura = v
                    break
                if quitar_tildes(v["nombre"].lower()) in msg_lower:
                    pintura = v
                    break

            if not pintura:
                return r(f"No reconoci el producto. {MENU_PINTURAS}")

            borrar_sesion(telefono)
            ficha = (
                f"📋 *{pintura['nombre']}*\n\n"
                f"🏠 *Usos:* {pintura['usos']}\n\n"
                f"📐 *Rendimiento:* {pintura['cobertura']} m²/galón\n"
                f"🖌️ *Manos recomendadas:* {pintura['manos']}\n"
                f"⏱️ *Secado:* {pintura['secado']}\n"
                f"💧 *Dilución:* {pintura['dilucion']}\n"
                f"✨ *Acabado:* {pintura['acabado']}\n"
                f"🧱 *Superficies:* {pintura['superficies']}\n\n"
                f"¿Deseas cotizar este producto? Escribe *2* o escribe *menu* para volver."
            )
            return r(ficha)

        # ── Eligiendo pintura para COTIZAR ──
        if estado == "eligiendo_pintura_cotizar":
            pintura = None
            for k, v in PINTURAS_SW.items():
                if msg_lower.strip() == k:
                    pintura = v
                    pintura["key"] = k
                    break
                if quitar_tildes(v["nombre"].lower()) in msg_lower:
                    pintura = v
                    pintura["key"] = k
                    break

            if not pintura:
                return r(f"No reconoci el producto. {MENU_PINTURAS}")

            # Buscar precio en DB
            precio_db = get_precio_pintura(pintura["nombre"])
            precio_txt = f"${precio_db:,.0f}/galon" if precio_db > 0 else "Precio a consultar"

            set_sesion(telefono, "esperando_area_pintura", "pintura", {
                "descripcion": pintura["nombre"],
                "cobertura": pintura["cobertura"],
                "manos": pintura["manos"],
                "precio_unitario": precio_db
            })
            return r(
                f"*{pintura['nombre']}* seleccionada ✅\n"
                f"Rendimiento: {pintura['cobertura']} m²/galón | {precio_txt}\n"
                f"Manos recomendadas: {pintura['manos']}\n\n"
                f"¿Cuantos *m²* vas a pintar?"
            )

        # ── Esperando área para pintura ──
        if estado == "esperando_area_pintura":
            num = extraer_numero(msg)
            if num is None:
                return r("Por favor escribe solo el numero de m². Ejemplo: *80*")

            area       = num
            cobertura  = datos.get("cobertura", 33)
            manos      = datos.get("manos", 2)
            precio     = datos.get("precio_unitario", 0)
            descripcion = datos.get("descripcion", "Pintura SW")

            area_total = area * manos
            galones    = math.ceil(area_total / cobertura)

            if precio > 0:
                total = galones * precio
                set_sesion(telefono, "esperando_confirmacion", "pintura", {
                    "descripcion": descripcion,
                    "area": area,
                    "galones": galones,
                    "precio_unitario": precio,
                    "total_estimado": total
                })
                return r(
                    f"📊 *Cotizacion {descripcion}*\n\n"
                    f"📐 Area: {area} m²\n"
                    f"🖌️ Manos: {manos}\n"
                    f"🪣 Galones necesarios: *{galones} galones*\n"
                    f"💰 Precio/galon: ${precio:,.0f}\n"
                    f"💵 *Total estimado: ${total:,.0f} COP*\n\n"
                    f"¿Deseas que un asesor te contacte para coordinar el pedido?\n"
                    f"Responde *SI* para confirmar o *NO* para volver al menu."
                )
            else:
                set_sesion(telefono, "esperando_precio_pintura", "pintura", {
                    **datos, "area": area, "galones": galones
                })
                return r(
                    f"📐 {area} m² anotado.\n"
                    f"Necesitas aproximadamente *{galones} galones*.\n\n"
                    f"¿Cual es el precio por galon que te ofrecieron?"
                )

        # ── Esperando precio de pintura ──
        if estado == "esperando_precio_pintura":
            num = extraer_numero(msg)
            if num is None:
                return r("Por favor escribe solo el precio. Ejemplo: *115000*")

            precio     = num
            galones    = datos.get("galones", 0)
            area       = datos.get("area", 0)
            descripcion = datos.get("descripcion", "Pintura SW")
            total      = galones * precio

            set_sesion(telefono, "esperando_confirmacion", "pintura", {
                "descripcion": descripcion,
                "area": area,
                "galones": galones,
                "precio_unitario": precio,
                "total_estimado": total
            })
            return r(
                f"📊 *Cotizacion {descripcion}*\n\n"
                f"📐 Area: {area} m²\n"
                f"🪣 Galones necesarios: *{galones} galones*\n"
                f"💰 Precio/galon: ${precio:,.0f}\n"
                f"💵 *Total estimado: ${total:,.0f} COP*\n\n"
                f"¿Deseas que un asesor te contacte para coordinar el pedido?\n"
                f"Responde *SI* para confirmar o *NO* para volver al menu."
            )

        # ── Eligiendo teja ──
        if estado == "eligiendo_teja":
            tejas = datos.get("tejas", [])
            num = extraer_numero(msg)
            if num is None or not (1 <= int(num) <= len(tejas)):
                return r(f"Escribe el numero de la teja entre 1 y {len(tejas)}.")

            teja = tejas[int(num) - 1]
            set_sesion(telefono, "esperando_area_teja", "teja", {
                "descripcion": teja["descripcion"],
                "precio_unitario": teja["precio"]
            })
            return r(
                f"*{teja['descripcion']}* seleccionada ✅\n"
                f"Precio: ${teja['precio']:,.0f}/und\n\n"
                f"¿Cuantos *m²* tiene el techo a cubrir?"
            )

        # ── Esperando área para teja ──
        if estado == "esperando_area_teja":
            num = extraer_numero(msg)
            if num is None:
                return r("Por favor escribe solo el numero de m². Ejemplo: *50*")

            area        = num
            precio      = datos.get("precio_unitario", 0)
            descripcion = datos.get("descripcion", "Teja JMUNDIAL")

            # Calculo teja JMUNDIAL: 11.80m x 1.075m con 10% traslapo
            largo, ancho, traslapo = 11.80, 1.075, 0.10
            area_teja = largo * ancho
            area_con_traslapo = area * (1 + traslapo)
            cantidad = math.ceil(area_con_traslapo / area_teja)
            total = cantidad * precio

            set_sesion(telefono, "esperando_confirmacion", "teja", {
                "descripcion": descripcion,
                "area": area,
                "cantidad": cantidad,
                "precio_unitario": precio,
                "total_estimado": total
            })
            return r(
                f"📊 *Cotizacion {descripcion}*\n\n"
                f"📐 Area del techo: {area} m²\n"
                f"📏 Teja: {largo}m x {ancho}m\n"
                f"🏗️ Cantidad necesaria: *{cantidad} tejas* (incluye 10% traslapo)\n"
                f"💰 Precio/teja: ${precio:,.0f}\n"
                f"💵 *Total estimado: ${total:,.0f} COP*\n\n"
                f"¿Deseas que un asesor te contacte para coordinar el pedido?\n"
                f"Responde *SI* para confirmar o *NO* para volver al menu."
            )

        # ── Esperando confirmacion final ──
        if estado == "esperando_confirmacion":
            if msg_lower in ["si", "si!", "sí", "sí!", "claro", "dale", "ok", "okay", "confirmo"]:
                notificar_n8n(telefono, nombre, sesion)
                borrar_sesion(telefono)
                return r(
                    "✅ *Confirmado!*\n\n"
                    "Un asesor de *OBRIXA* te contactara pronto para coordinar tu pedido 🏗️\n\n"
                    "¿Necesitas algo mas?\n\n"
                    + MENU_POST
                )
            else:
                borrar_sesion(telefono)
                return r("Entendido. " + MENU_POST)

        # Fallback
        return r(MENU_PRINCIPAL)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
