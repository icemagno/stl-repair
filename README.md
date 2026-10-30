# Utilitário de Reparo e Realce 3D para STLs (`reparar_stl.py`)

Documentação oficial do utilitário de pós-processamento geométrico desenvolvido para transformar malhas 3D brutas geradas por IA no **ComfyUI** (Hunyuan3D, TripoSR, Trellis, CRM, etc.) em **sólidos 100% estanques (*watertight 2-manifold*)**, prontos para fatiamento e impressão 3D no **Orca Slicer** e **Creality Print**.

---

## 1. Problemas Resolvidos pelo Utilitário

Modelos exportados diretamente de geradores 3D por IA costumam apresentar defeitos severos que impedem ou arruínam a impressão 3D:

1. **Peça Oca com Parede Dupla Microscópica:** O gerador cria uma casca interna falsa separada da externa por apenas ~0,2% da largura da peça (~0,18 mm na escala real). Como essa espessura é menor que o diâmetro do bico de `0.4 mm`, o fatiador ignora as paredes e a peça aparece vazia ou cheia de falhas na pré-visualização.
2. **Fundo ou Pedestal Aberto:** A base do objeto frequentemente não é tampada ou possui rasgos que impedem o fatiador de reconhecer o interior como volume sólido.
3. **Fragmentos Flutuantes Isolados:** Pequenas ilhas de triângulos desconectadas flutuando dentro ou fora da peça, gerando suportes desnecessários ou falhas no ar.
4. **Arestas Abertas e *Non-Manifold*:** Buracos na malha, faces auto-intersectantes e normais invertidas.
5. **Eixo Vertical Trocado (*Y-Up* vs *Z-Up*) e Escala Unitária:** O ComfyUI exporta malhas deitadas (eixo Y para cima) e com tamanho total de ~`1.0 mm` (caixa normalizada `[-0.5, 0.5]`).
6. **Detalhes Faciais Rasos:** Olhos, pálpebras, narinas e lábios gerados por IA costumam ter baixo contraste topográfico, ficando "lavados" após o fatiamento FDM em bico `0.4 mm`.

---

## 2. Como Funciona o Pipeline (7 Etapas)

