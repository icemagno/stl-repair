#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
reparar_stl.py — Reparador Universal e Realçador de Detalhes 3D (STL) para ComfyUI / RPG Minis
==============================================================================================
100% Autônomo (NÃO depende do Blender instalado — usa o kernel C++ nativo manifold3d em memória).

Resolve os problemas de malhas geradas por IA (Hunyuan3D, TripoSR, Trellis, etc.) e otimiza
a definição de detalhes finos (olhos, nariz, boca, dedos, ornamentos) para impressão em bico 0.4mm:
  1. Remove casca interna oca (preenche 100% do interior mantendo vãos e arcos externos)
  2. Remove fragmentos isolados flutuando fora da peça
  3. Sela furos, rasgos, arestas abertas e faces não-manifold (100% 2-manifold estanque)
  4. Funciona COM ou SEM base/pedestal (não cria base falsa entre pernas separadas)
  5. Realce de Micro-Detalhes 3D (3D Unsharp Mask nas normais) calibrado para bico 0.4mm
  6. Proteção de espessura mínima para evitar falhas de parede fina no fatiador com bico 0.4mm
  7. Simplificação geométrica adaptativa em C++ (manifold3d.simplify) com tolerância micrométrica

Como usar no PowerShell / CMD:
  C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\all.stl --referencia C:\Magno\Projetos\referencia.stl
  C:\Magno\Projetos\reparar_stl.bat minha_miniatura.stl --altura 45 --realce 0.75
  C:\Magno\Projetos\reparar_stl.bat peca_sem_base.stl --altura 60 --diagnostico
