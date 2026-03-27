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
DB_URL      = os.getenv("postgresql://postgres.zomdvxmiqqwpxhxklpeb:ObrixaSalaz2024@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
SUPABASE_URL = os.getenv("zomdvxmiqqwpxhxklpeb")
SUPABASE_KEY = os.getenv("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvbWR2eG1pcXF3cHhoeGtscGViIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM4NzIwODksImV4cCI6MjA4OTQ0ODA4OX0.xLRYnXIvVl6nl6UvnL2z5A4aSvrU8b_pMpt5NMe0qAk")

#@app.post("/whatsapp-twilio")
async def whatsapp_twilio(request: Request):
    """Recibe mensajes de WhatsApp via Twilio."""
    form = await request.form()
    mensaje  = form.get("Body", "")
    telefono = form.get("From", "").replace("whatsapp:", "")
    nombre   = form.get("ProfileName", "Cliente")

    # Busca respuesta en OBRIXA
    resultados = buscar_documentos(mensaje)
    if not resultados:
        respuesta = f"Hola {nombre}! No encontre informacion sobre *{mensaje}*. Intenta con el nombre exacto del producto."
    else:
        contexto  = "\n\n".join([r["contenido"] for r in resultados])
        respuesta = responder_con_ia(contexto, mensaje, "precios")
        respuesta = f"Hola {nombre}!\n\n{respuesta}"

    # Responde en formato TwiML que Twilio entiende
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{respuesta}</Message>
</Response>"""

    from fastapi.responses import Response
    return Response(content=twiml, media_type="application/xml")

openai_client = OpenAI(api_key=OPENAI_KEY)

app = FastAPI(
    title="OBRIXA AI API",
    description="API de materiales de construccion colombianos",
    version="1.0.0"
)

# Permite conexiones desde cualquier origen (n8n, WhatsApp, etc.)
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
def buscar_documentos(pregunta: str):
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

def responder_con_ia(contexto: str, pregunta: str, modo: str = "general") -> str:
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
            {"role": "user",   "content": f"Contexto:\n{contexto}\n\nPregunta: {pregunta}"}
        ],
        max_tokens=800
    )
    return resp.choices[0].message.content

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

# ---------------------------
# ENDPOINTS
# ---------------------------

@app.get("/")
def root():
    return {
        "mensaje": "OBRIXA AI API funcionando",
        "version": "1.0.0",
        "endpoints": ["/consultar", "/cotizar", "/precios", "/whatsapp", "/health"]
    }

@app.get("/health")
def health():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM embeddings")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"status": "ok", "fragmentos_en_db": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/consultar")
def consultar(req: ConsultaRequest):
    """
    Consulta inteligente sobre materiales.
    Usado por n8n y WhatsApp bot.
    """
    try:
        resultados = buscar_documentos(req.pregunta)
        if not resultados:
            return {
                "respuesta": "No encontre informacion sobre ese producto. Intenta con otras palabras clave.",
                "fragmentos_encontrados": 0,
                "fuentes": []
            }
        contexto  = "\n\n".join([r["contenido"] for r in resultados])
        respuesta = responder_con_ia(contexto, req.pregunta, req.modo)
        fuentes   = list(set([r.get("fuente", "") for r in resultados]))
        return {
            "respuesta": respuesta,
            "fragmentos_encontrados": len(resultados),
            "fuentes": fuentes
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cotizar")
def cotizar(req: CotizarRequest):
    """
    Calcula cantidades y precios de materiales.
    """
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
    """
    Busca precios de un producto en la base de datos.
    """
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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

@app.post("/whatsapp")
def whatsapp_webhook(req: WhatsAppRequest):
    """
    Endpoint principal para el bot de WhatsApp via n8n.
    Recibe mensaje, busca informacion y devuelve respuesta.
    """
    try:
        mensaje  = req.mensaje.strip()
        telefono = req.telefono
        nombre   = req.nombre

        # Detecta intención del mensaje
        mensaje_lower = mensaje.lower()

        # Si pide cotización
        if any(p in mensaje_lower for p in ["cotiz", "cuantos galones", "cuantas tejas", "cuanto cemento"]):
            respuesta = (
                f"Hola {nombre}! Para hacer una cotizacion necesito:\n"
                "1. Tipo de material (pintura, teja, cemento, acero)\n"
                "2. Area en m2\n"
                "3. Precio unitario\n\n"
                "Ejemplo: *cotizar pintura 50m2 precio 80000*"
            )
        # Si pide precio o info de producto
        else:
            resultados = buscar_documentos(mensaje)
            if not resultados:
                respuesta = (
                    f"Hola {nombre}! No encontre informacion sobre *{mensaje}*.\n"
                    "Intenta con el nombre exacto del producto o consulta nuestro catalogo."
                )
            else:
                contexto  = "\n\n".join([r["contenido"] for r in resultados])
                respuesta = responder_con_ia(contexto, mensaje, "precios")
                respuesta = f"Hola {nombre}!\n\n{respuesta}"

        return {
            "telefono": telefono,
            "respuesta": respuesta,
            "estado": "ok"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cargar-pdf")
async def cargar_pdf(
    archivo: UploadFile = File(...),
    producto: str = Form(...),
    proveedor: str = Form(...)
):
    """
    Carga un PDF y lo guarda en Supabase.
    """
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

        chunks = [texto[i:i+1500] for i in range(0, len(texto), 1500)]
        conn = get_conn()
        cur  = conn.cursor()
        ok   = 0
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

        return {
            "mensaje": f"PDF procesado correctamente",
            "fragmentos_guardados": ok,
            "archivo": archivo.filename
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))