O script [`reparar_stl.py`](file:///C:/Magno/Projetos/reparar_stl.py) executa automaticamente o seguinte fluxo sem depender de softwares externos (não exige Blender):

1. **Reorientação Inteligente (`orientar_malha`):** Detecta automaticamente se a peça está no padrão *Y-Up* do ComfyUI e rotaciona para o padrão *Z-Up* dos fatiadores 3D.
2. **Limpeza de Fragmentos (`filtrar_fragmentos_isolados`):** Analisa os componentes conectados da malha e descarta ilhas flutuantes isoladas (mantendo apenas o corpo principal e partes legítimas que representem ao menos 1% das faces ou 0,5% da altura).
3. **Fechamento Morfológico Adaptativo e Flood-Fill 6D (`construir_volume_solido_universal`):**
   - Voxeliza a superfície em alta definição (`1000` fatias verticais por padrão).
   - Utiliza fechamento morfológico com raio físico real (~`0.75 mm`) para fundir paredes duplas ocas e costurar micro-furos, mas **sem tampar espaços legítimos** (como o vão entre pernas, braços ou capas de peças sem base).
   - Inunda o ar externo a partir das 6 bordas do domínio 3D e converte 100% das cavidades internas em volume maciço.
4. **Projeção BVH com Prevenção de Degeneração (`extrair_e_realcar_superficie`):**
   - Extrai a isosuperfície estanque e crava os vértices na casca **externa** original com filtro de normal e limite de deslocamento por célula, preservando a fidelidade da escultura original sem criar dobras.
5. **Realce de Micro-Detalhes 3D (*3D Unsharp Mask* + *Crease Sharpening*):**
   - Calcula a curvatura média 3D de alta frequência e acentua sulcos (olhos, pálpebras, narinas, boca, costuras de roupa) em `±0.15 mm` a `0.22 mm`, tornando os traços visíveis para um bico de `0.4 mm`.
6. **Proteção de Espessura Mínima (`--espessura-minima`):**
   - Mede a espessura local da peça pelo campo de distância interna (EDT) e engrossa automaticamente pontas, orelhas ou lâminas mais finas que `0.50 mm` para evitar que desapareçam no fatiador.
7. **Simplificação e Validação 2-Manifold Nativa em C++ (`otimizar_e_validar_manifold`):**
   - Utiliza a biblioteca C++ `manifold3d` (`Manifold.simplify`) diretamente na memória RAM para otimizar a malha (~600 mil faces por padrão) garantindo **0 arestas abertas**, **0 arestas non-manifold** e **100% de estanqueidade**.

---

## 3. Arquivos do Utilitário

| Arquivo | Função |
| :--- | :--- |
| [`reparar_stl.py`](file:///C:/Magno/Projetos/reparar_stl.py) | Script principal em Python com todo o motor de reparo volumétrico, realce 3D e validação manifold. |
| [`reparar_stl.bat`](file:///C:/Magno/Projetos/reparar_stl.bat) | Atalho para Windows que executa o script usando automaticamente o Python embutido do ComfyUI (`M:\ComfyUI\python_embeded\python.exe`). Aceita *Drag & Drop* (arrastar e soltar arquivo `.stl`). |

> **Nota sobre dependências:** O utilitário é 100% autônomo em cima do `M:\ComfyUI\python_embeded\python.exe` (`numpy`, `scipy`, `trimesh`, `scikit-image`, `manifold3d`) e **não requer Blender instalado**.

---

## 4. Como Executar na Linha de Comando

### Sintaxe Básica (usando o `.bat`)

```bat
C:\Magno\Projetos\reparar_stl.bat <arquivo_entrada.stl> [opções]
```

### Sintaxe Direta (chamando o Python do ComfyUI)

```powershell
M:\ComfyUI\python_embeded\python.exe C:\Magno\Projetos\reparar_stl.py <arquivo_entrada.stl> [opções]
```

### Uso Rápido por Arrastar e Soltar (*Drag & Drop*)
Basta arrastar qualquer arquivo `.stl` no Windows Explorer e soltar em cima do ícone [`reparar_stl.bat`](file:///C:/Magno/Projetos/reparar_stl.bat). Ele gerará automaticamente o arquivo `<nome>_reparado.stl` na mesma pasta, com `75 mm` de altura padrão e realce de detalhes ativado.

---

## 5. Tabela de Opções da Linha de Comando

| Opção / Flag | Argumento | Valor Padrão | Descrição Detalhada |
| :--- | :--- | :--- | :--- |
| `entrada` *(posicional)* | `<caminho.stl>` | *(Obrigatório)* | Caminho do arquivo `.stl` bruto que será reparado (ex.: `all.stl`). |
| `-o`, `--saida` | `<caminho.stl>` | `<entrada>_reparado.stl` | Caminho e nome do arquivo `.stl` binário reparado de saída. |
| `-r`, `--referencia` | `<caminho.stl>` | `None` | Caminho de um arquivo `.stl` de referência para copiar automaticamente as proporções de tamanho (ex.: `referencia.stl`). |
| `--escala-ref` | `base` \| `altura` | `base` | Define qual dimensão do arquivo `--referencia` será igualada:<ul><li>`base`: iguala o diâmetro/largura da base da peça à referência (ideal para miniaturas de RPG com pedestal padrão).</li><li>`altura`: iguala a altura total (Z) da peça à altura da referência.</li></ul> |
| `--altura` | `<float (mm)>` | `75.0` *(se normalizado)* | Define diretamente a altura final exata da peça em milímetros (ex.: `--altura 43.5` ou `--altura 80`). Tem prioridade sobre `--referencia`. |
| `--largura-base` | `<float (mm)>` | `None` | Define diretamente a largura/diâmetro médio (XY) da peça em milímetros (ex.: `--largura-base 25.0`), escalando a altura proporcionalmente. |
| `--fator-escala` | `<float>` | `None` | Aplica um fator multiplicador direto nas coordenadas da peça (ex.: `--fator-escala 50.0`). |
| `--eixo-vertical` | `auto` \| `y` \| `z` \| `x` \| `-y` \| `-z` | `auto` | Eixo que aponta para cima na malha original:<ul><li>`auto`: detecta automaticamente se a malha veio em *Y-Up* do ComfyUI e converte para *Z-Up*.</li><li>`y`, `z`, `x`, `-y`, `-z`: força manualmente qual eixo original deve virar o eixo Z positivo.</li></ul> |
| `--base` | `auto` \| `plana` \| `preservar` | `auto` | Controla o tratamento dos pontos de apoio inferiores da peça:<ul><li>`auto` / `plana`: sela aberturas voltadas para baixo no plano da mesa e nivela os primeiros ~0,2% da base em `Z = 0.0` para aderência perfeita na mesa de impressão (funciona tanto para peças **com pedestal** quanto para peças **sem base** apoiadas apenas nos pés).</li><li>`preservar`: não achata nem nivela os vértices inferiores (indicado para esferas, peças suspensas ou encaixes inferiores curvos).</li></ul> |
| `--realce` | `<float>` | `0.65` | Intensidade do **Realce de Micro-Detalhes 3D** (*3D Unsharp Mask*) para olhos, nariz, boca e relevos:<ul><li>`0.0`: desativa o realce (mantém a superfície suavizada original).</li><li>`0.65`: realce equilibrado recomendado para bico `0.4 mm`.</li><li>`0.90` a `1.20`: realce extra-forte para miniaturas muito pequenas (`30 mm`–`45 mm`).</li></ul> |
| `--espessura-minima` | `<float (mm)>` | `0.50` | Espessura mínima de parede em milímetros. Regiões mais finas que esse valor são suavemente engrossadas para não falharem em bicos de `0.4 mm`. Use `0` para desativar. |
| `--resolucao` | `<int>` | `1000` | Resolução vertical (número de voxels no eixo Z) da grade volumétrica 3D. Valores maiores (`1000`–`1200`) capturam mais micro-detalhes, mas usam mais memória RAM e tempo (~60s). Valores menores (`600`–`800`) rodam em ~20s. |
| `--faces` | `<int>` | `600000` | Quantidade alvo aproximada de triângulos na malha final simplificada pelo `manifold3d`. Mantém o arquivo final leve (~30 MB) sem perda visual. |
| `--diagnostico` | *(sem valor)* | `False` | Quando informada, gera uma imagem `<saida>_diagnostico.png` contendo 3 cortes transversais horizontais (15%, 45%, 75%), 2 cortes verticais (X=0, Y=0) e 3 vistas sombreadas 3D para conferência visual imediata. |
| `-h`, `--help` | *(sem valor)* | — | Exibe o resumo de ajuda e todas as opções disponíveis no terminal. |

---

## 6. Exemplos Práticos de Uso

### Exemplo 1: Igualar a base da peça à base de um arquivo de referência (`referencia.stl`)
Ideal quando você tem uma base/pedestal padrão e quer que a nova miniatura tenha exatamente o mesmo diâmetro de base:
```bat
C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\all.stl --referencia C:\Magno\Projetos\referencia.stl --escala-ref base --diagnostico
```

### Exemplo 2: Igualar a altura total à altura de um arquivo de referência
```bat
C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\all.stl --referencia C:\Magno\Projetos\referencia.stl --escala-ref altura -o C:\Magno\Projetos\all_altura_ref.stl
```

### Exemplo 3: Reparar uma peça SEM base (apoiada nas pernas/pés) definindo altura de 65 mm
O algoritmo fecha o interior oco das pernas e do tronco **sem fechar o espaço vazio entre as pernas**:
```bat
C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\personagem_sem_base.stl --altura 65
```

### Exemplo 4: Miniatura pequena de RPG (35 mm) com realce forte de rosto (olhos, nariz e boca)
Para peças muito pequenas impressas em bico `0.4 mm`, aumentar `--realce` para `0.95` e `--espessura-minima` para `0.55` destaca ainda mais as feições no fatiador:
```bat
C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\goblin.stl --altura 35 --realce 0.95 --espessura-minima 0.55 --diagnostico
```

### Exemplo 5: Peça suspensa ou arredondada embaixo (sem achatar o fundo em Z=0)
```bat
C:\Magno\Projetos\reparar_stl.bat C:\Magno\Projetos\orbe_flutuante.stl --altura 50 --base preservar
```

---

## 7. Dicas para Impressão com Bico `0.4 mm` (Orca Slicer / Creality Print)

Para aproveitar ao máximo os detalhes faciais realçados pelo script usando um bico padrão de **0.4 mm**:

1. **Gerador de Paredes (*Wall Generator*):** Ative o modo **Arachne** (no lugar de *Classic*). O Arachne varia dinamicamente a largura de extrusão entre `0.28 mm` e `0.50 mm`, permitindo desenhar narinas, pálpebras e pontas finas com muito mais precisão.
2. **Altura de Camada (*Layer Height*):** Use **`0.12 mm`** (ou **Altura de Camada Variável / Adaptativa** entre `0.08 mm` no rosto e `0.16 mm` na base). Como a resolução vertical (eixo Z) depende apenas do motor de passo Z e não do diâmetro do bico, camadas de `0.08 mm`–`0.12 mm` revelam perfeitamente a curvatura dos olhos e lábios.
3. **Ordem das Paredes (*Wall Printing Order*):** Use **Inner/Outer** (Interna/Externa) ou **Inner/Outer/Inner** para melhorar a precisão dimensional dos micro-relevos externos.
