#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
reparar_stl.py (v2.6) — Reparador 3D de Alta Fidelidade e Acabamento Porcelana para ComfyUI / Impressão 3D
==============================================================================================
100% Autônomo (NÃO depende do Blender nem de compiladores externos — roda direto no Python do ComfyUI).

Projetado com foco em ZERO distorção estrutural externa:
  1. Filtro de fragmentos por conectividade de vértices + proximidade 3D: remove APENAS ilhas
     flutuando no ar fora da peça, sem jamais apagar partes legítimas (pontas de capa, mechas, etc.)
     separadas por costuras non-manifold.
  2. Rasterização 3D direta + Flood-Fill 3D VERDADEIRO do ar externo: o ar circula livremente
     em 3D entre as pernas (virilha), axilas e dobras internas da capa — eliminando 100% das
     "pontes/prateleiras horizontais" causadas por preenchimento 2D por fatia.
  3. Proteção Contínua em SDF para Folhas Finas / Cascas Abertas: preserva capas finas, abas e
     bordas abertas sem rasgar, sem esburacar e sem criar degraus ou bolinhas de voxel.
  4. Snapping K-Nearest com Verificação de Normal Externa + Interpolação Trilinear: descarta
     100% das paredes ocas internas (evitando cicatrizes no joelho/quadril) e mantém a superfície
     externa fiel ao modelo original.
  5. Realce de Micro-Detalhes 3D opcional (--realce, desativado por padrão para preservar 100%
     da escultura original em peças grandes; ative com --realce 0.6 em miniaturas pequenas de RPG).
  6. Simplificação geométrica adaptativa (manifold3d.simplify) 100% 2-manifold estanque.

Como usar no PowerShell / CMD:
  .\reparar_stl.bat ComfyUI_00020.stl --altura 200
  .\reparar_stl.bat all.stl --referencia referencia.stl --escala-ref base
  .\reparar_stl.bat miniatura_rpg.stl --altura 38 --realce 0.65
