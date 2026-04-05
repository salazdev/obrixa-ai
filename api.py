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

@app.get("/")
def root():
    return {"mensaje": "OBRIXA AI API funcionando", "version": "1.0.0"}

@app.get("/health")
def he