"""

import os
import sys
import time
import argparse
from collections import Counter

import numpy as np
import trimesh
import cv2
import manifold3d
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import marching_cubes


def diagnosticar_malha(mesh, nome="Malha"):
    """Retorna um dicionário com métricas topológicas e geométricas detalhadas."""
    edges = mesh.edges_sorted
    edge_counts = Counter(map(tuple, edges))
    open_edges = sum(1 for c in edge_counts.values() if c == 1)
    manifold_edges = sum(1 for c in edge_counts.values() if c == 2)
    nonman_edges = sum(1 for c in edge_counts.values() if c > 2)

    comps = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces))
    )
    ext = mesh.extents if len(mesh.vertices) > 0 else np.zeros(3)
    try:
        vol = float(mesh.volume)
    except Exception:
        vol = 0.0

    return {
        "nome": nome,
        "vertices": len(mesh.vertices),
        "faces": len(mesh.faces),
        "componentes": len(comps),
        "arestas_total": len(edge_counts),
        "arestas_manifold": manifold_edges,
        "arestas_abertas": open_edges,
        "arestas_nao_manifold": nonman_edges,
        "estanque": bool(mesh.is_watertight),
        "normais_consistentes": bool(mesh.is_winding_consistent),
        "volume": vol,
        "area": float(mesh.area),
        "extents": ext,
        "bounds": mesh.bounds.copy() if len(mesh.vertices) > 0 else np.zeros((2, 3)),
    }


def imprimir_relatorio(info):
    ext = info["extents"]
    print(f"  [{info['nome']}]")
    print(f"    Vértices / Faces     : {info['vertices']:,} / {info['faces']:,}")
    print(f"    Dimensões (X, Y, Z)  : {ext[0]:.3f} x {ext[1]:.3f} x {ext[2]:.3f}")
    print(f"    Componentes (Ilhas)  : {info['componentes']}")
    print(f"    Arestas Abertas      : {info['arestas_abertas']:,}")
    print(f"    Arestas Não-Manifold : {info['arestas_nao_manifold']:,}")
    print(f"    Estanque (Manifold)  : {'SIM' if info['estanque'] else 'NÃO'}")
    print(f"    Volume / Área        : {info['volume']:.4f} / {info['area']:.4f}")


def orientar_malha(verts, modo_eixo="auto"):
    """Converte a orientação da malha para Z-up."""
    v = verts.copy()
    spans = v.max(axis=0) - v.min(axis=0)

    if modo_eixo == "auto":
        if spans.max() <= 5.0 and spans[1] >= max(spans[0], spans[2]) * 0.85:
            modo_eixo = "y"
        else:
            modo_eixo = "z"

    if modo_eixo == "y":
        print("  -> Orientação: convertendo Y-up (padrão ComfyUI) para Z-up (padrão Slicer)")
        return np.column_stack([v[:, 0], -v[:, 2], v[:, 1]])
    elif modo_eixo == "-y":
        return np.column_stack([v[:, 0], v[:, 2], -v[:, 1]])
    elif modo_eixo == "x":
        return np.column_stack([-v[:, 2], v[:, 1], v[:, 0]])
    elif modo_eixo == "-z":
        return np.column_stack([v[:, 0], -v[:, 1], -v[:, 2]])
    else:
        print("  -> Orientação: mantendo eixo Z original como vertical (Z-up)")
        return v


def filtrar_fragmentos_isolados(mesh, min_area_ratio=0.005, max_dist_ratio=0.015):
    """Remove fragmentos isolados flutuando fora da peça, preservando partes principais."""
    comps = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces))
    )
    if len(comps) <= 1:
        return mesh, 0

    comps = sorted(comps, key=len, reverse=True)
    main_sub = mesh.submesh([comps[0]], append=True)
    main_area = main_sub.area
    diag = np.linalg.norm(main_sub.extents)
    main_tree = cKDTree(main_sub.vertices)

    kept_faces = [comps[0]]
    removed_count = 0

    for c in comps[1:]:
        if len(c) < 25:
            removed_count += 1
            continue
        sub = mesh.submesh([c], append=True)
        area_ratio = sub.area / (main_area + 1e-12)
        dists, _ = main_tree.query(sub.vertices)
        min_dist = float(dists.min())

        if area_ratio >= min_area_ratio and min_dist <= max_dist_ratio * diag:
            kept_faces.append(c)
        else:
            removed_count += 1

    cleaned = mesh.submesh([np.concatenate(kept_faces)], append=True)
    return cleaned, removed_count


def construir_volume_solido_universal(mesh, res_z=1000, modo_base="auto"):
    """Reconstrói o volume 3D 100% sólido da peça em alta resolução (COM ou SEM base)."""
    bmin, bmax = mesh.bounds
    spans = bmax - bmin

    NZ = int(np.clip(res_z, 400, 1400))
    NX = int(np.clip(round(NZ * max(0.55, spans[0] / (spans[2] + 1e-9))), 520, 900))
    NY = int(np.clip(round(NZ * max(0.55, spans[1] / (spans[2] + 1e-9))), 520, 900))

    pad = 0.01 * spans.max()
    gmin = bmin - pad
    gmax = bmax + pad
    dx = (gmax[0] - gmin[0]) / (NX - 1)
    dy = (gmax[1] - gmin[1]) / (NY - 1)
    dz = (gmax[2] - gmin[2]) / (NZ - 1)

    print(f"  -> Grade volumétrica 3D: {NX} x {NY} x {NZ} (dx={dx:.5f}, dy={dy:.5f}, dz={dz:.5f})")

    z_planes = np.linspace(gmin[2], gmax[2], NZ)
    sections = mesh.section_multiplane(
        plane_origin=[0, 0, 0], plane_normal=[0, 0, 1], heights=z_planes
    )

    solid_vol = np.zeros((NZ, NY, NX), dtype=np.uint8)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    for iz, sec in enumerate(sections):
        if sec is None:
            continue
        img = np.zeros((NY, NX), dtype=np.uint8)
        for path in sec.discrete:
            px = np.round((path[:, 0] - gmin[0]) / dx).astype(np.int32)
            py = np.round((path[:, 1] - gmin[1]) / dy).astype(np.int32)
            pts = np.column_stack([px, py]).reshape((-1, 1, 2))
            cv2.polylines(img, [pts], isClosed=False, color=255, thickness=1)
        img_closed = cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel_close)
        ff = img_closed.copy()
        mask = np.zeros((NY + 2, NX + 2), np.uint8)
        cv2.floodFill(ff, mask, (0, 0), 128)
        solid_vol[iz] = (ff != 128).astype(np.uint8)

    z_close_slices = max(9, int(round(0.016 * NZ)) | 1)
    z_struct = np.ones((z_close_slices, 1, 1), dtype=bool)
    solid_vol = ndimage.binary_closing(solid_vol > 0, structure=z_struct).astype(np.uint8)

    solid_vol = ndimage.binary_closing(
        solid_vol > 0, structure=np.ones((3, 3, 3), dtype=bool)
    ).astype(np.uint8)

    iz_bot = int(np.ceil((bmin[2] - gmin[2]) / dz))
    iz_ref = min(NZ - 1, iz_bot + max(3, int(round(0.005 * spans[2] / dz))))
    if modo_base in ("auto", "plana"):
        for iz in range(iz_bot, iz_ref + 1):
            solid_vol[iz] = np.maximum(solid_vol[iz], solid_vol[iz_ref])
        solid_vol[:iz_bot] = 0

    struct3d = ndimage.generate_binary_structure(3, 1)
    labels, n_lbls = ndimage.label(solid_vol > 0, structure=struct3d)
    if n_lbls > 1:
        counts = np.bincount(labels.ravel())
        counts[0] = 0
        max_vox = counts.max()
        keep_lbls = np.where(counts >= max_vox * 0.01)[0]
        solid_vol = np.isin(labels, keep_lbls).astype(np.uint8)
        print(f"  -> Componentes 3D no volume: {n_lbls} (mantidos {len(keep_lbls)} principais)")

    solid_vol = ndimage.binary_fill_holes(solid_vol > 0).astype(np.uint8)
    return solid_vol, gmin, (dx, dy, dz), iz_bot


def extrair_e_realcar_superficie(
    mesh_orig,
    solid_vol,
    gmin,
    spacing,
    iz_bot,
    modo_base="auto",
    realce_detalhes=0.65,
    espessura_min_mm=0.50,
    altura_alvo_mm=75.0,
):
    """
    Extrai a malha estanque, projeta nos planos exatos dos triângulos originais (10 pontos/face),
    aplica Realce de Micro-Detalhes 3D (3D Unsharp Mask) e protege espessura mínima para bico 0.4mm.
    """
    dx, dy, dz = spacing
    NZ, NY, NX = solid_vol.shape
    bmin, bmax = mesh_orig.bounds
    altura_orig = bmax[2] - bmin[2]
    max_step = max(dx, dy, dz)

    mm_por_unidade = altura_alvo_mm / (altura_orig + 1e-9)
    unidades_por_mm = 1.0 / mm_por_unidade

    dist_in = ndimage.distance_transform_edt(solid_vol > 0, sampling=(dz, dy, dx))
    dist_out = ndimage.distance_transform_edt(solid_vol == 0, sampling=(dz, dy, dx))
    sdf = dist_out - dist_in

    def eval_grid(pts, grid):
        ix = np.clip(np.round((pts[:, 0] - gmin[0]) / dx).astype(int), 0, NX - 1)
        iy = np.clip(np.round((pts[:, 1] - gmin[1]) / dy).astype(int), 0, NY - 1)
        iz = np.clip(np.round((pts[:, 2] - gmin[2]) / dz).astype(int), 0, NZ - 1)
        return grid[iz, iy, ix]

    c_pts = mesh_orig.triangles_center
    n_vec = mesh_orig.face_normals
    probe_step = 2.0 * max_step
    sdf_c = eval_grid(c_pts, sdf)
    sdf_out = eval_grid(c_pts + probe_step * n_vec, sdf)
    sdf_in = eval_grid(c_pts - probe_step * n_vec, sdf)

    outer_mask = (np.abs(sdf_c) < 2.2 * max_step) & (sdf_out > sdf_in)
    if np.any(outer_mask):
        outer_mesh = mesh_orig.submesh([np.where(outer_mask)[0]], append=True)
    else:
        outer_mesh = mesh_orig

    print(f"  -> Triângulos externos de referência identificados: {len(outer_mesh.faces):,}")

    sdf_smooth = ndimage.gaussian_filter(sdf, sigma=0.6)
    if modo_base in ("auto", "plana"):
        sdf_smooth[:iz_bot] = np.abs(sdf_smooth[:iz_bot]) + dz

    verts_mc, faces_mc, _, _ = marching_cubes(sdf_smooth, level=0.0, spacing=(dz, dy, dx))
    verts_xyz = np.column_stack([
        verts_mc[:, 2] + gmin[0],
        verts_mc[:, 1] + gmin[1],
        verts_mc[:, 0] + gmin[2],
    ])
    faces_xyz = faces_mc[:, [0, 2, 1]]
    repaired = trimesh.Trimesh(vertices=verts_xyz, faces=faces_xyz, process=True)

    # Suaviza o serrilhado de voxel ANTES de cravar na superfície original!
    trimesh.smoothing.filter_taubin(repaired, lamb=0.5, nu=-0.53, iterations=4)

    ov0 = outer_mesh.vertices[outer_mesh.faces[:, 0]]
    ov1 = outer_mesh.vertices[outer_mesh.faces[:, 1]]
    ov2 = outer_mesh.vertices[outer_mesh.faces[:, 2]]
    fn = outer_mesh.face_normals

    sample_pts = np.vstack([
        (ov0 + ov1 + ov2) / 3.0,
        0.5 * (ov0 + ov1),
        0.5 * (ov1 + ov2),
        0.5 * (ov2 + ov0),
        0.6 * ov0 + 0.2 * ov1 + 0.2 * ov2,
        0.2 * ov0 + 0.6 * ov1 + 0.2 * ov2,
        0.2 * ov0 + 0.2 * ov1 + 0.6 * ov2,
        ov0, ov1, ov2,
    ])
    sample_norms = np.vstack([fn] * 10)

    tree = cKDTree(sample_pts)
    dists, idxs = tree.query(repaired.vertices)

    max_snap_dist = 2.0 * max_step
    z_margin = (bmin[2] + 3.0 * dz) if modo_base in ("auto", "plana") else (bmin[2] - dz)
    snap_mask = (repaired.vertices[:, 2] > z_margin) & (dists < max_snap_dist)

    v_curr = repaired.vertices[snap_mask]
    idx_snap = idxs[snap_mask]
    p_targ = sample_pts[idx_snap]
    n_targ = sample_norms[idx_snap]

    normal_disp = np.sum((p_targ - v_curr) * n_targ, axis=1, keepdims=True)
    normal_disp = np.clip(normal_disp, -1.2 * max_step, 1.2 * max_step)
    repaired.vertices[snap_mask] = v_curr + 0.92 * normal_disp * n_targ

    if realce_detalhes > 0.0:
        print(f"  -> Aplicando Realce de Micro-Detalhes 3D (intensidade={realce_detalhes:.2f}) para olhos, nariz e relevos...")
        mesh_base = repaired.copy()
        trimesh.smoothing.filter_taubin(mesh_base, lamb=0.5, nu=-0.53, iterations=8)

        v_norms = repaired.vertex_normals
        detail_vec = repaired.vertices - mesh_base.vertices
        detail_scalar = np.sum(detail_vec * v_norms, axis=1, keepdims=True)

        max_boost_unit = 0.16 * unidades_por_mm
        boost_disp = np.clip(realce_detalhes * detail_scalar, -max_boost_unit, max_boost_unit)
        detail_mask = (repaired.vertices[:, 2] > z_margin).reshape(-1, 1)
        repaired.vertices += detail_mask * boost_disp * v_norms

    if espessura_min_mm > 0.0:
        raio_min_unit = 0.5 * espessura_min_mm * unidades_por_mm
        v_norms = repaired.vertex_normals
        local_half_thick = eval_grid(repaired.vertices - raio_min_unit * v_norms, dist_in)
        thin_deficit = np.clip(raio_min_unit - local_half_thick, 0.0, 0.12 * unidades_por_mm).reshape(-1, 1)
        thin_mask = (repaired.vertices[:, 2] > z_margin).reshape(-1, 1) & (thin_deficit > 0.0)
        if np.any(thin_mask):
            repaired.vertices += 0.5 * thin_mask * thin_deficit * v_norms

    trimesh.smoothing.filter_taubin(repaired, lamb=0.35, nu=-0.37, iterations=1)

    if modo_base in ("auto", "plana"):
        z_floor = bmin[2]
        bot_mask = repaired.vertices[:, 2] <= z_floor + 2.2 * dz
        repaired.vertices[bot_mask, 2] = z_floor

    return repaired


def otimizar_e_validar_manifold(mesh, target_faces=600000, modo_base="auto"):
    """
    Otimiza a contagem de triângulos e valida 100% 2-manifold estanque diretamente em memória
    usando o kernel C++ nativo manifold3d (SEM depender do Blender instalado!).
    """
    mesh.vertices[:, 0] -= 0.5 * (mesh.bounds[0, 0] + mesh.bounds[1, 0])
    mesh.vertices[:, 1] -= 0.5 * (mesh.bounds[0, 1] + mesh.bounds[1, 1])
    mesh.vertices[:, 2] -= mesh.bounds[0, 2]

    comps = mesh.split(only_watertight=False)
    if len(comps) > 1:
        comps = sorted(comps, key=lambda m: len(m.faces), reverse=True)
        max_f = len(comps[0].faces)
        kept = [m for m in comps if len(m.faces) >= max_f * 0.01]
        mesh = trimesh.util.concatenate(kept) if len(kept) > 1 else comps[0]

    n_in = len(mesh.faces)
    span_max = float(mesh.extents.max())

    m_in = manifold3d.Mesh(
        vert_properties=mesh.vertices.astype(np.float32),
        tri_verts=mesh.faces.astype(np.uint32),
    )
    man = manifold3d.Manifold(m_in)

    # Simplificação adaptativa em C++ (manifold3d.simplify) preservando detalhes curvos
    if n_in > target_faces * 1.1:
        # Tolerância micrométrica proporcional (~0.008% da altura da peça = ~0.006 mm em peça de 75mm)
        tol = 0.00008 * span_max * np.sqrt(float(n_in) / float(max(100000, target_faces)) * 0.1)
        tol = float(np.clip(tol, 0.00003 * span_max, 0.00025 * span_max))
        print(f"  -> Otimizando malha em C++ via manifold3d.simplify ({n_in:,} faces, tol={tol:.6f})...")
        man = man.simplify(tol)

    m_out = man.to_mesh()
    mesh = trimesh.Trimesh(
        vertices=m_out.vert_properties[:, :3], faces=m_out.tri_verts, process=True
    )
    print(f"  -> Malha 2-manifold otimizada: {len(mesh.faces):,} faces")

    mesh.vertices[:, 2] -= mesh.bounds[0, 2]
    if modo_base in ("auto", "plana"):
        dz_tol = 0.0015 * mesh.extents[2]
        bot_m = mesh.vertices[:, 2] <= dz_tol
        mesh.vertices[bot_m, 2] = 0.0

    mesh.fix_normals()
    return mesh


def estimar_altura_alvo_mm(mesh, args):
    """Estima a altura final em mm para calibrar o realce de detalhes e espessura mínima."""
    ext = mesh.extents
    largura_atual = 0.5 * (ext[0] + ext[1])
    altura_atual = ext[2]
    if args.fator_escala is not None:
        return altura_atual * float(args.fator_escala)
    if args.altura is not None:
        return float(args.altura)
    if args.largura_base is not None:
        return altura_atual * (float(args.largura_base) / largura_atual)
    if args.referencia and os.path.exists(args.referencia):
        ref = trimesh.load(args.referencia, force="mesh")
        ref_ext = ref.extents
        if args.escala_ref == "altura":
            return float(ref_ext[2])
        ref_base = 0.5 * (ref_ext[0] + ref_ext[1])
        return altura_atual * (ref_base / largura_atual)
    return 75.0 if altura_atual <= 5.0 else float(altura_atual)


def aplicar_escala_final(mesh, args):
    """Aplica a escala desejada em milímetros (por altura, largura de base, referência ou fator)."""
    ext = mesh.extents
    largura_atual = 0.5 * (ext[0] + ext[1])
    altura_atual = ext[2]
    fator = 1.0

    if args.fator_escala is not None:
        fator = float(args.fator_escala)
        print(f"  -> Aplicando fator de escala direto: {fator:.4f}x")
    elif args.altura is not None:
        fator = float(args.altura) / altura_atual
        print(f"  -> Escalando para altura final de {args.altura:.2f} mm (fator {fator:.2f}x)")
    elif args.largura_base is not None:
        fator = float(args.largura_base) / largura_atual
        print(f"  -> Escalando para largura/diâmetro de {args.largura_base:.2f} mm (fator {fator:.2f}x)")
    elif args.referencia and os.path.exists(args.referencia):
        ref = trimesh.load(args.referencia, force="mesh")
        ref_ext = ref.extents
        if args.escala_ref == "altura":
            fator = ref_ext[2] / altura_atual
            print(f"  -> Escalando pela ALTURA da referência ({ref_ext[2]:.2f} mm, fator {fator:.2f}x)")
        else:
            ref_base = 0.5 * (ref_ext[0] + ref_ext[1])
            fator = ref_base / largura_atual
            print(f"  -> Escalando pela LARGURA/BASE da referência ({ref_base:.2f} mm, fator {fator:.2f}x)")
    elif altura_atual <= 5.0:
        fator = 75.0 / altura_atual
        print(f"  -> Malha em escala normalizada ({altura_atual:.3f}): escalando automaticamente para 75.0 mm de altura")

    if abs(fator - 1.0) > 1e-6:
        mesh.apply_scale(fator)

    mesh.vertices[:, 0] -= 0.5 * (mesh.bounds[0, 0] + mesh.bounds[1, 0])
    mesh.vertices[:, 1] -= 0.5 * (mesh.bounds[0, 1] + mesh.bounds[1, 1])
    mesh.vertices[:, 2] -= mesh.bounds[0, 2]
    return mesh


def salvar_imagem_diagnostico(mesh_reparada, out_png):
    """Salva uma prancha visual com cortes horizontais, verticais e vistas sombreadas."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 4, figsize=(20, 10))
    fig.suptitle(f"Diagnóstico da Peça Reparada: {os.path.basename(out_png)}", fontsize=15)

    bmin, bmax = mesh_reparada.bounds
    dz = bmax[2] - bmin[2]
    for idx, frac in enumerate([0.15, 0.45, 0.75]):
        ax = axes[0, idx]
        zv = bmin[2] + frac * dz
        sec = mesh_reparada.section(plane_origin=[0, 0, zv], plane_normal=[0, 0, 1])
        if sec is not None:
            for ent in sec.entities:
                pts = sec.vertices[ent.points]
                ax.plot(pts[:, 0], pts[:, 1], "k-", linewidth=1.0)
        ax.set_title(f"Corte Horizontal Z={zv:.1f}mm ({frac*100:.0f}%)")
        ax.set_aspect("equal")
        ax.grid(True, alpha=0.3)

    ax = axes[0, 3]
    sec_v = mesh_reparada.section(plane_origin=[0, 0, 0], plane_normal=[0, 1, 0])
    if sec_v is not None:
        for ent in sec_v.entities:
            pts = sec_v.vertices[ent.points]
            ax.plot(pts[:, 0], pts[:, 2], "r-", linewidth=1.0)
    ax.set_title("Corte Vertical Y=0 (mm)")
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    sec_vx = mesh_reparada.section(plane_origin=[0, 0, 0], plane_normal=[1, 0, 0])
    if sec_vx is not None:
        for ent in sec_vx.entities:
            pts = sec_vx.vertices[ent.points]
            ax.plot(pts[:, 1], pts[:, 2], "b-", linewidth=1.0)
    ax.set_title("Corte Vertical X=0 (mm)")
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)

    centers = mesh_reparada.triangles_center
    normals = mesh_reparada.face_normals
    step = max(1, len(centers) // 70000)
    c_sub = centers[::step]
    n_sub = normals[::step]
    light = np.array([0.4, -0.6, 0.7])
    light /= np.linalg.norm(light)
    shade = np.clip(np.dot(n_sub, light), -1.0, 1.0)

    order_f = np.argsort(-c_sub[:, 1])
    axes[1, 1].scatter(c_sub[order_f, 0], c_sub[order_f, 2], c=shade[order_f], cmap="bone", s=0.8, vmin=-0.6, vmax=1)
    axes[1, 1].set_title("Vista Frontal")
    axes[1, 1].set_aspect("equal")

    order_s = np.argsort(c_sub[:, 0])
    axes[1, 2].scatter(c_sub[order_s, 1], c_sub[order_s, 2], c=shade[order_s], cmap="bone", s=0.8, vmin=-0.6, vmax=1)
    axes[1, 2].set_title("Vista Lateral")
    axes[1, 2].set_aspect("equal")

    light_b = np.array([-0.4, 0.6, 0.7])
    light_b /= np.linalg.norm(light_b)
    shade_b = np.clip(np.dot(n_sub, light_b), -1.0, 1.0)
    order_b = np.argsort(c_sub[:, 1])
    axes[1, 3].scatter(-c_sub[order_b, 0], c_sub[order_b, 2], c=shade_b[order_b], cmap="bone", s=0.8, vmin=-0.6, vmax=1)
    axes[1, 3].set_title("Vista Traseira")
    axes[1, 3].set_aspect("equal")

    plt.tight_layout()
    plt.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"  -> Imagem de diagnóstico salva em: {out_png}")