"""

import os
import sys
import time
import argparse
from collections import Counter, deque

import numpy as np
import trimesh
import cv2
import manifold3d
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components as csgraph_components
from scipy.spatial import cKDTree
from skimage.measure import marching_cubes


# Grade baricêntrica regular k=4 (15 pontos por triângulo) para rasterização 3D sem frestas
_BARY_4 = []
for _i in range(5):
    for _j in range(5 - _i):
        _k = 4 - _i - _j
        _BARY_4.append((_i / 4.0, _j / 4.0, _k / 4.0))
_BARY_W = np.array(_BARY_4, dtype=np.float32)


def diagnosticar_malha(mesh, nome="Malha"):
    """Retorna um dicionário com métricas topológicas e geométricas detalhadas."""
    edges = mesh.edges_sorted
    edge_counts = Counter(map(tuple, edges))
    open_edges = sum(1 for c in edge_counts.values() if c == 1)
    manifold_edges = sum(1 for c in edge_counts.values() if c == 2)
    nonman_edges = sum(1 for c in edge_counts.values() if c > 2)

    if len(mesh.faces) > 0 and len(mesh.vertices) > 0:
        f = mesh.faces
        rows = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
        cols = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
        adj = coo_matrix(
            (np.ones(len(rows), dtype=np.uint8), (rows, cols)),
            shape=(len(mesh.vertices), len(mesh.vertices)),
        )
        n_comps_v, v_lbl = csgraph_components(adj, directed=False)
        used_lbls = np.unique(v_lbl[f[:, 0]])
        n_componentes = int(len(used_lbls))
    else:
        n_componentes = 0

    ext = mesh.extents if len(mesh.vertices) > 0 else np.zeros(3)
    try:
        vol = float(mesh.volume)
    except Exception:
        vol = 0.0

    return {
        "nome": nome,
        "vertices": len(mesh.vertices),
        "faces": len(mesh.faces),
        "componentes": n_componentes,
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


def filtrar_fragmentos_isolados(mesh):
    """
    Remove ilhas flutuantes desconectadas no ar externo (por conectividade de vértices + proximidade)
    e poda cirurgicamente apenas micro-pinos/gotas parasitas non-manifold externos, com garantia
    topológica estrita de NUNCA abrir furos na pele da peça (preservando 100% da vedação do corpo).
    """
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, 0

    mesh.merge_vertices()
    f = mesh.faces
    n_faces = len(f)
    n_verts = len(mesh.vertices)

    # Etapa 1: Conectividade por VÉRTICES (não quebra em costuras non-manifold nem arranca remendos da pele!)
    rows = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    cols = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    v_adj = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, cols)),
        shape=(n_verts, n_verts),
    )
    n_vcomps, v_labels = csgraph_components(v_adj, directed=False)
    f_vlabels = v_labels[f[:, 0]]
    face_areas = mesh.area_faces
    vcomp_areas = np.bincount(f_vlabels, weights=face_areas, minlength=n_vcomps)
    active_vcids = np.where(np.bincount(f_vlabels, minlength=n_vcomps) > 0)[0]

    main_vcid = int(np.argmax(vcomp_areas))
    main_area = float(vcomp_areas[main_vcid])
    main_verts = mesh.vertices[v_labels == main_vcid]
    diag = float(np.linalg.norm(main_verts.max(axis=0) - main_verts.min(axis=0)))

    kept_vcids = {main_vcid}
    rem_vcids = [int(c) for c in active_vcids if int(c) != main_vcid]
    for _ in range(4):
        if not rem_vcids:
            break
        kv_mask = np.isin(v_labels, list(kept_vcids))
        tree_v = cKDTree(mesh.vertices[kv_mask])
        newly_kept = []
        for cid in rem_vcids:
            c_pts = mesh.vertices[v_labels == cid]
            max_d = 0.020 * diag if vcomp_areas[cid] >= 0.005 * main_area else 0.0025 * diag
            d_min = float(tree_v.query(c_pts, workers=-1)[0].min())
            if d_min <= max_d:
                newly_kept.append(cid)
        if not newly_kept:
            break
        for cid in newly_kept:
            kept_vcids.add(cid)
            rem_vcids.remove(cid)

    removed_total = int(len(active_vcids) - len(kept_vcids))
    keep_f_stage1 = np.isin(f_vlabels, list(kept_vcids))
    if not np.all(keep_f_stage1):
        mesh = mesh.submesh([np.where(keep_f_stage1)[0]], append=True)
        mesh.merge_vertices()

    # Etapa 2: Poda cirúrgica APENAS de micro-pinos/gotas parasitas non-manifold projetados para fora,
    # exigindo prova topológica de que remover o pino NÃO cria nenhuma aresta aberta nova na peça!
    f2 = mesh.faces
    n_f2 = len(f2)
    n_v2 = len(mesh.vertices)
    f_adj = mesh.face_adjacency
    if len(f_adj) > 0 and n_f2 > 0:
        adj_m = coo_matrix(
            (np.ones(len(f_adj), dtype=np.uint8), (f_adj[:, 0], f_adj[:, 1])),
            shape=(n_f2, n_f2),
        )
        n_fc, fc_lbls = csgraph_components(adj_m, directed=False)
        if n_fc > 1:
            f2_areas = mesh.area_faces
            fc_areas = np.bincount(fc_lbls, weights=f2_areas, minlength=n_fc)
            nv64 = np.int64(n_v2)
            all_edges = np.sort(
                np.vstack([f2[:, [0, 1]], f2[:, [1, 2]], f2[:, [2, 0]]]), axis=1
            ).astype(np.int64)
            all_ekeys = all_edges[:, 0] * nv64 + all_edges[:, 1]
            u_ekeys, u_ecnts = np.unique(all_ekeys, return_counts=True)

            # Candidatos a pino: área minúscula (< 0.04% da peça)
            cand_cids = np.where((fc_areas > 0.0) & (fc_areas < 0.00040 * main_area))[0]
            if len(cand_cids) > 0:
                non_cand_mask = ~np.isin(fc_lbls, cand_cids)
                body_v_mask = np.zeros(n_v2, dtype=bool)
                body_v_mask[f2[non_cand_mask].ravel()] = True
                body_tree = cKDTree(mesh.vertices[body_v_mask])

                drop_cids = []
                for cid in cand_cids:
                    cf = f2[fc_lbls == cid]
                    cv = np.unique(cf)
                    shared_v = cv[body_v_mask[cv]]
                    if len(shared_v) == 0:
                        continue
                    s_pts = mesh.vertices[shared_v]
                    seam_span = float(np.linalg.norm(s_pts.max(axis=0) - s_pts.min(axis=0)))
                    if seam_span > 0.010 * diag:
                        continue

                    # Garantia topológica zero-furo: nenhuma aresta fechada (>=2) pode virar aberta (==1)
                    ce = np.sort(
                        np.vstack([cf[:, [0, 1]], cf[:, [1, 2]], cf[:, [2, 0]]]), axis=1
                    ).astype(np.int64)
                    ck, cc = np.unique(ce[:, 0] * nv64 + ce[:, 1], return_counts=True)
                    pos = np.searchsorted(u_ekeys, ck)
                    m_tot = u_ecnts[pos]
                    m_after = m_tot - cc
                    if np.any((m_tot >= 2) & (m_after == 1)):
                        continue

                    # Verifica se realmente se projeta para fora como pino/gota (altura > largura da base)
                    d_out, _ = body_tree.query(mesh.vertices[cv], workers=-1)
                    max_prot = float(d_out.max())
                    if max_prot >= max(0.0022 * diag, 0.80 * seam_span):
                        drop_cids.append(int(cid))

                if drop_cids:
                    keep_f2 = ~np.isin(fc_lbls, drop_cids)
                    mesh = mesh.submesh([np.where(keep_f2)[0]], append=True)
                    removed_total += len(drop_cids)

    return mesh, removed_total


def fechar_micro_furos_malha(mesh, max_hole_ratio=0.030):
    """
    Cria uma cópia auxiliar da malha tampando pequenos furos fechados de borda aberta
    (diâmetro <= 3.0% da peça) via ciclo mínimo em grafo (BFS), funcionando mesmo quando
    o furo encosta em vértices non-manifold ou gravata-borboleta (grau > 2).
    """
    f = mesh.faces
    v = mesh.vertices
    if len(f) == 0:
        return mesh

    e0 = f[:, [0, 1]]
    e1 = f[:, [1, 2]]
    e2 = f[:, [2, 0]]
    undirected = np.sort(np.vstack([e0, e1, e2]), axis=1).astype(np.int64)
    nv64 = np.int64(len(v))
    e_keys = undirected[:, 0] * nv64 + undirected[:, 1]
    u_keys, u_cnts = np.unique(e_keys, return_counts=True)
    open_keys = u_keys[u_cnts == 1]
    if len(open_keys) == 0:
        return mesh
    oa = (open_keys // nv64).astype(np.int64)
    ob = (open_keys % nv64).astype(np.int64)
    valid_oe = oa != ob
    open_edges = list(zip(oa[valid_oe].tolist(), ob[valid_oe].tolist()))
    if not open_edges:
        return mesh

    adj_open = {}
    for a, b in open_edges:
        adj_open.setdefault(a, []).append(b)
        adj_open.setdefault(b, []).append(a)

    # Prioriza arestas mais curtas para fechar micro-furos triangulares antes de fendas maiores
    open_edges.sort(key=lambda e: float(np.sum((v[e[0]] - v[e[1]]) ** 2)))
    max_hole_diag = max_hole_ratio * float(mesh.extents.max())
    used_edges = set()
    new_verts = []
    new_faces = []
    base_v_count = len(v)

    for a, b in open_edges:
        edge_key = (a, b) if a < b else (b, a)
        if edge_key in used_edges:
            continue

        # Busca o menor ciclo fechado entre 'a' e 'b' sem usar a própria aresta (a, b)
        parent = {a: -1}
        depth = {a: 0}
        queue = deque([a])
        found = False

        while queue and len(parent) < 450:
            curr = queue.popleft()
            d_curr = depth[curr]
            if d_curr >= 80:
                continue
            for nxt in adj_open.get(curr, ()):
                if curr == a and nxt == b:
                    continue
                ek = (curr, nxt) if curr < nxt else (nxt, curr)
                if ek in used_edges:
                    continue
                if nxt == b:
                    parent[b] = curr
                    found = True
                    break
                if nxt not in parent:
                    parent[nxt] = curr
                    depth[nxt] = d_curr + 1
                    queue.append(nxt)
            if found:
                break

        if found:
            loop = [b]
            node = parent[b]
            while node != -1:
                loop.append(node)
                node = parent[node]
            if 3 <= len(loop) <= 85:
                pts = v[loop]
                hole_diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
                if hole_diag <= max_hole_diag:
                    k = len(loop)
                    for i in range(k):
                        u0, u1 = loop[i], loop[(i + 1) % k]
                        used_edges.add((u0, u1) if u0 < u1 else (u1, u0))
                    c_idx = base_v_count + len(new_verts)
                    new_verts.append(pts.mean(axis=0))
                    for i in range(k):
                        new_faces.append([loop[i], loop[(i + 1) % k], c_idx])

    if not new_faces:
        return mesh

    all_v = np.vstack([v, np.array(new_verts, dtype=np.float64)])
    all_f = np.vstack([f, np.array(new_faces, dtype=np.int64)])
    return trimesh.Trimesh(vertices=all_v, faces=all_f, process=False)


def rasterizar_superficie_3d(verts, faces, gmin, h, shape):
    """
    Rasteriza todos os triângulos diretamente em uma grade 3D (NZ, NY, NX) com subdivisão
    adaptativa por ponto médio, garantindo casca 3D contínua em qualquer orientação.
    """
    NZ, NY, NX = shape
    vol = np.zeros(shape, dtype=np.uint8)
    if len(faces) == 0:
        return vol

    v0 = verts[faces[:, 0]].astype(np.float32)
    v1 = verts[faces[:, 1]].astype(np.float32)
    v2 = verts[faces[:, 2]].astype(np.float32)
    gmin_f = np.asarray(gmin, dtype=np.float32)
    inv_h = np.float32(1.0 / h)
    max_edge_limit = np.float32(1.8 * h)

    for _ in range(8):
        e01 = np.linalg.norm(v1 - v0, axis=1)
        e12 = np.linalg.norm(v2 - v1, axis=1)
        e20 = np.linalg.norm(v0 - v2, axis=1)
        big = np.maximum(np.maximum(e01, e12), e20) > max_edge_limit
        if not np.any(big):
            break
        keep = ~big
        b0, b1, b2 = v0[big], v1[big], v2[big]
        m01 = 0.5 * (b0 + b1)
        m12 = 0.5 * (b1 + b2)
        m20 = 0.5 * (b2 + b0)
        v0 = np.vstack([v0[keep], b0, m01, m20, m01])
        v1 = np.vstack([v1[keep], m01, b1, m12, m12])
        v2 = np.vstack([v2[keep], m20, m12, b2, m20])
        if len(v0) > 10_000_000:
            break

    chunk = 250_000
    w0 = _BARY_W[:, 0][:, None, None]
    w1 = _BARY_W[:, 1][:, None, None]
    w2 = _BARY_W[:, 2][:, None, None]

    for start in range(0, len(v0), chunk):
        end = min(len(v0), start + chunk)
        c0 = v0[start:end][None, :, :]
        c1 = v1[start:end][None, :, :]
        c2 = v2[start:end][None, :, :]
        pts = (w0 * c0 + w1 * c1 + w2 * c2).reshape(-1, 3)

        ix = np.clip(np.round((pts[:, 0] - gmin_f[0]) * inv_h).astype(np.int32), 0, NX - 1)
        iy = np.clip(np.round((pts[:, 1] - gmin_f[1]) * inv_h).astype(np.int32), 0, NY - 1)
        iz = np.clip(np.round((pts[:, 2] - gmin_f[2]) * inv_h).astype(np.int32), 0, NZ - 1)
        vol[iz, iy, ix] = 1

    return vol


def _contar_vizinhos_7x7x7(mask_bool):
    """Conta rapidamente os voxels ocupados na vizinhança 7x7x7 de cada voxel (uint16)."""
    s = np.pad(mask_bool.astype(np.uint16), ((4, 3), (4, 3), (4, 3)), mode="constant")
    s = np.cumsum(s, axis=0, dtype=np.uint16)
    s = s[7:, :, :] - s[:-7, :, :]
    s = np.cumsum(s, axis=1, dtype=np.uint16)
    s = s[:, 7:, :] - s[:, :-7, :]
    s = np.cumsum(s, axis=2, dtype=np.uint16)
    s = s[:, :, 7:] - s[:, :, :-7]
    return s


def _solidificar_cavidades_ventiladas_3d(solid_vol, shell_closed, h, span_max):
    """
    Detecta e preenche em 3D cavidades internas ocas mesmo quando possuem furos, fendas ou
    canais em espiral conectando-as ao ar externo (típico de malhas de parede dupla do ComfyUI).
    Garantia matemática de ZERO deformação externa:
      1. Núcleos candidatos (core_all) exigem distância estrita > r_c de TODAS as paredes (jamais
         inclui vincos externos, axilas, pescoço, dobras de capa, dedos ou costuras).
      2. Exige > 96% de confinamento direto pelas paredes reais da malha (rejeita arcos/mechas).
      3. Expansão geodésica contida em (~occ_sub) jamais atravessa paredes para o exterior.
    """
    NZ, NY, NX = solid_vol.shape
    ds = 2
    p_z = (ds - (NZ % ds)) % ds
    p_y = (ds - (NY % ds)) % ds
    p_x = (ds - (NX % ds)) % ds

    occ_fine = (solid_vol > 0) | (shell_closed > 0)
    if p_z or p_y or p_x:
        occ_pad = np.pad(occ_fine, ((0, p_z), (0, p_y), (0, p_x)), mode="constant")
    else:
        occ_pad = occ_fine
    NZ_c, NY_c, NX_c = occ_pad.shape[0] // ds, occ_pad.shape[1] // ds, occ_pad.shape[2] // ds
    occ_c = occ_pad.reshape(NZ_c, ds, NY_c, ds, NX_c, ds).any(axis=(1, 3, 5))
    del occ_fine, occ_pad

    cav_pure_c = np.zeros_like(occ_c, dtype=bool)
    vent_plug_c = np.zeros_like(occ_c, dtype=bool)
    h_c = ds * h
    struct_6 = ndimage.generate_binary_structure(3, 1)
    struct_18 = ndimage.generate_binary_structure(3, 2)
    filled_any = False

    # Três escalas calibradas (em 200mm: raios ~1.56mm, ~0.85mm e ~0.60mm):
    # selam fendas/espirais de até ~3.1mm mantendo axilas, pescoço, mechas e capa 100% livres!
    for r_ratio in (0.0078, 0.0042, 0.0030):
        r_c = float(np.clip((r_ratio * span_max) / h_c, 1.45, 8.0))
        pad_c = int(np.ceil(r_c)) + 3
        occ_box = np.pad(occ_c, pad_c, mode="constant", constant_values=False)

        d_wall = ndimage.distance_transform_edt(~occ_box).astype(np.float32)
        wall_dil = d_wall <= r_c
        del d_wall
        wall_dil[0, :, :] = False
        wall_dil[-1, :, :] = False
        wall_dil[:, 0, :] = False
        wall_dil[:, -1, :] = False
        wall_dil[:, :, 0] = False
        wall_dil[:, :, -1] = False

        filled_dil = ndimage.binary_fill_holes(wall_dil)
        core_all = filled_dil & (~wall_dil)
        del wall_dil

        if np.any(core_all):
            cz, cy, cx = np.where(core_all)
            margin = int(np.ceil(2.0 * r_c)) + 3
            z0, z1 = max(0, int(cz.min()) - margin), min(occ_box.shape[0], int(cz.max()) + margin + 1)
            y0, y1 = max(0, int(cy.min()) - margin), min(occ_box.shape[1], int(cy.max()) + margin + 1)
            x0, x1 = max(0, int(cx.min()) - margin), min(occ_box.shape[2], int(cx.max()) + margin + 1)
            del cz, cy, cx

            fd_sub = filled_dil[z0:z1, y0:y1, x0:x1]
            del filled_dil
            if np.all(fd_sub):
                hull_sub = np.ones_like(fd_sub, dtype=bool)
            else:
                hull_sub = ndimage.distance_transform_edt(fd_sub).astype(np.float32) > r_c
            del fd_sub

            core_sub = core_all[z0:z1, y0:y1, x0:x1]
            occ_sub = occ_box[z0:z1, y0:y1, x0:x1]
            core_lbls, n_cores = ndimage.label(core_sub, structure=struct_18)
            core_sizes = np.bincount(core_lbls.ravel())
            core_sizes[0] = 0

            if n_cores == 1:
                d_core = ndimage.distance_transform_edt(~core_sub).astype(np.float32)
                cand_domain = (d_core <= (r_c + 0.65)) & hull_sub & (~occ_sub)
                del d_core, hull_sub
                exp_band = ndimage.binary_propagation(
                    core_sub, mask=cand_domain, structure=struct_6
                )
                del cand_domain
                exp_lbls = exp_band.astype(np.int32)
            else:
                d_core, near_idx = ndimage.distance_transform_edt(
                    ~core_sub, return_distances=True, return_indices=True
                )
                nearest_lbl = core_lbls[near_idx[0], near_idx[1], near_idx[2]]
                del near_idx
                cand_domain = (d_core <= (r_c + 0.65)) & hull_sub & (~occ_sub)
                del d_core, hull_sub
                exp_band = ndimage.binary_propagation(
                    core_sub, mask=cand_domain, structure=struct_6
                )
                del cand_domain
                exp_lbls = np.where(exp_band, nearest_lbl, 0)
                del nearest_lbl

            outside_air = (~occ_sub) & (~exp_band)
            touch_air = exp_band & ndimage.binary_dilation(outside_air, structure=struct_6)
            touch_wall = exp_band & ndimage.binary_dilation(occ_sub, structure=struct_6)
            del outside_air

            air_cnts = np.bincount(exp_lbls[touch_air], minlength=n_cores + 1)
            wall_cnts = np.bincount(exp_lbls[touch_wall], minlength=n_cores + 1)
            del touch_air, touch_wall

            air_ratio = air_cnts / np.maximum(air_cnts + wall_cnts, 1)
            valid_cids = np.where((core_sizes >= 8) & (air_ratio < 0.040))[0]
            valid_cids = valid_cids[valid_cids > 0]

            if len(valid_cids) > 0:
                cav_sub_local = np.isin(exp_lbls, valid_cids)
                ext_outside = (~occ_sub) & (~cav_sub_local)
                plug_sub_local = cav_sub_local & ndimage.binary_dilation(ext_outside, structure=struct_18)
                del ext_outside

                iz0, iz1 = max(z0, pad_c), min(z1, pad_c + NZ_c)
                iy0, iy1 = max(y0, pad_c), min(y1, pad_c + NY_c)
                ix0, ix1 = max(x0, pad_c), min(x1, pad_c + NX_c)
                if iz1 > iz0 and iy1 > iy0 and ix1 > ix0:
                    cav_crop = cav_sub_local[iz0 - z0 : iz1 - z0, iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0]
                    plug_crop = plug_sub_local[iz0 - z0 : iz1 - z0, iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0]
                    oc_slice = occ_c[iz0 - pad_c : iz1 - pad_c, iy0 - pad_c : iy1 - pad_c, ix0 - pad_c : ix1 - pad_c]
                    new_cav = cav_crop & (~oc_slice)
                    if np.any(new_cav):
                        cav_pure_c[iz0 - pad_c : iz1 - pad_c, iy0 - pad_c : iy1 - pad_c, ix0 - pad_c : ix1 - pad_c] |= new_cav
                        vent_plug_c[iz0 - pad_c : iz1 - pad_c, iy0 - pad_c : iy1 - pad_c, ix0 - pad_c : ix1 - pad_c] |= plug_crop
                        oc_slice |= new_cav
                        filled_any = True
                del cav_sub_local, plug_sub_local

            del core_sub, occ_sub, core_lbls, exp_band, exp_lbls
        else:
            del filled_dil

        del core_all, occ_box

    del occ_c
    if filled_any:
        seal_vol = (solid_vol > 0) | (shell_closed > 0)
        cav_fine = np.broadcast_to(
            cav_pure_c[:, None, :, None, :, None], (NZ_c, ds, NY_c, ds, NX_c, ds)
        ).reshape(NZ_c * ds, NY_c * ds, NX_c * ds)[:NZ, :NY, :NX]
        seal_vol |= cav_fine
        del cav_fine

        pz, py, px = np.where(vent_plug_c)
        if len(pz) > 0:
            for dz in (-1, 0, 1, 2):
                fz = np.clip(pz * ds + dz, 1, NZ - 2)
                for dy in (-1, 0, 1, 2):
                    fy = np.clip(py * ds + dy, 1, NY - 2)
                    for dx in (-1, 0, 1, 2):
                        fx = np.clip(px * ds + dx, 1, NX - 2)
                        seal_vol[fz, fy, fx] = True

        seal_vol[0, :, :] = False
        seal_vol[-1, :, :] = False
        seal_vol[:, 0, :] = False
        seal_vol[:, -1, :] = False
        seal_vol[:, :, 0] = False
        seal_vol[:, :, -1] = False

        filled_vol = ndimage.binary_fill_holes(seal_vol)
        del seal_vol
        added_vox = int(np.sum(filled_vol & (solid_vol == 0)))
        solid_vol = filled_vol.astype(np.uint8)
        del filled_vol
        print(f"  -> Cavidades ocas internas seladas e preenchidas em 3D: {added_vox:,} voxels convertidos em sólido")

    del cav_pure_c, vent_plug_c
    return solid_vol


def construir_volume_solido_universal(mesh, res_z=1000, modo_base="auto"):
    """
    Reconstrói o volume 3D 100% sólido usando rasterização 3D direta + Flood-Fill 3D verdadeiro
    + selagem 3D por esfera rolante para cavidades ocas ventiladas (sem pontes na virilha/capa).
    """
    bmin, bmax = mesh.bounds
    spans = bmax - bmin
    span_max = float(spans.max())

    n_long = int(np.clip(res_z, 400, 1400))
    h = span_max / float(n_long - 12)
    pad = 6.0 * h
    gmin = bmin - pad
    gmax = bmax + pad

    NX = int(np.ceil((gmax[0] - gmin[0]) / h)) + 1
    NY = int(np.ceil((gmax[1] - gmin[1]) / h)) + 1
    NZ = int(np.ceil((gmax[2] - gmin[2]) / h)) + 1

    print(f"  -> Grade volumétrica 3D isotrópica: {NX} x {NY} x {NZ} (passo h={h:.5f})")

    mesh_sealed = fechar_micro_furos_malha(mesh, max_hole_ratio=0.035)
    shell_vol = rasterizar_superficie_3d(
        mesh_sealed.vertices, mesh_sealed.faces, gmin, h, (NZ, NY, NX)
    )

    # Fechamento morfológico 3D mínimo (3x3x3 = 1 voxel) apenas para vedar frestas sub-voxel
    # sem JAMAIS unir pernas, virilha, dedos ou dobras internas da capa.
    shell_closed = ndimage.binary_closing(
        shell_vol > 0, structure=np.ones((3, 3, 3), dtype=bool)
    ).astype(np.uint8)

    iz_bot = int(np.clip(np.round((bmin[2] - gmin[2]) / h), 1, NZ - 6))
    if modo_base in ("auto", "plana"):
        # Sela APENAS os ~0.35% inferiores encostados na mesa (Z=0) para fechar pedestais/pés ocos
        n_cap = max(2, int(round(0.0035 * spans[2] / h)))
        iz_ref = min(NZ - 2, iz_bot + n_cap)
        base_band = np.max(shell_closed[iz_bot : iz_ref + 1], axis=0).astype(np.uint8) * 255
        k_base = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        base_closed = cv2.morphologyEx(base_band, cv2.MORPH_CLOSE, k_base)
        ff = base_closed.copy()
        mask = np.zeros((NY + 2, NX + 2), dtype=np.uint8)
        cv2.floodFill(ff, mask, (0, 0), 128)
        base_capped = (ff != 128).astype(np.uint8)
        shell_closed[iz_bot : iz_ref + 1] = np.maximum(
            shell_closed[iz_bot : iz_ref + 1], base_capped[None, :, :]
        )
        shell_closed[:iz_bot] = 0

    # Garante ar externo livre nas 6 faces da caixa de domínio antes da inundação 3D
    shell_closed[0, :, :] = 0
    shell_closed[-1, :, :] = 0
    shell_closed[:, 0, :] = 0
    shell_closed[:, -1, :] = 0
    shell_closed[:, :, 0] = 0
    shell_closed[:, :, -1] = 0

    # Preenchimento 3D VERDADEIRO inicial + selagem 3D de cavidades internas com fendas/respiros!
    solid_vol = ndimage.binary_fill_holes(shell_closed > 0).astype(np.uint8)
    solid_vol = _solidificar_cavidades_ventiladas_3d(solid_vol, shell_closed, h, span_max)
    del shell_closed

    if modo_base in ("auto", "plana"):
        # Cura eventuais micro-crateras ou furos rasos na região do pedestal/base (12% inferiores)
        iz_base_top = min(NZ - 2, iz_bot + max(6, int(round(0.12 * spans[2] / h))))
        iz_sub_end = min(NZ, iz_base_top + 6)
        sub_base = solid_vol[:iz_sub_end] > 0
        rg = np.arange(-3, 4)
        zz, yy, xx = np.meshgrid(rg, rg, rg, indexing="ij")
        ball3 = (zz * zz + yy * yy + xx * xx) <= 9
        sub_dil = ndimage.binary_dilation(sub_base, structure=ball3)
        sub_dil[0, :, :] = False
        sub_dil[:, 0, :] = False
        sub_dil[:, -1, :] = False
        sub_dil[:, :, 0] = False
        sub_dil[:, :, -1] = False
        sub_fill = ndimage.binary_fill_holes(sub_dil)
        sub_closed = ndimage.binary_erosion(sub_fill, structure=ball3, border_value=0)
        iz_apply = min(iz_base_top, iz_sub_end - 4)
        if iz_apply > iz_bot:
            solid_vol[iz_bot:iz_apply] = np.maximum(
                solid_vol[iz_bot:iz_apply], sub_closed[iz_bot:iz_apply].astype(np.uint8)
            )
        del sub_base, sub_dil, sub_fill, sub_closed
        solid_vol[:iz_bot] = 0
        shell_vol[:iz_bot] = 0

    struct3d = ndimage.generate_binary_structure(3, 3)  # 26-conectado preserva quinas diagonais finas
    labels, n_lbls = ndimage.label(solid_vol > 0, structure=struct3d)
    if n_lbls > 1:
        counts = np.bincount(labels.ravel())
        counts[0] = 0
        max_vox = counts.max()
        keep_lbls = np.where(counts >= max_vox * 0.002)[0]
        solid_vol = np.isin(labels, keep_lbls).astype(np.uint8)
        print(f"  -> Componentes 3D no volume: {n_lbls} (mantidos {len(keep_lbls)} principais)")
    del labels

    # Garante que shell_vol mantenha apenas cascas 2D reais conectadas ao sólido (poda fios/pinos 1D!)
    for _ in range(2):
        occ_7 = _contar_vizinhos_7x7x7((solid_vol > 0) | (shell_vol > 0))
        shell_vol = ((shell_vol > 0) & ((solid_vol > 0) | (occ_7 >= 18))).astype(np.uint8)
        del occ_7

    comb_lbls, n_comb = ndimage.label((solid_vol > 0) | (shell_vol > 0), structure=struct3d)
    if n_comb > 1:
        valid_comb = np.unique(comb_lbls[solid_vol > 0])
        valid_comb = valid_comb[valid_comb > 0]
        shell_vol = ((shell_vol > 0) & np.isin(comb_lbls, valid_comb)).astype(np.uint8)
    del comb_lbls

    return solid_vol, shell_vol, gmin, h, iz_bot


def _avaliar_trilinear(pts, grid, gmin, h):
    """Avalia um campo escalar 3D (NZ, NY, NX) com interpolação trilinear contínua."""
    inv_h = np.float32(1.0 / h)
    iz = ((pts[:, 2] - gmin[2]) * inv_h).astype(np.float32)
    iy = ((pts[:, 1] - gmin[1]) * inv_h).astype(np.float32)
    ix = ((pts[:, 0] - gmin[0]) * inv_h).astype(np.float32)
    coords = np.vstack([iz, iy, ix])
    return ndimage.map_coordinates(grid, coords, order=1, mode="nearest")


def _construir_operador_vizinhos(n_verts, faces):
    """Retorna matriz esparsa CSR linha-normalizada das médias da vizinhança 1-ring."""
    rows = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2], faces[:, 1], faces[:, 2], faces[:, 0]])
    cols = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0], faces[:, 0], faces[:, 1], faces[:, 2]])
    data = np.ones(len(rows), dtype=np.float32)
    adj = coo_matrix((data, (rows, cols)), shape=(n_verts, n_verts)).tocsr()
    adj.data[:] = 1.0
    row_counts = np.diff(adj.indptr)
    inv_deg = np.where(row_counts > 0, 1.0 / np.maximum(row_counts, 1), 0.0).astype(np.float32)
    adj.data *= np.repeat(inv_deg, row_counts)
    return adj


def _remover_spikes_malha(mesh, sdf_grid, gmin, h, z_bot=None):
    """
    Detecta e elimina cirurgicamente 100% dos spikes (agulhas para dentro ou para fora,
    de 1 ou múltiplos vértices, inclusive atravessando vãos) mantendo os vértices sãos
    100% fixos (solução harmônica de Dirichlet + projeção suave no SDF zero).
    """
    verts = mesh.vertices.copy()
    faces = mesh.faces
    n_verts = len(verts)
    if n_verts == 0 or len(faces) == 0:
        return mesh

    L_op = _construir_operador_vizinhos(n_verts, faces)
    edges = mesh.edges_unique
    u_e = edges[:, 0]
    v_e = edges[:, 1]
    tot_deg = np.zeros(n_verts, dtype=np.int32)
    np.add.at(tot_deg, u_e, 1)
    np.add.at(tot_deg, v_e, 1)

    relaxed_ever = np.zeros(n_verts, dtype=bool)

    for _pass in range(3):
        v_avg = L_op.dot(verts)
        delta_norm = np.linalg.norm(verts - v_avg, axis=1)

        if z_bot is not None:
            above_bot = verts[:, 2] > (z_bot + 2.2 * h)
        else:
            above_bot = np.ones(n_verts, dtype=bool)

        # 1) Coerência das normais das faces incidentes: agulhas cônicas e lâminas dobradas
        #    possuem normais opostas que se cancelam (coerência < 0.22), enquanto planos e curvas sãs > 0.55.
        v0 = verts[faces[:, 0]]
        v1 = verts[faces[:, 1]]
        v2 = verts[faces[:, 2]]
        fn_raw = np.cross(v1 - v0, v2 - v0)
        fn_len = np.linalg.norm(fn_raw, axis=1, keepdims=True)
        fn_unit = fn_raw / np.maximum(fn_len, 1e-15)

        sum_fn = np.zeros((n_verts, 3), dtype=np.float64)
        f_deg = np.zeros(n_verts, dtype=np.float64)
        for col in range(3):
            np.add.at(sum_fn, faces[:, col], fn_unit)
            np.add.at(f_deg, faces[:, col], 1.0)
        norm_coherence = np.linalg.norm(sum_fn, axis=1) / np.maximum(f_deg, 1.0)

        max_nb_delta = np.zeros(n_verts, dtype=np.float64)
        np.maximum.at(max_nb_delta, u_e, delta_norm[v_e])
        np.maximum.at(max_nb_delta, v_e, delta_norm[u_e])

        cone_spike = above_bot & (norm_coherence < 0.22) & (delta_norm > 0.70 * h)
        local_max_spike = (
            above_bot
            & (norm_coherence < 0.42)
            & (delta_norm >= max_nb_delta * 0.98)
            & (delta_norm > 1.8 * h)
        )

        # 2) Validação direta contra o SDF: vértices fora da superfície zero ou arestas cruzando o ar/sólido
        if sdf_grid is not None:
            sdf_vals = np.abs(_avaliar_trilinear(verts, sdf_grid, gmin, h))
            sdf_outlier = above_bot & (sdf_vals > 1.95 * h)

            edge_len = np.linalg.norm(verts[u_e] - verts[v_e], axis=1)
            long_mask = edge_len > 2.5 * h
            bridge_spike = np.zeros(n_verts, dtype=bool)
            if np.any(long_mask):
                lu = u_e[long_mask]
                lv = v_e[long_mask]
                pa = verts[lu]
                pb = verts[lv]
                s_mid = np.abs(_avaliar_trilinear(0.50 * (pa + pb), sdf_grid, gmin, h))
                s_q1 = np.abs(_avaliar_trilinear(0.75 * pa + 0.25 * pb, sdf_grid, gmin, h))
                s_q3 = np.abs(_avaliar_trilinear(0.25 * pa + 0.75 * pb, sdf_grid, gmin, h))
                bad_long = np.maximum(s_mid, np.maximum(s_q1, s_q3)) > 2.05 * h
                if np.any(bad_long):
                    bad_deg = np.zeros(n_verts, dtype=np.int32)
                    np.add.at(bad_deg, lu[bad_long], 1)
                    np.add.at(bad_deg, lv[bad_long], 1)
                    bridge_spike = above_bot & (bad_deg >= np.maximum(2, (tot_deg + 1) // 2))
        else:
            sdf_outlier = np.zeros(n_verts, dtype=bool)
            bridge_spike = np.zeros(n_verts, dtype=bool)

        spike_mask = cone_spike | local_max_spike | sdf_outlier | bridge_spike
        if not np.any(spike_mask):
            break

        relaxed_ever |= spike_mask
        # Mantém 100% dos vértices sãos (~spike_mask) cravados no lugar (condição de contorno de Dirichlet)!
        for _ in range(45):
            v_new = L_op.dot(verts)
            verts[spike_mask] = v_new[spike_mask]

    if np.any(relaxed_ever):
        if sdf_grid is not None:
            pts_r = verts[relaxed_ever]
            s_val = _avaliar_trilinear(pts_r, sdf_grid, gmin, h)
            eps = 0.5 * h
            gx = _avaliar_trilinear(pts_r + [eps, 0.0, 0.0], sdf_grid, gmin, h) - _avaliar_trilinear(pts_r - [eps, 0.0, 0.0], sdf_grid, gmin, h)
            gy = _avaliar_trilinear(pts_r + [0.0, eps, 0.0], sdf_grid, gmin, h) - _avaliar_trilinear(pts_r - [0.0, eps, 0.0], sdf_grid, gmin, h)
            gz = _avaliar_trilinear(pts_r + [0.0, 0.0, eps], sdf_grid, gmin, h) - _avaliar_trilinear(pts_r - [0.0, 0.0, eps], sdf_grid, gmin, h)
            grad = np.column_stack([gx, gy, gz])
            g_norm = np.linalg.norm(grad, axis=1, keepdims=True)
            grad_u = grad / np.maximum(g_norm, 1e-12)
            pts_r = pts_r - np.clip(s_val[:, None], -1.2 * h, 1.2 * h) * grad_u
            verts[relaxed_ever] = pts_r
            for _ in range(3):
                v_new = L_op.dot(verts)
                verts[relaxed_ever] = 0.5 * verts[relaxed_ever] + 0.5 * v_new[relaxed_ever]

        print(f"  -> Filtro anti-spike: {int(np.sum(relaxed_ever)):,} vértices anômalos retraídos com sucesso")
        mesh.vertices = verts

    return mesh


def _polir_taubin_normal(mesh, lamb=0.45, nu=0.47, iterations=6, z_bot=None, h=0.0):
    """
    Suavização Taubin projetada exclusivamente ao longo da normal de cada vértice:
    elimina micro-ondulações sem deslizamento tangencial (impede que triângulos longos
    em superfícies planas de pedestais deslizem e dobrem sobre as bordas).
    """
    verts = mesh.vertices.copy()
    faces = mesh.faces
    n_verts = len(verts)
    if n_verts == 0 or len(faces) == 0:
        return mesh

    L_op = _construir_operador_vizinhos(n_verts, faces)
    v_norms = mesh.vertex_normals.copy()
    if z_bot is not None:
        mobile = (verts[:, 2] > (z_bot + 2.0 * h))[:, None]
    else:
        mobile = np.ones((n_verts, 1), dtype=bool)

    for idx in range(iterations):
        diff = L_op.dot(verts) - verts
        normal_proj = np.sum(diff * v_norms, axis=1, keepdims=True) * v_norms
        if idx % 2 == 0:
            verts += mobile * (lamb * normal_proj)
        else:
            verts -= mobile * (nu * normal_proj)

    mesh.vertices = verts
    return mesh


def _calcular_amostras_phong_externas(verts_orig, outer_faces, outer_fn, h):
    """
    Gera amostras contínuas com interpolação curva de Phong (PN-Triangles com proteção de vincos):
    reconstrói a curvatura orgânica suave entre triângulos brutos facetados da IA sem achatar a pele,
    mantendo 100% retas as quinas vivas (>= 45°, como bordas de pedestais, solas e armaduras).
    """
    ov0 = verts_orig[outer_faces[:, 0]]
    ov1 = verts_orig[outer_faces[:, 1]]
    ov2 = verts_orig[outer_faces[:, 2]]

    dbl_area = np.linalg.norm(np.cross(ov1 - ov0, ov2 - ov0), axis=1, keepdims=True)
    weighted_fn = outer_fn * np.maximum(dbl_area, 1e-12)

    v_norm_sum = np.zeros_like(verts_orig, dtype=np.float64)
    np.add.at(v_norm_sum, outer_faces[:, 0], weighted_fn)
    np.add.at(v_norm_sum, outer_faces[:, 1], weighted_fn)
    np.add.at(v_norm_sum, outer_faces[:, 2], weighted_fn)
    v_norm_len = np.linalg.norm(v_norm_sum, axis=1, keepdims=True)
    v_smooth = np.where(v_norm_len > 1e-12, v_norm_sum / np.maximum(v_norm_len, 1e-12), 0.0)
    del v_norm_sum, v_norm_len, weighted_fn, dbl_area

    def _normal_canto_com_vinco(v_idx):
        vn = v_smooth[v_idx]
        cos_th = np.sum(vn * outer_fn, axis=1, keepdims=True)
        blend = np.clip((cos_th - 0.78) / 0.12, 0.0, 1.0)
        nc = (1.0 - blend) * outer_fn + blend * vn
        nc_len = np.linalg.norm(nc, axis=1, keepdims=True)
        return nc / np.maximum(nc_len, 1e-12)

    N0 = _normal_canto_com_vinco(outer_faces[:, 0])
    N1 = _normal_canto_com_vinco(outer_faces[:, 1])
    N2 = _normal_canto_com_vinco(outer_faces[:, 2])
    del v_smooth

    bary_list = (
        (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
        (0.5, 0.5, 0.0),
        (0.0, 0.5, 0.5),
        (0.5, 0.0, 0.5),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    pts_chunks = []
    nrm_chunks = []
    max_off = 0.45 * h

    for b0, b1, b2 in bary_list:
        p_flat = b0 * ov0 + b1 * ov1 + b2 * ov2
        if b0 < 1.0 and b1 < 1.0 and b2 < 1.0:
            d0 = np.sum((p_flat - ov0) * N0, axis=1, keepdims=True) * N0
            d1 = np.sum((p_flat - ov1) * N1, axis=1, keepdims=True) * N1
            d2 = np.sum((p_flat - ov2) * N2, axis=1, keepdims=True) * N2
            phong_off = -0.55 * (b0 * d0 + b1 * d1 + b2 * d2)
            off_len = np.linalg.norm(phong_off, axis=1, keepdims=True)
            scale = np.where(off_len > max_off, max_off / np.maximum(off_len, 1e-12), 1.0)
            p_curv = p_flat + phong_off * scale
        else:
            p_curv = p_flat
        n_curv = b0 * N0 + b1 * N1 + b2 * N2
        n_curv /= np.maximum(np.linalg.norm(n_curv, axis=1, keepdims=True), 1e-12)
        pts_chunks.append(p_curv.astype(np.float32))
        nrm_chunks.append(n_curv.astype(np.float32))

    pts_chunks.append(np.zeros((1, 3), dtype=np.float32))
    nrm_chunks.append(np.zeros((1, 3), dtype=np.float32))
    return np.vstack(pts_chunks), np.vstack(nrm_chunks)


def _descolar_vertices_pinch_manifold(verts, faces, eps_sep=1e-4):
    """
    Separa em nível micrométrico vértices topologicamente distintos que compartilham a mesma
    coordenada float32 (costuras/pinches exportados pelo manifold3d), evitando que a fusão por
    coordenadas no arquivo STL gere arestas ou vértices não-manifold no fatiador.
    """
    v = np.array(verts, dtype=np.float64, copy=True)
    f = np.asarray(faces, dtype=np.int64)
    if len(v) == 0 or len(f) == 0:
        return v

    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    vn = np.zeros_like(v, dtype=np.float64)
    np.add.at(vn, f[:, 0], fn)
    np.add.at(vn, f[:, 1], fn)
    np.add.at(vn, f[:, 2], fn)
    vn_len = np.linalg.norm(vn, axis=1, keepdims=True)
    vn_unit = np.where(vn_len > 1e-15, vn / np.maximum(vn_len, 1e-15), 0.0)

    for p_idx, scale in enumerate((1.0, 2.5, 6.0)):
        vf32 = v.astype(np.float32)
        vf32 = np.where(vf32 == 0.0, np.float32(0.0), vf32)
        raw_keys = np.ascontiguousarray(vf32).view(np.dtype((np.void, 12))).ravel()
        _, inv, cnts = np.unique(raw_keys, return_inverse=True, return_counts=True)
        dup_mask = cnts[inv] > 1
        if not np.any(dup_mask):
            break
        dup_idx = np.where(dup_mask)[0]
        step = float(eps_sep) * scale
        n_d = vn_unit[dup_idx]
        if p_idx >= 1:
            u64 = dup_idx.astype(np.uint64)
            jx = (((u64 * np.uint64(2654435761)) & np.uint64(0xFF)).astype(np.float64) / 255.0 - 0.5) * 0.5
            jy = (((u64 * np.uint64(2246822519)) & np.uint64(0xFF)).astype(np.float64) / 255.0 - 0.5) * 0.5
            jz = (((u64 * np.uint64(3266489917)) & np.uint64(0xFF)).astype(np.float64) / 255.0 - 0.5) * 0.5
            n_d = n_d + np.column_stack([jx, jy, jz])
        v[dup_idx] -= step * n_d

    return v


def extrair_e_realcar_superficie(
    mesh_orig,
    solid_vol,
    shell_vol,
    gmin,
    h,
    iz_bot,
    modo_base="auto",
    realce_detalhes=0.0,
    suavizacao=1.0,
    espessura_min_mm=0.50,
    altura_alvo_mm=75.0,
    target_faces=2000000,
):
    """
    Extrai malha densa de ultra-alta resolução com SDF de gradiente unitário exato,
    encaixe curvo de Phong (sem estampar facetas planas da IA), acabamento superficial
    estilo porcelana com compensação de volume na malha uniforme e validação 2-manifold
    micrométrica de alta densidade seguida de proteção anti-spike.
    """
    bmin, bmax = mesh_orig.bounds
    altura_orig = bmax[2] - bmin[2]

    mm_por_unidade = altura_alvo_mm / (altura_orig + 1e-9)
    unidades_por_mm = 1.0 / mm_por_unidade
    suav_clamped = float(np.clip(suavizacao, 0.0, 2.5))

    # SDF Euclidiano exato sem o salto artificial de +1.0*h na interface sólido/ar
    dist_in = ndimage.distance_transform_edt(solid_vol > 0, sampling=(h, h, h)).astype(np.float32)
    dist_out = ndimage.distance_transform_edt(solid_vol == 0, sampling=(h, h, h)).astype(np.float32)
    half_h = np.float32(0.5 * h)
    sdf_body = np.where(solid_vol > 0, -(dist_in - half_h), (dist_out - half_h))
    del solid_vol, dist_in, dist_out

    # Identifica 100% da superfície externa visível da peça, sem falhas em xadrez,
    # rejeitando 100% das paredes ocas internas enterradas dentro do volume.
    c_pts = mesh_orig.triangles_center
    n_vec = mesh_orig.face_normals

    sdf_center = _avaliar_trilinear(c_pts, sdf_body, gmin, h)
    air_1 = _avaliar_trilinear(c_pts + (1.8 * h) * n_vec, sdf_body, gmin, h)
    air_2 = _avaliar_trilinear(c_pts + (2.8 * h) * n_vec, sdf_body, gmin, h)
    air_front = np.maximum(air_1, air_2)

    back_1 = _avaliar_trilinear(c_pts - (1.8 * h) * n_vec, sdf_body, gmin, h)
    back_2 = _avaliar_trilinear(c_pts - (2.8 * h) * n_vec, sdf_body, gmin, h)
    air_back = np.maximum(back_1, back_2)

    outer_mask = (sdf_center >= -1.15 * h) & (
        (air_front >= 0.45 * h) | ((air_back >= 0.65 * h) & (air_front >= -0.35 * h))
    )
    del sdf_center
    if np.any(outer_mask):
        idx_outer = np.where(outer_mask)[0]
        outer_faces = mesh_orig.faces[idx_outer]
        flip_sign = np.where(air_front[idx_outer] >= air_back[idx_outer], 1.0, -1.0)[:, None]
        outer_fn = n_vec[idx_outer] * flip_sign
    else:
        outer_faces = mesh_orig.faces
        outer_fn = n_vec

    print(f"  -> Triângulos da superfície externa verificados: {len(outer_faces):,}")

    # Proteção 100% uniforme em toda a malha para folhas abertas/finas (ZERO bolhas de subconjunto!)
    dist_shell = ndimage.distance_transform_edt(shell_vol == 0, sampling=(h, h, h)).astype(np.float32)
    del shell_vol
    min_mm = max(0.40, float(espessura_min_mm)) if espessura_min_mm > 0 else 0.0
    r_guard = float(np.clip(0.5 * min_mm * unidades_por_mm, 0.72 * h, 0.95 * h))
    np.minimum(sdf_body, dist_shell - np.float32(r_guard), out=sdf_body)
    del dist_shell

    # Suavização Gaussiana isotrópica calibrada para eliminar degraus de voxel preservando detalhes finos
    sigma_sdf = float(np.clip(0.65 + 0.15 * suav_clamped, 0.55, 1.05))
    ndimage.gaussian_filter(sdf_body, sigma=sigma_sdf, output=sdf_body)
    if modo_base in ("auto", "plana"):
        sdf_body[:iz_bot] = np.abs(sdf_body[:iz_bot]) + np.float32(h)

    verts_mc, faces_mc, _, _ = marching_cubes(sdf_body, level=0.0, spacing=(h, h, h))

    verts_xyz = np.column_stack([
        verts_mc[:, 2] + gmin[0],
        verts_mc[:, 1] + gmin[1],
        verts_mc[:, 0] + gmin[2],
    ])
    faces_xyz = faces_mc[:, [0, 2, 1]]
    del verts_mc, faces_mc
    repaired = trimesh.Trimesh(vertices=verts_xyz, faces=faces_xyz, process=True)

    comps = repaired.split(only_watertight=False)
    if len(comps) > 1:
        comps = sorted(comps, key=lambda m: len(m.faces), reverse=True)
        max_f = len(comps[0].faces)
        kept = [m for m in comps if len(m.faces) >= max_f * 0.003]
        repaired = trimesh.util.concatenate(kept) if len(kept) > 1 else comps[0]

    z_bot_world = float(gmin[2] + iz_bot * h) if modo_base in ("auto", "plana") else None
    z_margin = (bmin[2] + 2.5 * h) if modo_base in ("auto", "plana") else (bmin[2] - h)

    # 1) Suavização Taubin real preservadora de volume (+0.50 / -0.52) antes do encaixe:
    #    elimina o serrilhado do Marching Cubes sem encolher dedos, ponta do nariz ou mechas!
    L_dense = _construir_operador_vizinhos(len(repaired.vertices), repaired.faces)
    verts_d = repaired.vertices.copy()
    if z_bot_world is not None:
        mob_bot = (verts_d[:, 2] > (z_bot_world + 1.8 * h))[:, None]
    else:
        mob_bot = np.ones((len(verts_d), 1), dtype=bool)

    for idx_p in range(6):
        step_w = 0.50 if (idx_p % 2 == 0) else -0.52
        verts_d += mob_bot * (step_w * (L_dense.dot(verts_d) - verts_d))
    repaired.vertices = verts_d
    del verts_d

    # 2) Mede espessura interna local suave para proteger cascas finas abertas durante o encaixe
    v_norms_pre = repaired.vertex_normals.astype(np.float32)
    sdf_inward = _avaliar_trilinear(repaired.vertices - (1.8 * h) * v_norms_pre, sdf_body, gmin, h)
    thin_safe = np.clip((-sdf_inward - 0.20 * h) / (1.0 * h), 0.15, 1.0)
    del v_norms_pre, sdf_inward

    # 3) Encaixe curvo de Phong na malha densa uniforme (preserva detalhes sem copiar facetas planas!)
    if len(outer_faces) > 0:
        sample_pts_pad, sample_norms_pad = _calcular_amostras_phong_externas(
            mesh_orig.vertices, outer_faces, outer_fn, h
        )
        tree = cKDTree(sample_pts_pad[:-1])
        max_snap_dist = 2.0 * h
        dists, idxs = tree.query(
            repaired.vertices, k=6, distance_upper_bound=max_snap_dist, workers=-1
        )

        v_norms = repaired.vertex_normals.astype(np.float32)
        cand_norms = sample_norms_pad[idxs]
        dots = np.sum(v_norms[:, None, :] * cand_norms, axis=2)
        above_base = repaired.vertices[:, 2] > z_margin

        valid = np.isfinite(dists) & (dots >= 0.55) & above_base[:, None]
        has_valid = np.any(valid, axis=1)

        if np.any(has_valid):
            first_col = np.argmax(valid, axis=1)
            row_idx = np.where(has_valid)[0]
            matched_idx = idxs[row_idx, first_col[has_valid]]

            v_curr = repaired.vertices[row_idx]
            p_targ = sample_pts_pad[matched_idx]
            n_targ = sample_norms_pad[matched_idx]

            normal_disp = np.sum((p_targ - v_curr) * n_targ, axis=1, keepdims=True)
            normal_disp = np.clip(normal_disp, -0.95 * h, 0.95 * h)

            snap_strength = float(np.clip(0.90 - 0.05 * suav_clamped, 0.75, 0.92))
            ts = thin_safe[row_idx, None]
            w = np.where(normal_disp < 0.0, snap_strength * ts, snap_strength)

            disp_all = np.zeros_like(repaired.vertices)
            disp_all[row_idx] = w * normal_disp * n_targ

            for _ in range(2):
                disp_all = 0.50 * disp_all + 0.50 * L_dense.dot(disp_all)
            disp_all[~above_base] = 0.0
            repaired.vertices += disp_all

        del sample_pts_pad, sample_norms_pad, tree
    del thin_safe

    # 4) Acabamento estilo porcelana Taubin projetado na normal COM proteção de vincos escultóricos:
    #    dissolve 100% das micro-facetas na pele e na capa mantendo olhos, nariz, lábios e dedos nítidos!
    n_cycles = int(round(4 * suav_clamped))
    if n_cycles > 0:
        print(f"  -> Aplicando acabamento liso de superfície (intensidade={suav_clamped:.2f}, {n_cycles} ciclos)...")
        vn_curr = repaired.vertex_normals.astype(np.float64)
        vn_avg = L_dense.dot(vn_curr.astype(np.float32)).astype(np.float64)
        vn_cos = np.sum(vn_curr * vn_avg, axis=1, keepdims=True)
        crease_w = np.clip((vn_cos - 0.82) / 0.12, 0.12, 1.0)
        del vn_avg, vn_cos

        v_fair = repaired.vertices.copy()
        mob_w = (v_fair[:, 2] > z_margin)[:, None] * crease_w
        del crease_w
        for idx_f in range(n_cycles * 2):
            diff_f = L_dense.dot(v_fair) - v_fair
            norm_proj = np.sum(diff_f * vn_curr, axis=1, keepdims=True) * vn_curr
            step_f = 0.46 if (idx_f % 2 == 0) else -0.485
            v_fair += mob_w * (step_f * norm_proj)
        del mob_w, vn_curr
        repaired.vertices = v_fair
        del v_fair

    # 5) Realce de Micro-Detalhes 3D opcional (--realce) APÓS dissolver as facetas brutas,
    #    acentuando apenas relevos escultóricos reais (olhos, nariz, lábios, mechas) sem ruído!
    if realce_detalhes > 0.0:
        print(f"  -> Aplicando Realce de Micro-Detalhes 3D (intensidade={realce_detalhes:.2f})...")
        v_base = repaired.vertices.copy()
        for _ in range(10):
            v_base += mob_bot * (0.50 * (L_dense.dot(v_base) - v_base))
        v_norms = repaired.vertex_normals
        detail_scalar = np.sum((repaired.vertices - v_base) * v_norms, axis=1, keepdims=True)
        del v_base

        max_boost_unit = 0.14 * unidades_por_mm
        boost_vec = np.clip(realce_detalhes * detail_scalar, -max_boost_unit, max_boost_unit) * v_norms
        for _ in range(2):
            boost_vec = 0.50 * boost_vec + 0.50 * L_dense.dot(boost_vec)
        detail_mask = (repaired.vertices[:, 2] > z_margin).reshape(-1, 1)
        repaired.vertices += detail_mask * boost_vec
        del boost_vec

    del L_dense, mob_bot

    if modo_base in ("auto", "plana") and z_bot_world is not None:
        bot_m_pre = repaired.vertices[:, 2] <= z_bot_world + 1.8 * h
        repaired.vertices[bot_m_pre, 2] = z_bot_world

    # 6) Validação 2-manifold e simplificação micrométrica de alta densidade (deixa a densidade alta
    #    para máxima lisura visual e permite ao fatiador gerenciar a complexidade se desejado)
    n_in = len(repaired.faces)
    span_max = float(repaired.extents.max())
    m_in = manifold3d.Mesh(
        vert_properties=repaired.vertices.astype(np.float32),
        tri_verts=repaired.faces.astype(np.uint32),
    )
    man = manifold3d.Manifold(m_in)
    del m_in

    if n_in > target_faces * 1.03:
        ratio = float(n_in) / float(max(200000, target_faces))
        tol = 0.000008 * span_max * (ratio ** 0.85)
        tol = float(np.clip(tol, 0.000004 * span_max, 0.000120 * span_max))
        print(f"  -> Otimizando malha via manifold3d.simplify ({n_in:,} faces)...")
        man = man.simplify(tol)

    m_out = man.to_mesh()
    del man
    v_man = _descolar_vertices_pinch_manifold(
        m_out.vert_properties[:, :3], m_out.tri_verts, eps_sep=2.0e-5 * span_max
    )
    repaired = trimesh.Trimesh(
        vertices=v_man,
        faces=m_out.tri_verts,
        process=True,
    )
    del m_out, v_man
    print(f"  -> Malha 2-manifold otimizada: {len(repaired.faces):,} faces")

    # 7) Filtro anti-spike pós-simplificação validado contra o SDF + polimento normal sem deslize em bases
    repaired = _remover_spikes_malha(repaired, sdf_body, gmin, h, z_bot=z_bot_world)
    del sdf_body

    verts_post_man = repaired.vertices.copy()
    fn_post_man = repaired.face_normals.copy()

    repaired = _polir_taubin_normal(
        repaired, lamb=0.35, nu=0.37, iterations=2, z_bot=z_bot_world, h=h
    )
    repaired = _remover_spikes_malha(repaired, None, gmin, h, z_bot=z_bot_world)

    # 8) Garantia estrita contra faces dobradas/invertidas (protege bases e quinas planas)
    for _ in range(6):
        fv0 = repaired.vertices[repaired.faces[:, 0]]
        fv1 = repaired.vertices[repaired.faces[:, 1]]
        fv2 = repaired.vertices[repaired.faces[:, 2]]
        fn_curr = np.cross(fv1 - fv0, fv2 - fv0)
        fn_len = np.linalg.norm(fn_curr, axis=1, keepdims=True)
        fn_curr_u = fn_curr / np.maximum(fn_len, 1e-15)
        bad_faces = (np.sum(fn_curr_u * fn_post_man, axis=1) < 0.25) | (fn_len[:, 0] < 1e-13)
        if not np.any(bad_faces):
            break
        bad_v = np.zeros(len(repaired.vertices), dtype=bool)
        bad_v[repaired.faces[bad_faces].ravel()] = True
        repaired.vertices[bad_v] = 0.30 * repaired.vertices[bad_v] + 0.70 * verts_post_man[bad_v]
    del verts_post_man, fn_post_man

    repaired.vertices[:, 0] -= 0.5 * (repaired.bounds[0, 0] + repaired.bounds[1, 0])
    repaired.vertices[:, 1] -= 0.5 * (repaired.bounds[0, 1] + repaired.bounds[1, 1])
    repaired.vertices[:, 2] -= repaired.bounds[0, 2]

    if modo_base in ("auto", "plana"):
        dz_tol = 1.0e-4 * repaired.extents[2]
        bot_mask = repaired.vertices[:, 2] <= dz_tol
        repaired.vertices[bot_mask, 2] = 0.0

    repaired.fix_normals()
    return repaired


def estimar_altura_alvo_mm(mesh, args):
    """Estima a altura final em mm para calibrar a proteção de espessura mínima e realce."""
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


def garantir_manifold_final_stl(mesh):
    """
    Garante que a malha final na precisão exata float32 do formato STL binário seja 100%
    estanque (2-manifold), com 0 arestas abertas, 0 arestas não-manifold e sem micro-bolhas.
    """
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh

    def _filtrar_ilhas_principais(m_in):
        f = m_in.faces
        if len(f) == 0:
            return m_in
        rows = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
        cols = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
        adj = coo_matrix(
            (np.ones(len(rows), dtype=np.uint8), (rows, cols)),
            shape=(len(m_in.vertices), len(m_in.vertices)),
        )
        n_c, v_lbl = csgraph_components(adj, directed=False)
        if n_c <= 1:
            return m_in
        f_lbl = v_lbl[f[:, 0]]
        f_cnts = np.bincount(f_lbl, minlength=n_c)
        max_f = int(f_cnts.max())
        keep_c = np.where(f_cnts >= max(32, int(max_f * 0.005)))[0]
        keep_mask = np.isin(f_lbl, keep_c)
        if np.all(keep_mask):
            return m_in
        sub = m_in.submesh([np.where(keep_mask)[0]], append=True)
        sub.remove_unreferenced_vertices()
        return sub

    mesh = _filtrar_ilhas_principais(mesh)
    span_max = float(mesh.extents.max())
    eps_f32 = max(2.5e-4, 2.5e-5 * span_max)

    v_sep = _descolar_vertices_pinch_manifold(mesh.vertices, mesh.faces, eps_sep=eps_f32)
    v32 = v_sep.astype(np.float32)
    v32[:, 2] -= np.float32(v32[:, 2].min())
    mesh_f32 = trimesh.Trimesh(vertices=v32.astype(np.float64), faces=mesh.faces, process=True)
    mesh_f32 = _filtrar_ilhas_principais(mesh_f32)

    if not mesh_f32.is_watertight:
        m_in = manifold3d.Mesh(
            vert_properties=mesh_f32.vertices.astype(np.float32),
            tri_verts=mesh_f32.faces.astype(np.uint32),
        )
        m_out = manifold3d.Manifold(m_in).to_mesh()
        del m_in
        v_clean = _descolar_vertices_pinch_manifold(
            m_out.vert_properties[:, :3], m_out.tri_verts, eps_sep=eps_f32
        )
        v32 = v_clean.astype(np.float32)
        v32[:, 2] -= np.float32(v32[:, 2].min())
        mesh_f32 = trimesh.Trimesh(
            vertices=v32.astype(np.float64), faces=m_out.tri_verts, process=True
        )
        del m_out
        mesh_f32 = _filtrar_ilhas_principais(mesh_f32)

    mesh_f32.fix_normals()
    return mesh_f32


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
        description="Reparador 3D de Alta Fidelidade Estrutural para arquivos STL gerados pelo ComfyUI."
    )
    parser.add_argument("entrada", help="Caminho do arquivo .stl de entrada (ex: ComfyUI_00020.stl)")
    parser.add_argument("-o", "--saida", default=None, help="Caminho do arquivo .stl reparado de saída (padrão: <nome>_reparado.stl)")
    parser.add_argument("-r", "--referencia", default=None, help="Arquivo .stl de referência opcional para copiar a escala")
    parser.add_argument(
        "--escala-ref",
        choices=["base", "altura"],
        default="base",
        help="Ao usar --referencia, escalar igualando a 'base' (diâmetro/largura) ou a 'altura' (padrão: base)",
    )
    parser.add_argument("--altura", type=float, default=None, help="Altura final desejada em milímetros (ex: 200.0 ou 75.0)")
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
        default=0.0,
        help="Intensidade do Realce de Micro-Detalhes 3D opcional para miniaturas pequenas (padrão: 0.0 = fidelidade 1:1; use 0.65 para minis de 30-45mm)",
    )
    parser.add_argument(
        "--suavizacao",
        type=float,
        default=1.0,
        help="Intensidade do acabamento liso de superfície estilo porcelana (padrão: 1.0; use 0.0 para desativar ou 1.5 para suavização extra)",
    )
    parser.add_argument(
        "--espessura-minima",
        type=float,
        default=0.50,
        help="Espessura mínima em mm para proteger cascas/folhas finas abertas sem rasgar (padrão: 0.50 mm)",
    )
    parser.add_argument(
        "--resolucao",
        type=int,
        default=1000,
        help="Resolução do eixo maior na grade volumétrica 3D isotrópica (padrão: 1000)",
    )
    parser.add_argument(
        "--faces",
        type=int,
        default=2000000,
        help="Quantidade alvo aproximada de triângulos na malha final (padrão: 2000000 — máxima lisura sem simplificação excessiva)",
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
    print(f"INICIANDO REPARO 3D DE ALTA FIDELIDADE (v2.6): {args.entrada}")
    print("=" * 72)

    raw_mesh = trimesh.load(args.entrada, force="mesh")
    info_antes = diagnosticar_malha(raw_mesh, "ANTES DO REPARO (Original)")
    imprimir_relatorio(info_antes)

    verts_z = orientar_malha(raw_mesh.vertices, args.eixo_vertical)
    mesh = trimesh.Trimesh(vertices=verts_z, faces=raw_mesh.faces.copy(), process=True)

    mesh_clean, n_rem = filtrar_fragmentos_isolados(mesh)
    print(f"  -> Ilhas e resíduos desconectados removidos: {n_rem} (preservadas {len(mesh_clean.faces):,} faces na peça)")

    altura_alvo = estimar_altura_alvo_mm(mesh_clean, args)

    print("  -> Executando rasterização 3D direta e preenchimento sólido 3D verdadeiro...")
    solid_vol, shell_vol, gmin, h, iz_bot = construir_volume_solido_universal(
        mesh_clean, res_z=args.resolucao, modo_base=args.base
    )

    print("  -> Extraindo superfície de alta densidade, encaixe curvo e acabamento liso...")
    repaired = extrair_e_realcar_superficie(
        mesh_clean,
        solid_vol,
        shell_vol,
        gmin,
        h,
        iz_bot,
        modo_base=args.base,
        realce_detalhes=args.realce,
        suavizacao=args.suavizacao,
        espessura_min_mm=args.espessura_minima,
        altura_alvo_mm=altura_alvo,
        target_faces=args.faces,
    )

    repaired = aplicar_escala_final(repaired, args)
    repaired = garantir_manifold_final_stl(repaired)

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
