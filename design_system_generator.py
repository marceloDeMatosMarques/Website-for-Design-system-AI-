import os
import re
import ai_provider

_SPEC_PATH = os.path.join(os.path.dirname(__file__), 'Extract HTML Design System.md')

_ALLOWED_COMPONENT_PREFIXES = ('components/atoms/', 'components/molecules/', 'components/organisms/')

_OUTPUT_CONTRACT = """
## FORMATO DE SAÍDA (OBRIGATÓRIO — substitui qualquer instrução anterior de "salvar arquivo")

Não escreva em disco. Responda EXATAMENTE neste formato, um bloco por arquivo, sem nenhum
texto fora dos blocos:

=== design-system.html ===
<conteúdo completo do arquivo>

=== components/atoms/<nome>.html ===
<conteúdo>

=== components/molecules/<nome>.html ===
<conteúdo>

=== components/organisms/<nome>.html ===
<conteúdo>

Regras adicionais:
- Gere de 2 a 5 componentes "atoms" (ex.: button, input, badge), 1 a 3 "molecules"
  (ex.: card, form-group, nav-item) e 1 a 3 "organisms" (ex.: navbar, hero, footer) —
  apenas os que existirem de fato no HTML de referência.
- Cada arquivo de componente deve ser um HTML autocontido e reutilizável (incluindo um
  <style> com as classes/CSS originais necessárias), reaproveitando classes/CSS EXATOS
  do HTML de referência, nunca inventando estilos novos.
- Não repita CSS além do necessário para cada componente funcionar isoladamente.
- Não inclua nenhum texto fora dos blocos "=== caminho ===".

## ESTRUTURA DE PASTAS REAL (JÁ EXISTE EM DISCO — NÃO INVENTE OUTRA)

Os arquivos que você vai gerar fazem parte de um pacote que JÁ contém os assets reais
baixados do site (você não está inventando nada, apenas reaproveitando o que já existe):

    /                              <- design-system.html vai aqui (raiz)
    css/*.css                      <- stylesheets já baixados
    js/*.js                        <- scripts já baixados
    fonts/*.woff2, *.ttf, ...       <- fontes já baixadas
    img/*.png, *.jpg, *.svg, ...    <- imagens já baixadas
    components/atoms/*.html        <- 2 níveis abaixo da raiz
    components/molecules/*.html    <- 2 níveis abaixo da raiz
    components/organisms/*.html    <- 2 níveis abaixo da raiz

O HTML de referência fornecido abaixo já foi processado e reescrito para apontar
EXATAMENTE para esses arquivos locais (ex.: `css/style_a1b2c3d4e5f6.css`,
`img/foto_1a2b3c4d5e6f.webp`). A seção "ARQUIVOS REAIS DISPONÍVEIS" lista todos os
arquivos que existem de fato — use SOMENTE esses paths e nomes de arquivo. NUNCA invente
nomes de arquivo novos (imagem, fonte, CSS ou JS) e NUNCA use paths absolutos começando
com "/" (eles não funcionam quando o arquivo é aberto localmente).

Regra de profundidade (ajuste o path conforme onde o arquivo que você está gerando vai
morar):
- Em `design-system.html` (raiz): use o path exatamente como aparece no HTML de
  referência ou na lista de arquivos (ex.: `css/style_a1b2c3d4e5f6.css`).
- Em qualquer arquivo dentro de `components/atoms/`, `components/molecules/` ou
  `components/organisms/` (2 níveis abaixo da raiz): adicione o prefixo `../../` ao
  mesmo path (ex.: `../../css/style_a1b2c3d4e5f6.css`).

Para reaproveitar estilos (cores, tipografia, espaçamento, gradientes, animações),
PREFIRA linkar o arquivo CSS real via `<link rel="stylesheet" href="...">` (copiando o
`<link>` que já existe no HTML de referência, ajustando só o prefixo de path conforme a
regra acima) em vez de copiar trechos de CSS manualmente — isso garante que fontes e
ícones referenciados dentro do CSS (via `url()`) continuem funcionando.

Se precisar de uma regra CSS específica que não existe em nenhum arquivo linkado, você
pode declará-la inline, mas NUNCA usando `url()` para um arquivo que não esteja
explicitamente presente na lista de "ARQUIVOS REAIS DISPONÍVEIS".

Se um exemplo (card, avatar etc.) precisar de uma imagem ilustrativa e não houver uma
imagem real adequada disponível, use um bloco colorido (div com background-color/gradient
reaproveitado do próprio design) como placeholder — nunca um `<img src="...">` com um
nome de arquivo inventado.
"""


