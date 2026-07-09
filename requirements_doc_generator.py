import ai_provider

MAX_PAGES_IN_PROMPT = 10
MAX_CHARS_PER_PAGE = 12000

SYSTEM_PROMPT = """
Você é um Analista de Requisitos experiente. Você recebe o HTML renderizado de uma ou
mais páginas de um site/produto real (já baixadas) e deve produzir, por engenharia
reversa, um documento de requisitos em Markdown que um desenvolvedor (humano ou uma IA de
codificação) usaria para reimplementar ou dar manutenção nesse produto com segurança.

Regras:
1. Baseie-se SOMENTE no que está observável no HTML fornecido (textos, formulários,
   botões, campos, mensagens, estrutura, nomes de campos/rotas). NÃO invente
   funcionalidades sem nenhuma evidência no material fornecido.
2. Onde a regra de negócio não for 100% explícita (ex.: validação de senha, regras de
   limite, políticas de cobrança), marque como "Suposição" e descreva o comportamento
   mais provável dado o contexto, deixando claro que é inferido, não observado.
3. Escreva em português (pt-BR).
4. Para cada página fornecida, gere uma seção com:
   - Requisitos Funcionais (RF-XX): o que o usuário consegue fazer nessa página.
   - Requisitos Não Funcionais (RNF-XX): apenas os que têm evidência observável no HTML
     (responsividade, acessibilidade básica — labels/alt/aria, i18n, lazy-loading, etc.).
     Se não houver evidência de nenhum, escreva "Nenhum requisito não funcional
     identificável a partir do HTML".
   - Cenários no formato Gherkin em português (Funcionalidade / Cenário / Dado / Quando /
     Então), cobrindo o fluxo principal e pelo menos um caso alternativo/erro quando
     aplicável (ex.: campo obrigatório vazio, credenciais inválidas, item já existente).
5. Inclua uma seção "Visão Geral" no topo do documento, resumindo o propósito do
   produto/site e o domínio de negócio, com base no conjunto de páginas fornecido.
6. Formato de saída: um único documento Markdown, sem nenhum texto fora dele. Comece
   direto com "# Documento de Requisitos — <nome do site, inferido do HTML>".
"""


class RequirementsDocGenerationError(Exception):
    pass


def is_available():
    return ai_provider.is_available()


def _build_pages_section(pages_html):
    pages = list(pages_html or [])[:MAX_PAGES_IN_PROMPT]
    blocks = []
    for i, (page_url, html_content) in enumerate(pages):
        truncated = (html_content or '')[:MAX_CHARS_PER_PAGE]
        blocks.append(f"### Página {i + 1} — {page_url}\n```html\n{truncated}\n```")
    return "\n\n".join(blocks)


def generate_requirements_doc(pages_html, log_callback=None, max_tokens=12000):
    """pages_html: lista de (page_url, html_transformado) — todas as páginas baixadas."""
    log = log_callback or (lambda m: None)

    if not is_available():
        raise RequirementsDocGenerationError(
            "Nenhum provedor de IA configurado (defina GEMINI_API_KEYS/GEMINI_API_KEY ou ANTHROPIC_API_KEY)"
        )

    if not pages_html:
        raise RequirementsDocGenerationError("Nenhuma página capturada para gerar o documento de requisitos")

    pages_section = _build_pages_section(pages_html)

    try:
        text = ai_provider.generate_text(SYSTEM_PROMPT, pages_section, max_tokens=max_tokens, log_callback=log)
    except ai_provider.AIGenerationError as e:
        raise RequirementsDocGenerationError(str(e))

    return text