def main():
    parser = argparse.ArgumentParser(
        description="Reparador Universal e Realçador de Detalhes de arquivos STL gerados pelo ComfyUI."
    )
    parser.add_argument("entrada", help="Caminho do arquivo .stl de entrada (ex: C:\\Magno\\Projetos\\all.stl)")
    parser.add_argument("-o", "--saida", default=None, help="Caminho do arquivo .stl reparado de saída (padrão: <nome>_reparado.stl)")
    parser.add_argument("-r", "--referencia", default=None, help="Arquivo .stl de referência opcional para copiar a escala")
    parser.add_argument(
        "--escala-ref",
        choices=["base", "altura"],
        default="base",
        help="Ao usar --referencia, escalar igualando a 'base' (diâmetro/largura) ou a 'altura' (padrão: base)",
    )
    parser.add_argument("--altura", type=float, default=None, help="Altura final desejada em milímetros (ex: 75.0 ou 43.5)")
    parser.add_argument("--largura-base", type=float, default=None, help="Largura/diâmetro da base desejado em mm (ex: 23.46)")
    parser.add_argument("--fator-escala", type=float, default=None, help="Fator multiplicador direto de escala")
    parser.add_argument(
        "--eixo-vertical",
        choices=["auto", "y", "z", "x", "-y", "-z"],
        default="auto",
        help="Eixo vertical da peça original (padrão: auto — detecta Y-up do ComfyUI automaticamente)",
    )
    parser.add_argument(
        "--base",
        choices=["auto", "plana", "preservar"],
        default="auto",
        help="Tratamento do fundo da peça: 'auto'/'plana' nivela o apoio em Z=0; 'preservar' não altera o fundo",
    )
    parser.add_argument(
        "--realce",
        type=float,
        default=0.65,
        help="Intensidade do Realce de Micro-Detalhes 3D para olhos, nariz e relevos (padrão: 0.65; use 0 para desativar)",
    )
    parser.add_argument(
        "--espessura-minima",
        type=float,
        default=0.50,
        help="Espessura mínima de parede em mm para proteger pontas finas em bico 0.4mm (padrão: 0.50 mm)",
    )
    parser.add_argument(
        "--resolucao",
        type=int,
        default=1000,
        help="Resolução vertical Z da grade volumétrica 3D (padrão: 1000)",
    )
    parser.add_argument(
        "--faces",
        type=int,
        default=600000,
        help="Quantidade alvo aproximada de triângulos na malha final otimizada (padrão: 600000)",
    )
    parser.add_argument(
        "--diagnostico",
        action="store_true",
        help="Gera uma imagem PNG com cortes transversais e vistas 3D da peça reparada",
    )

    args = parser.parse_args()

    if not os.path.exists(args.entrada):
        print(f"[ERRO] Arquivo de entrada não encontrado: {args.entrada}")
        sys.exit(1)

    if args.saida is None:
        base_path, _ = os.path.splitext(args.entrada)
        args.saida = f"{base_path}_reparado.stl"

    t_start = time.time()
    print("=" * 72)
    print(f"INICIANDO REPARO E REALCE 3D: {args.entrada}")
    print("=" * 72)

    raw_mesh = trimesh.load(args.entrada, force="mesh")
    info_antes = diagnosticar_malha(raw_mesh, "ANTES DO REPARO (Original)")
    imprimir_relatorio(info_antes)

    verts_z = orientar_malha(raw_mesh.vertices, args.eixo_vertical)
    mesh = trimesh.Trimesh(vertices=verts_z, faces=raw_mesh.faces.copy(), process=True)

    mesh_clean, n_rem = filtrar_fragmentos_isolados(mesh)
    print(f"  -> Fragmentos isolados removidos: {n_rem} (restaram {len(mesh_clean.faces):,} faces na peça)")

    altura_alvo = estimar_altura_alvo_mm(mesh_clean, args)

    print("  -> Executando preenchimento sólido 3D de alta resolução...")
    solid_vol, gmin, spacing, iz_bot = construir_volume_solido_universal(
        mesh_clean, res_z=args.resolucao, modo_base=args.base
    )

    print("  -> Extraindo superfície estanque, cravando detalhes originais e aplicando realce 3D...")
    repaired = extrair_e_realcar_superficie(
        mesh_clean,
        solid_vol,
        gmin,
        spacing,
        iz_bot,
        modo_base=args.base,
        realce_detalhes=args.realce,
        espessura_min_mm=args.espessura_minima,
        altura_alvo_mm=altura_alvo,
    )

    repaired = otimizar_e_validar_manifold(
        repaired, target_faces=args.faces, modo_base=args.base
    )

    repaired = aplicar_escala_final(repaired, args)

    repaired.export(args.saida)
    info_depois = diagnosticar_malha(repaired, "DEPOIS DO REPARO (Final)")
    print("-" * 72)
    imprimir_relatorio(info_depois)
    print("-" * 72)
    print(f"Arquivo STL salvo com sucesso em: {args.saida} ({os.path.getsize(args.saida)/1e6:.2f} MB)")
    print(f"Tempo total: {time.time() - t_start:.1f}s")

    if args.diagnostico:
        png_path = os.path.splitext(args.saida)[0] + "_diagnostico.png"
        salvar_imagem_diagnostico(repaired, png_path)


if __name__ == "__main__":
    main()