class DesignSystemGenerationError(Exception):
    pass


def is_available():
    return ai_provider.is_available()


def _load_spec():
    with open(_SPEC_PATH, 'r', encoding='utf-8') as f:
        return f.read()


MAX_PAGES_IN_PROMPT = 6
MAX_CHARS_PER_PAGE = 15000


def _build_prompt(pages_html, css_bodies, asset_manifest=None):
    spec = _load_spec()
    css_joined = "\n\n/* --- next stylesheet --- */\n\n".join(css_bodies)
    css_joined = css_joined[:80000]

    manifest_list = "\n".join(f"- {path}" for path in (asset_manifest or []))
    manifest_section = (
        "## ARQUIVOS REAIS DISPONÍVEIS (use apenas estes — nada além disso existe)\n\n"
        f"{manifest_list if manifest_list else '(nenhum asset externo foi baixado para este site)'}"
    )

    pages_note = """
## PÁGINAS DE REFERÊNCIA FORNECIDAS ABAIXO

Você recebe o HTML de uma ou mais páginas já baixadas do mesmo site. A PRIMEIRA página
listada é a home — é ela que deve ser usada para a seção 0 (Hero, clone exato). As
páginas seguintes (se houver) são referência ADICIONAL só para você encontrar variações
de botões, formulários, cards e outros componentes que não aparecem na home — use-as para
enriquecer as seções de Typography/Colors/UI Components/Layout/Motion, sempre reaproveitando
classes/CSS exatos de qualquer uma das páginas fornecidas.
"""

    pages = list(pages_html or [])[:MAX_PAGES_IN_PROMPT]
    pages_blocks = []
    for i, (page_url, html_content) in enumerate(pages):
        role = "HOME" if i == 0 else "referência adicional"
        truncated_html = (html_content or '')[:MAX_CHARS_PER_PAGE]
        pages_blocks.append(
            f"### Página {i + 1} ({role}) — {page_url}\n```html\n{truncated_html}\n```"
        )
    pages_section = "\n\n".join(pages_blocks) if pages_blocks else "(nenhuma página capturada)"

    system_prompt = spec + "\n\n" + _OUTPUT_CONTRACT + "\n\n" + manifest_section + "\n\n" + pages_note
    user_content = (
        f"{pages_section}\n\n"
        f"CSS capturado (conteúdo original de todas as páginas, apenas para consulta de valores "
        f"como cores/tipografia — não copie url() deste bloco literalmente, use a lista de "
        f"ARQUIVOS REAIS DISPONÍVEIS):\n```css\n{css_joined}\n```"
    )
    return system_prompt, user_content


_BLOCK_RE = re.compile(r'^===\s*(.+?)\s*===\s*$', re.MULTILINE)


def _is_safe_path(path):
    if not path or '..' in path or path.startswith(('/', '\\')) or ':' in path:
        return False
    if path == 'design-system.html':
        return True
    return path.startswith(_ALLOWED_COMPONENT_PREFIXES) and path.endswith(('.html', '.css'))


def _parse_file_blocks(text):
    parts = _BLOCK_RE.split(text)
    files = {}
    # parts[0] é preâmbulo (ignorado); depois alterna [path, content, path, content, ...]
    for i in range(1, len(parts) - 1, 2):
        path = parts[i].strip()
        content = parts[i + 1].strip('\n')
        if _is_safe_path(path):
            files[path] = content
    return files


def generate_design_system(pages_html, css_bodies, asset_manifest=None, log_callback=None, max_tokens=32000):
    """pages_html: lista de (page_url, html_transformado); a primeira é usada como home/hero."""
    log = log_callback or (lambda m: None)

    if not is_available():
        raise DesignSystemGenerationError(
            "Nenhum provedor de IA configurado (defina GEMINI_API_KEYS/GEMINI_API_KEY ou ANTHROPIC_API_KEY)"
        )

    if not pages_html:
        raise DesignSystemGenerationError("Nenhuma página capturada para gerar o Design System")

    system_prompt, user_content = _build_prompt(pages_html, css_bodies, asset_manifest)

    try:
        text = ai_provider.generate_text(system_prompt, user_content, max_tokens=max_tokens, log_callback=log)
    except ai_provider.AIGenerationError as e:
        raise DesignSystemGenerationError(str(e))

    files = _parse_file_blocks(text)

    if 'design-system.html' not in files:
        raise DesignSystemGenerationError(
            "Resposta da IA não continha 'design-system.html' no formato esperado"
        )

    return files
