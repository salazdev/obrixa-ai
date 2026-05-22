# ---------------------------
# FUNCIÓN: obtener precio SW por producto y grupo
# Agregar junto a las otras funciones de DB en app_obrixa.py
# ---------------------------

def get_precio_sw_por_grupo(nombre_producto: str, grupo: int):
    """Busca el precio exacto de un producto SW según el grupo RAL."""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        col_precio = f"precio_g{grupo}" if grupo in [1,2,3,4] else "precio_g1"
        
        cur.execute(f"""
            SELECT descripcion, {col_precio} as precio, precio_g1, precio_g2, precio_g3, precio_g4
            FROM precios_materiales
            WHERE material = 'pintura'
              AND proveedor = 'Sherwin-Williams'
              AND descripcion ILIKE %s
              AND {col_precio} > 0
            ORDER BY grupo ASC
            LIMIT 1
        """, (f"%{nombre_producto}%",))
        
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"Error precio SW: {e}")
        return None

def listar_productos_sw():
    """Lista todos los productos SW únicos con sus 4 precios."""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT DISTINCT descripcion, precio_g1, precio_g2, precio_g3, precio_g4
            FROM precios_materiales
            WHERE material = 'pintura' AND proveedor = 'Sherwin-Williams'
              AND precio_g1 > 0
            ORDER BY descripcion
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"Error listando productos SW: {e}")
        return []


# ==============================================================
# TAB 6: CONSULTA RAL — REEMPLAZA el bloque "with col_r1:"
# completo por este código actualizado
# ==============================================================

with tab6:
    import math as _math
    st.subheader("🎨 Consulta de Color RAL — Sherwin-Williams")
    st.caption("Consulta grupo y base, calcula galones y genera el mensaje listo para el cliente.")

    col_r1, col_r2 = st.columns([1, 1])

    with col_r1:
        st.markdown("#### 🔍 Datos del pedido")
        ral_input = st.text_input(
            "Codigo RAL", placeholder="Ej: RAL6005  o  6005"
        ).strip().upper()
        if ral_input and not ral_input.startswith("RAL"):
            ral_input = "RAL" + ral_input

        area_ral  = st.number_input("Area a pintar (m2)", min_value=1.0, value=50.0, step=5.0)
        manos_ral = st.number_input("Numero de manos", min_value=1, value=2, max_value=4)

        st.markdown("#### 🎨 Producto Sherwin-Williams")
        
        # Cargar productos desde Supabase
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

            # Obtener precio real de Supabase según grupo
            if prod_info:
                col_g = f"precio_g{grupo}"
                precio_base = float(prod_info.get(col_g, 0) or 0)
                # Si no hay precio para ese grupo, usar el más cercano disponible
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
            
            # Mostrar los 4 precios del producto seleccionado
            if prod_info:
                with st.expander("Ver precios por grupo"):
                    c1,c2,c3,c4 = st.columns(4)
                    pg1 = float(prod_info.get("precio_g1",0) or 0)
                    pg2 = float(prod_info.get("precio_g2",0) or 0)
                    pg3 = float(prod_info.get("precio_g3",0) or 0)
                    pg4 = float(prod_info.get("precio_g4",0) or 0)
                    c1.metric("🟢 Grupo 1", f"${pg1:,.0f}" if pg1 > 0 else "N/A")
                    c2.metric("🟡 Grupo 2", f"${pg2:,.0f}" if pg2 > 0 else "N/A")
                    c3.metric("🟠 Grupo 3", f"${pg3:,.0f}" if pg3 > 0 else "N/A")
                    c4.metric("🔴 Grupo 4", f"${pg4:,.0f}" if pg4 > 0 else "N/A")

            ca, cb, cc = st.columns(3)
            ca.metric("Galones",        f"{galones} gal")
            cb.metric("Precio/galon",   f"${precio_final:,.0f}" if precio_final > 0 else "Consultar")
            cc.metric("Total estimado", f"${p_total:,.0f}" if precio_final > 0 else "Consultar")

            obs_msg  = f"\n\n⚠️ Nota tecnica: {obs}" if obs else ""
            base_msg = "blanca para tonos claros" if base == "WHITE" else "Ultra Deep para colores intensos"
            prod_txt = f"\n🎨 Producto: {producto_seleccionado}" if producto_seleccionado else ""
            precio_txt = f"${precio_final:,.0f} COP" if precio_final > 0 else "Precio a consultar"
            total_txt  = f"${p_total:,.0f} COP" if precio_final > 0 else "Precio a confirmar"

            mensaje = f"""Hola! Le comparto la cotizacion del color que solicito:

🎨 *{ral_input} — {nombre}*
📋 Grupo {grupo} ({dg[0]}) | Base {base}{prod_txt}

Para pintar *{area_ral:.0f} m²* con {manos_ral} mano(s) necesita:
• *{galones} galones* de pintura
• Precio por galon (Grupo {grupo}): *{precio_txt}*
• *Total estimado: {total_txt}*

Este color usa base {base} ({base_msg}), garantizando la fidelidad exacta del tono RAL.{obs_msg}

Desea que le generemos la cotizacion formal? 🏗️ OBRIXA"""

            st.markdown("#### 📱 Mensaje listo para el cliente")
            st.text_area("Copia y pega en WhatsApp:", value=mensaje, height=320)

        elif ral_input and ral_input not in COLORES_RAL:
            st.warning(f"El color {ral_input} no esta en la paleta SW. Verifica el codigo.")
            similares = [r for r in COLORES_RAL if r[:5] == ral_input[:5]]
            if similares:
                st.caption(f"Colores similares: {\', \'.join(similares[:8])}")
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
