# Utilitário de Reparo 3D para STLs — v2.3 (`reparar_stl.py`)

Documentação oficial do utilitário de pós-processamento geométrico (pasta `C:\Magno\Projetos\stl-repair\`) desenvolvido para transformar malhas 3D brutas geradas por IA no **ComfyUI** (Hunyuan3D, TripoSR, Trellis, CRM, etc.) em **sólidos 100% estanques (*watertight 2-manifold*)** com **fidelidade estrutural 1:1**, prontos para impressão 3D no **Orca Slicer** e **Creality Print**.

---

## 1. Problemas Resolvidos pelo Utilitário

Modelos exportados diretamente de geradores 3D por IA costumam apresentar defeitos severos que impedem ou prejudicam a impressão 3D:

1. **Peça Oca com Parede Dupla Microscópica:** O gerador cria uma casca interna falsa separada da externa por apenas ~0,2% da altura da peça. Como essa parede é mais fina que o bico de `0.4 mm`, o fatiador ignora trechos inteiros e o modelo aparece oco ou esburacado.
2. **Fundo ou Pedestal Aberto:** A base do objeto frequentemente não é tampada, impedindo o fatiador de reconhecer o interior como volume sólido.
3. **Fragmentos Flutuantes Isolados:** Pequenas ilhas de triângulos desconectadas flutuando no ar fora da peça.
4. **Cascas Finas / Capas Abertas (Zero-Thickness):** Barras de capas, fitas e abas finas costumam ter espessura zero ou sub-milimétrica, rasgando ou sumindo no fatiamento.
5. **Arestas Abertas e *Non-Manifold*:** Milhares de costuras não-manifold, furos e normais invertidas.
6. **Eixo Vertical Trocado (*Y-Up* vs *Z-Up*) e Escala Normalizada:** O ComfyUI exporta malhas deitadas e com dimensão unitária (~`1.0 mm`).

---

## 2. Arquitetura v2.3 — Zero Modificação Estrutural Indevida e Superfície Homogênea

O pipeline do [`reparar_stl.py`](file:///C:/Magno/Projetos/stl-repair/reparar_stl.py) foi projetado em **3D verdadeiro** para corrigir o interior sem alterar a anatomia externa da escultura:

1. **Reorientação Inteligente (`orientar_malha`):** Detecta automaticamente malhas em *Y-Up* do ComfyUI e rotaciona para *Z-Up*.
2. **Filtro de Fragmentos por Vértices + Proximidade 3D (`filtrar_fragmentos_isolados`):**
   - Usa grafo esparso de vértices compartilhados (`scipy.sparse.csgraph`), que **não quebra em arestas non-manifold**, somado a crescimento iterativo por proximidade 3D (`cKDTree`).
   - Remove **apenas** fragmentos genuinamente flutuando no ar longe da peça, preservando 100% de abas de capa, mechas de cabelo, dedos e adereços encostados no corpo.
3. **Rasterização 3D Direta + Flood-Fill 3D Verdadeiro (`construir_volume_solido_universal`):**
   - Subdivide e rasteriza todos os triângulos diretamente em uma grade 3D isotrópica (`dx = dy = dz`).
   - A inundação do ar externo ocorre em **3D verdadeiro** (`ndimage.binary_fill_holes` a partir das 6 faces externas da caixa), permitindo que o ar circule livremente pela virilha, axilas e dobras internas da capa — **sem jamais criar pontes ou prateleiras horizontais**.
   - Somente cavidades 100% fechadas em 3D (interior oco do tronco, cabeça, membros e pedestal) tornam-se maciças.
4. **Proteção Contínua em SDF para Capas e Folhas Finas (`extrair_e_realcar_superficie`):**
   - Identifica automaticamente regiões de folha aberta ou espessura sub-voxel (como barras inferiores de capas esvoaçantes) e aplica união suave em campo de distância contínuo (`SDF`), impedindo rasgos, furos ou degraus.
5. **Encaixe K-Nearest com Filtro Estrito de Normal + Interpolação Trilinear:**
   - Rejeita 100% dos triângulos da parede oca interna (evitando depressões/cicatrizes nos joelhos, quadril ou capa), corrige normais invertidas e projeta a superfície com interpolação trilinear suave.
6. **Simplificação 2-Manifold Pré-Encaixe + Filtro Anti-Spike (`extrair_e_realcar_superficie` / `_remover_spikes_malha`):**
   - Executa `manifold3d.Manifold.simplify()` diretamente em memória RAM (**sem precisar do Blender nem de programas externos**) antes do encaixe fino e com limite controlado abaixo de 1 milhão de faces (evitando alertas no Creality Print), seguido de verificação geométrica/SDF anti-spike e polimento Taubin preservador de volume.

---

## 3. Arquivos na Pasta `C:\Magno\Projetos\stl-repair`

| Arquivo | Função |
| :--- | :--- |
| [`reparar_stl.py`](file:///C:/Magno/Projetos/stl-repair/reparar_stl.py) | Motor Python v2.3 de reparo volumétrico 3D, proteção de folhas finas, anti-spike e validação manifold. |
| [`reparar_stl.bat`](file:///C:/Magno/Projetos/stl-repair/reparar_stl.bat) | Atalho para Windows/PowerShell que executa o script usando o Python embutido do ComfyUI (`M:\ComfyUI\python_embeded\python.exe`). Aceita *Drag & Drop*. |
| [`stl-repair.md`](file:///C:/Magno/Projetos/stl-repair/stl-repair.md) | Este manual de referência e linha de comando. |

---

## 4. Como Executar na Linha de Comando

Dentro da pasta `C:\Magno\Projetos\stl-repair`:

```powershell
.\reparar_stl.bat <arquivo_entrada.stl> [opções]
```

Ou chamando diretamente o Python do ComfyUI de qualquer pasta:

```powershell
M:\ComfyUI\python_embeded\python.exe C:\Magno\Projetos\stl-repair\reparar_stl.py <arquivo_entrada.stl> [opções]
```

---

## 5. Tabela Completa de Opções da Linha de Comando

| Opção / Flag | Argumento | Valor Padrão | Descrição Detalhada |
| :--- | :--- | :--- | :--- |
| `entrada` *(posicional)* | `<caminho.stl>` | *(Obrigatório)* | Caminho do arquivo `.stl` bruto a ser reparado (ex.: `ComfyUI_00020.stl`). |
| `-o`, `--saida` | `<caminho.stl>` | `<entrada>_reparado.stl` | Caminho e nome do arquivo `.stl` binário reparado de saída. |
| `-r`, `--referencia` | `<caminho.stl>` | `None` | Arquivo `.stl` de referência opcional para copiar a escala automaticamente. |
| `--escala-ref` | `base` \| `altura` | `base` | Ao usar `--referencia`, escolhe se iguala a largura/diâmetro da `base` ou a `altura` total. |
| `--altura` | `<float (mm)>` | `75.0` *(se normalizado)* | Altura final exata da peça em milímetros (ex.: `--altura 200` ou `--altura 43.5`). |
| `--largura-base` | `<float (mm)>` | `None` | Largura/diâmetro médio (XY) desejado em milímetros, escalando a altura proporcionalmente. |
| `--fator-escala` | `<float>` | `None` | Fator multiplicador direto nas coordenadas da malha. |
| `--eixo-vertical` | `auto` \| `y` \| `z` \| `x` \| `-y` \| `-z` | `auto` | Eixo vertical original (`auto` detecta o padrão *Y-Up* do ComfyUI e converte para *Z-Up*). |
| `--base` | `auto` \| `plana` \| `preservar` | `auto` | Tratamento do apoio inferior:<ul><li>`auto` / `plana`: veda bases/solas abertas encostadas em `Z = 0` e nivela o contato com a mesa.</li><li>`preservar`: mantém o fundo intocado (ideal para esferas ou objetos suspensos).</li></ul> |
| `--realce` | `<float>` | `0.0` | Intensidade do **Realce de Micro-Detalhes 3D** (*3D Unsharp Mask*):<ul><li>`0.0` *(padrão)*: **Fidelidade 1:1** à escultura original (recomendado para estátuas médias/grandes, como `100 mm`–`250 mm`).</li><li>`0.55` a `0.85`: acentua sulcos de olhos, nariz e lábios em miniaturas pequenas de RPG (`28 mm`–`45 mm`).</li></ul> |
| `--espessura-minima` | `<float (mm)>` | `0.50` | Espessura mínima (mm) aplicada em SDF contínuo apenas sobre folhas finas/bordas abertas (como pontas de capa) para não rasgarem no fatiador com bico `0.4 mm`. |
| `--resolucao` | `<int>` | `950` | Resolução do maior eixo na grade volumétrica 3D isotrópica (`400` a `1400`). |
| `--faces` | `<int>` | `800000` | Contagem alvo aproximada de triângulos na simplificação via `manifold3d` (mantém a malha abaixo do alerta de 1 milhão de faces do Creality Print com superfície lisa e sem facetas visíveis). |
| `--diagnostico` | *(sem valor)* | `False` | Salva uma prancha `<saida>_diagnostico.png` com cortes transversais 2D e vistas 3D. |
| `-h`, `--help` | *(sem valor)* | — | Mostra todas as opções disponíveis no terminal. |

---

## 6. Exemplos Práticos de Uso

### Exemplo 1: Estátua grande (200 mm) com capa e poses complexas (fidelidade estrutural 1:1)
```powershell
.\reparar_stl.bat ComfyUI_00020.stl --altura 200
```

### Exemplo 2: Igualar o diâmetro da base ao de uma peça de referência
```powershell
.\reparar_stl.bat all.stl --referencia referencia.stl --escala-ref base --diagnostico
```

### Exemplo 3: Miniatura pequena de RPG (38 mm) ativando o realce facial para bico `0.4 mm`
```powershell
.\reparar_stl.bat guerreiro_mini.stl --altura 38 --realce 0.65 --espessura-minima 0.55
```

### Exemplo 4: Peça sem pedestal (apoiada diretamente nos pés) ou suspensa
```powershell
.\reparar_stl.bat personagem_sem_base.stl --altura 75
.\reparar_stl.bat orbe_flutuante.stl --altura 60 --base preservar
```

---

## 7. Dicas de Fatiamento para Bico `0.4 mm` (Orca Slicer / Creality Print)

1. **Gerador de Paredes (*Wall Generator*):** Use **Arachne** para que bordas finas de capas, dedos e feições do rosto sejam fatiadas com largura de linha adaptativa.
2. **Altura de Camada (*Layer Height*):** `0.12 mm` a `0.16 mm` para estátuas grandes (`150–200 mm`) ou `0.08 mm` a `0.12 mm` para miniaturas pequenas (`30–50 mm`).
