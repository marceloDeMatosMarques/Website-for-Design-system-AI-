# 🌐 Website for Design System AI

Clona qualquer site com JS renderizado, baixa múltiplas páginas (com suporte a login), e usa IA pra reconstruir o design system em componentes atômicos + gerar documentação de requisitos em BDD — tudo automático.

## ✨ Funcionalidades

- 📥 Download completo de sites (HTML, CSS, JS, imagens, fontes)
- 🎭 Renderização de JavaScript usando Playwright/Chromium
- 🖼️ Captura de imagens lazy-loaded
- 📚 Modo avançado: descoberta e download de múltiplas páginas do mesmo site, com assets organizados em pastas por tipo (css/js/fonts/img) e navegação entre páginas religada
- 🔒 Suporte a login (usuário/senha) antes de baixar páginas restritas
- 🎨 Geração de Design System via IA: `design-system.html` + componentes atomic design (atoms/molecules/organisms) reaproveitando classes/CSS reais do site
- 📋 Geração automática de documento de requisitos (`REQUISITOS.md`) em formato BDD (Gherkin, pt-BR), com requisitos funcionais e não funcionais por página
- 🤖 Suporte a múltiplos provedores de IA (Gemini grátis com múltiplas chaves + fallback automático para Anthropic)
- 📦 Exportação em arquivo ZIP
- 🔄 Interface em tempo real com logs de progresso
- 🧹 Limpeza automática de arquivos temporários
- 🛡️ Correção automática de problemas de scroll para visualização offline
- ⚡ Suporte para sites modernos (Next.js, Gatsby, Nuxt, etc.)

## 🚀 Deploy em Produção

Veja o arquivo [DEPLOY.md](DEPLOY.md) para instruções completas de deploy no Render, Railway, ou outros serviços.


## 🛠️ Desenvolvimento Local

### Requisitos
- Python 3.11+
- uv (gerenciador de pacotes Python)

### Instalação

```bash
# Instalar dependências
uv sync

# Instalar Playwright browsers
uv run playwright install chromium

# Rodar aplicação
uv run python app.py
```

Acesse: `http://localhost:5001`

## 📁 Estrutura do Projeto

```
.
├── app.py              # Aplicação Flask (API + SSE)
├── downloader.py       # Lógica de download e processamento
├── templates/
│   └── index.html      # Interface do usuário
├── downloads/          # Arquivos temporários (auto-limpa)
└── requirements.txt    # Dependências Python
```

## 🔧 Como Funciona

1. **Captura**: Usa Playwright para renderizar a página e capturar recursos de rede
2. **Processamento**: BeautifulSoup processa HTML e reescreve URLs para assets locais
3. **Otimização**: Remove scripts de framework que não funcionam offline
4. **Correção**: Injeta CSS para corrigir problemas de scroll e visibilidade
5. **Empacotamento**: Cria um arquivo ZIP com tudo

## 📝 Notas Técnicas

- **Smooth Scroll Libraries**: Detecta e remove Lenis, Locomotive Scroll, etc.
- **SPAs**: Remove scripts de hydration de Next.js, Gatsby, Nuxt
- **Iframes**: Extrai conteúdo de iframes (comum em site builders como Aura)
- **Lazy Loading**: Rola a página para carregar imagens lazy-loaded

## 📄 Licença

Uso pessoal e educacional.

## 🤝 Contribuições

Sugestões e melhorias são bem-vindas!
