import os
import re
import html
import shutil
import hashlib
import requests
import urllib3
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import mimetypes

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

BROWSER_LAUNCH_ARGS = [
    '--disable-dev-shm-usage',  # Overcome limited resource problems
    '--no-sandbox',  # Required for Docker
    '--disable-setuid-sandbox',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-translate',
    '--metrics-recording-only',
    '--mute-audio',
    '--no-first-run',
    '--safebrowsing-disable-auto-update',
]

ASSET_TYPE_DIRS = ('css', 'js', 'fonts', 'img', 'misc')

IGNORED_LINK_EXTENSIONS = (
    '.pdf', '.zip', '.rar', '.7z', '.jpg', '.jpeg', '.png', '.gif', '.svg',
    '.webp', '.ico', '.mp4', '.webm', '.mp3', '.wav', '.doc', '.docx',
    '.xls', '.xlsx', '.ppt', '.pptx', '.exe', '.dmg',
)


def _normalize_domain(netloc):
    netloc = netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    return netloc


def extract_iframe_content(page, log_fn):
    """
    Check if the page content is inside an iframe (common in site builders like Aura, Webflow, etc.)
    and extract the actual content if found.
    Returns (content, is_iframe, base_url_override).
    """
    # Check for srcdoc iframes (content embedded in attribute)
    srcdoc_iframe = page.query_selector('iframe[srcdoc]')
    if srcdoc_iframe:
        log_fn("🔍 Detectado iframe com srcdoc - extraindo conteúdo real...")
        srcdoc = srcdoc_iframe.get_attribute('srcdoc')
        if srcdoc:
            decoded_content = html.unescape(srcdoc)
            return decoded_content, True, None

    # Check for preview frames (common in site builders)
    preview_selectors = [
        'iframe[class*="preview"]',
        'iframe[class*="site-frame"]',
        'iframe[class*="canvas"]',
        'iframe[id*="preview"]',
        '#preview-iframe',
        '.preview-frame iframe',
        '[role="tabpanel"] iframe',  # Aura-style tab panels
        '[data-testid*="preview"] iframe',
    ]

    for selector in preview_selectors:
        iframe = page.query_selector(selector)
        if iframe:
            frames = page.frames
            for frame in frames:
                if frame != page.main_frame and frame.url and frame.url != 'about:blank':
                    try:
                        log_fn(f"🔍 Detectado iframe de preview - extraindo de {frame.url[:50]}...")
                        content = frame.content()
                        if len(content) > 500:  # Has substantial content
                            return content, True, frame.url
                    except:
                        pass

    # Check all frames including those with srcdoc (about:srcdoc URL)
    for frame in page.frames:
        if frame != page.main_frame:
            try:
                frame_url = frame.url
                if frame_url == 'about:srcdoc':
                    content = frame.content()
                    if len(content) > 1000:  # Substantial content
                        log_fn("🔍 Detectado iframe srcdoc via frame - extraindo conteúdo...")
                        return content, True, None
            except:
                pass

    # Check if main content is suspiciously small (might be a wrapper)
    main_content = page.content()
    body = page.query_selector('body')
    if body:
        direct_children = page.query_selector_all('body > *')
        iframes = page.query_selector_all('iframe')

        if len(direct_children) <= 5 and len(iframes) > 0:
            for frame in page.frames:
                if frame != page.main_frame:
                    try:
                        content = frame.content()
                        if len(content) > len(main_content) * 0.3:
                            log_fn("🔍 Detectado wrapper com iframe - usando conteúdo do frame...")
                            base_override = frame.url if frame.url and frame.url not in ('about:blank', 'about:srcdoc') else None
                            return content, True, base_override
                    except:
                        pass

    return None, False, None


EMAIL_INPUT_SELECTORS = (
    'input[type="email"], input[autocomplete="username"], '
    'input[name*="email" i], input[id*="email" i], '
    'input[name*="user" i], input[id*="user" i], '
    'input[name*="login" i], input[id*="login" i]'
)


def perform_login(page, login_url, username, password, log_fn, timeout_ms=30000):
    """
    Faz login num formulário padrão (campo de usuário/e-mail + senha) antes de baixar as
    páginas. Retorna True/False indicando sucesso (best-effort). Nunca loga usuário/senha.
    """
    log_fn(f"🔐 Fazendo login em {login_url}...")
    try:
        page.goto(login_url, wait_until='load', timeout=timeout_ms)
        page.wait_for_timeout(1500)
    except Exception as e:
        log_fn(f"⚠️ Aviso ao carregar página de login: {str(e)[:100]}")

    password_input = page.locator('input[type="password"]').first
    if password_input.count() == 0:
        log_fn("⚠️ Campo de senha não encontrado na página de login — pulando login")
        return False

    email_input = page.locator(EMAIL_INPUT_SELECTORS).first
    if email_input.count() == 0:
        log_fn("⚠️ Campo de usuário/e-mail não encontrado na página de login — pulando login")
        return False

    try:
        email_input.fill(username, timeout=timeout_ms)
        password_input.fill(password, timeout=timeout_ms)
    except Exception as e:
        log_fn(f"⚠️ Erro ao preencher credenciais: {str(e)[:100]}")
        return False

    submitted = False
    try:
        form = password_input.locator('xpath=ancestor::form[1]')
        if form.count() > 0:
            submit_btn = form.locator('button[type="submit"], input[type="submit"], button:not([type])').first
            if submit_btn.count() > 0:
                submit_btn.click(timeout=timeout_ms)
                submitted = True
    except Exception:
        pass

    if not submitted:
        try:
            password_input.press('Enter')
            submitted = True
        except Exception:
            pass

    if not submitted:
        log_fn("⚠️ Não foi possível submeter o formulário de login")
        return False

    try:
        page.wait_for_load_state('networkidle', timeout=timeout_ms)
    except Exception:
        page.wait_for_timeout(3000)

    if page.url.rstrip('/') == login_url.rstrip('/'):
        log_fn("⚠️ Login pode não ter funcionado (a página permaneceu na tela de login)")
        return False

    log_fn("✅ Login realizado com sucesso")
    return True


def _page_norm(u):
    parsed = urlparse(u)
    return f"{_normalize_domain(parsed.netloc)}{(parsed.path.rstrip('/') or '/')}"


def _crawl_links(url, timeout_ms, login=None, login_log_fn=None):
    """Abre um browser novo, opcionalmente faz login, navega até `url` e extrai os
    <a href> encontrados. Retorna (effective_url, raw_links, login_ok)."""
    login_ok = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1920, 'height': 1080},
        )
        page = context.new_page()
        try:
            if login:
                login_ok = perform_login(page, login['login_url'], login['username'], login['password'], login_log_fn or (lambda m: None), timeout_ms)

            try:
                page.goto(url, wait_until='load', timeout=timeout_ms)
                page.wait_for_timeout(1500)
            except Exception:
                pass  # segue mesmo com timeout parcial, tenta extrair o que carregou

            iframe_content, is_iframe, base_override = extract_iframe_content(page, lambda m: None)
            effective_url = base_override or page.url

            raw_links = page.eval_on_selector_all(
                'a[href]',
                "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim().slice(0,60)}))"
            )
            return effective_url, raw_links, login_ok
        finally:
            browser.close()


def discover_pages(url, max_pages=40, timeout_ms=30000, login=None):
    """Renderiza a home com Playwright e retorna os links internos (mesmo domínio).

    Se `login` for informado, faz DUAS varreduras — uma pública (sem login) e uma
    autenticada — e une os links das duas. Isso evita que páginas públicas (ex.: home de
    marketing, páginas que somem do menu autenticado) fiquem ocultas só porque o site
    redireciona para uma área logada depois do login.

    Retorna (pages, truncated, login_ok, login_logs) — login_ok é None se nenhum login
    foi solicitado; login_logs contém as mensagens do processo de login (sem credenciais)."""
    login_ok = None
    login_logs = []

    effective_url_public, raw_links, _ = _crawl_links(url, timeout_ms)
    home_netloc = _normalize_domain(urlparse(url).netloc)
    home_norms = {_page_norm(url), _page_norm(effective_url_public)}

    if login:
        effective_url_auth, raw_links_auth, login_ok = _crawl_links(url, timeout_ms, login=login, login_log_fn=login_logs.append)
        raw_links = raw_links + raw_links_auth
        home_norms.add(_page_norm(effective_url_auth))

    seen = set()
    pages = []
    for link in raw_links:
        href = (link.get('href') or '').strip()
        if not href or href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue

        parsed = urlparse(href)
        if parsed.scheme not in ('http', 'https'):
            continue

        if _normalize_domain(parsed.netloc) != home_netloc:
            continue

        ext = os.path.splitext(parsed.path)[1].lower()
        if ext in IGNORED_LINK_EXTENSIONS:
            continue

        norm = _page_norm(href)
        if norm in home_norms or norm in seen:
            continue
        seen.add(norm)

        clean_url = href.split('#')[0]
        label = (link.get('text') or '').strip() or (parsed.path or clean_url)
        pages.append({'url': clean_url, 'path': parsed.path or '/', 'label': label})

    truncated = len(pages) > max_pages
    return pages[:max_pages], truncated, login_ok, login_logs


class WebsiteDownloader:
    def __init__(self, url, output_dir, log_callback=None, typed_assets=False):
        self.url = url
        self.output_dir = output_dir
        self.typed_assets = typed_assets
        self.assets_dir = os.path.join(output_dir, 'assets')
        self.resource_cache = {}  # url -> local_path
        self.network_resources = {}  # url -> {'body': bytes, 'content_type': str}
        self.base_url = url
        self.session = None  # Will be set with cookies from browser
        self.log_callback = log_callback or (lambda msg: print(msg))
        self._home_html = None
        self._page_htmls = []  # [(page_url, html_transformado), ...] — todas as páginas baixadas

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        if typed_assets:
            os.makedirs(output_dir)
            for sub in ASSET_TYPE_DIRS:
                os.makedirs(os.path.join(output_dir, sub), exist_ok=True)
        else:
            os.makedirs(self.assets_dir)

    def log(self, message):
        """Send log message to callback"""
        self.log_callback(message)

    def get_home_html(self):
        """HTML da home JÁ transformado (assets reescritos para os paths locais reais),
        usado como referência para gerar o Design System via IA"""
        return self._home_html

    def get_all_pages_html(self):
        """Lista (page_url, html_transformado) de TODAS as páginas baixadas nesta execução
        (não só a home) — usada para a geração do Design System via IA, para capturar
        botões/formulários/componentes que só existem em páginas selecionadas além da home."""
        return list(self._page_htmls)

    def get_asset_manifest(self):
        """Lista (deduplicada, ordenada) dos assets realmente salvos localmente nesta execução"""
        return sorted(set(self.resource_cache.values()))

    def get_captured_css(self):
        """Lista de corpos de CSS capturados (deduplicados), usados para gerar o Design System via IA"""
        seen = set()
        bodies = []
        for res_url, res in self.network_resources.items():
            content_type = (res.get('content_type') or '').split(';')[0].strip().lower()
            if content_type == 'text/css' and res_url not in seen:
                seen.add(res_url)
                try:
                    bodies.append(res['body'].decode('utf-8', errors='ignore'))
                except Exception:
                    pass
        return bodies

    def _asset_prefixes(self):
        if self.typed_assets:
            return tuple(f'{d}/' for d in ASSET_TYPE_DIRS)
        return ('assets/',)

    def _assign(self, rel_path, page_dir):
        """Converte um path de asset relativo à raiz do output_dir em relativo à pasta da página atual"""
        if not rel_path or not isinstance(rel_path, str):
            return rel_path
        if page_dir in ('.', '', None):
            return rel_path
        if not rel_path.startswith(self._asset_prefixes()):
            return rel_path
        return os.path.relpath(rel_path, start=page_dir).replace('\\', '/')

    def _asset_subdir_for(self, content_type, url):
        """Decide em qual subpasta tipada (css/js/fonts/img/misc) um asset deve ser salvo"""
        ct = (content_type or '').split(';')[0].strip().lower()
        ext = os.path.splitext(urlparse(url).path)[1].lower()

        if ct == 'text/css' or ext == '.css':
            return 'css'
        if ct in ('application/javascript', 'text/javascript', 'application/x-javascript') or ext == '.js':
            return 'js'
        if (ct.startswith('font/') or
                ct in ('application/font-woff', 'application/font-woff2', 'application/x-font-ttf', 'application/vnd.ms-fontobject') or
                ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot')):
            return 'fonts'
        if (ct.startswith(('image/', 'video/', 'audio/')) or
                ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.mp4', '.webm', '.mp3', '.wav', '.ogg')):
            return 'img'
        return 'misc'

    def _get_extension(self, url, content_type=''):
        """Get file extension from URL or content-type"""
        parsed = urlparse(url)
        path = parsed.path
        _, ext = os.path.splitext(path)

        if ext and len(ext) <= 6:
            return ext

        if content_type:
            mime = content_type.split(';')[0].strip()
            guessed = mimetypes.guess_extension(mime)
            if guessed:
                return guessed

        return ''

    def _generate_filename(self, url, content_type=''):
        """Generate a unique filename for a resource"""
        ext = self._get_extension(url, content_type)
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]

        parsed = urlparse(url)
        name = os.path.basename(parsed.path)
        if name:
            name = re.sub(r'[^a-zA-Z0-9_-]', '_', name.split('.')[0])[:30]
        else:
            name = 'resource'

        return f"{name}_{url_hash}{ext}"

    def _save_resource(self, url, content, content_type=''):
        """Save a resource to disk and return relative path"""
        if url in self.resource_cache:
            return self.resource_cache[url]

        if not content:
            return None

        filename = self._generate_filename(url, content_type)

        if self.typed_assets:
            subdir = self._asset_subdir_for(content_type, url)
            target_dir = os.path.join(self.output_dir, subdir)
        else:
            subdir = 'assets'
            target_dir = self.assets_dir

        filepath = os.path.join(target_dir, filename)

        with open(filepath, 'wb') as f:
            f.write(content if isinstance(content, bytes) else content.encode('utf-8'))

        rel_path = f"{subdir}/{filename}"
        self.resource_cache[url] = rel_path
        return rel_path

    def _download_fallback(self, url):
        """Download a resource that wasn't captured during page load"""
        if url in self.resource_cache:
            return self.resource_cache[url]

        if not url or url.startswith('data:') or url.startswith('blob:') or url.startswith('#'):
            return url

        try:
            response = self.session.get(url, timeout=15, verify=False)
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                local_path = self._save_resource(url, response.content, content_type)
                return local_path
        except Exception as e:
            pass  # Silent fail for fallback

        return None

    def _get_resource(self, url, base=None):
        """Get a resource - from cache, network capture, or fallback download"""
        if not url or url.startswith('data:') or url.startswith('blob:') or url.startswith('#'):
            return url

        # Make absolute URL
        abs_url = urljoin(base or self.base_url, url)

        # Check cache first
        if abs_url in self.resource_cache:
            return self.resource_cache[abs_url]

        # Check network captures
        if abs_url in self.network_resources:
            res = self.network_resources[abs_url]
            return self._save_resource(abs_url, res['body'], res.get('content_type', ''))

        # Fallback download
        local_path = self._download_fallback(abs_url)
        if local_path:
            return local_path

        # Return original if all fails
        return url

    def _rewrite_css_urls(self, css_content, css_url, local_ref_dir='.'):
        """Rewrite all url() references in CSS content"""
        def replacer(match):
            full_match = match.group(0)
            url_content = match.group(1).strip()

            # Remove quotes if present
            if url_content.startswith(("'", '"')) and url_content.endswith(("'", '"')):
                url_content = url_content[1:-1]

            if not url_content or url_content.startswith('data:'):
                return full_match

            # Make absolute URL relative to CSS file
            abs_url = urljoin(css_url, url_content)
            local_path = self._get_resource(abs_url)

            if local_path and local_path.startswith(self._asset_prefixes()):
                if self.typed_assets:
                    rel = os.path.relpath(local_path, start=local_ref_dir).replace('\\', '/')
                else:
                    # CSS is in assets/, so reference sibling files directly
                    rel = os.path.basename(local_path)
                return f'url("{rel}")'

            return full_match

        return re.sub(r'url\(\s*([^)]+)\s*\)', replacer, css_content)

    def _detect_nextjs(self, soup):
        """Detect if page is built with Next.js even without #__next"""
        # Check for Next.js data script
        for script in soup.find_all('script'):
            script_id = script.get('id', '')
            script_text = script.string or ''
            if '__NEXT_DATA__' in script_id or '__NEXT_DATA__' in script_text:
                return True
            if 'self.__next' in script_text:
                return True

        # Check for Next.js script patterns in src
        for script in soup.find_all('script', src=True):
            src = script['src']
            if '_next/' in src or 'webpack' in src.lower():
                return True

        # Check for Next.js link patterns
        for link in soup.find_all('link'):
            href = link.get('href', '')
            if '_next/' in href:
                return True

        return False

    def _fix_scroll_blocking(self, soup):
        """Fix CSS and HTML issues that block scrolling in offline viewing"""
        self.log("🔧 Corrigindo problemas de scroll para visualização offline...")

        # 1. Fix html element
        html_elem = soup.find('html')
        if html_elem:
            html_classes = html_elem.get('class', [])
            if isinstance(html_classes, str):
                html_classes = html_classes.split()

            # Remove Lenis-specific classes that block scroll
            lenis_classes = ['lenis', 'lenis-smooth', 'lenis-scrolling', 'lenis-stopped',
                           'has-scroll-smooth', 'has-scroll-init', 'locomotive-scroll']
            new_classes = [c for c in html_classes if c.lower() not in [lc.lower() for lc in lenis_classes]]
            if new_classes != html_classes:
                html_elem['class'] = new_classes
                self.log("   ✅ Removidas classes Lenis/Locomotive do html")

        # 2. Fix body element
        body = soup.find('body')
        if body:
            body_classes = body.get('class', [])
            if isinstance(body_classes, str):
                body_classes = body_classes.split()

            # Remove scroll-blocking classes
            blocking_classes = ['overflow-hidden', 'no-scroll', 'scroll-lock', 'fixed',
                              'lenis', 'lenis-smooth', 'has-scroll-smooth']
            new_classes = [c for c in body_classes if c.lower() not in [bc.lower() for bc in blocking_classes]]

            # Fix flex centering that cuts off content
            if 'items-center' in new_classes and 'flex' in new_classes:
                new_classes = [c if c != 'items-center' else 'items-start' for c in new_classes]
                self.log("   ✅ Corrigida centralização vertical do body")

            if new_classes != body_classes:
                body['class'] = new_classes

        # 3. Fix main containers that might have height: 100vh with overflow hidden
        for elem in soup.find_all(class_=lambda c: c and any(
            x in str(c).lower() for x in ['scroll-container', 'smooth-scroll', 'lenis', 'locomotive']
        )):
            # Remove data attributes that control smooth scroll
            for attr in list(elem.attrs.keys()):
                if 'scroll' in attr.lower() or 'lenis' in attr.lower():
                    del elem[attr]

        # 4. Remove/fix inline styles that block scroll
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'overflow' in style.lower() and 'hidden' in style.lower():
                # Remove overflow: hidden from inline styles
                new_style = re.sub(r'overflow\s*:\s*hidden\s*;?', '', style, flags=re.IGNORECASE)
                elem['style'] = new_style.strip()

        # 5. Inject CSS overrides to ensure scrolling works
        scroll_fix_css = """
        /* Scroll fixes for offline viewing */
        html, body {
            overflow: auto !important;
            overflow-x: hidden !important;
            height: auto !important;
            min-height: 100% !important;
            scroll-behavior: auto !important;
            opacity: 1 !important;
            visibility: visible !important;
        }

        /* Force visibility - many sites use JS animations for initial display */
        body, .wrapper, main, #__next, #app, .page, .content {
            opacity: 1 !important;
            visibility: visible !important;
            transform: none !important;
        }

        /* Disable loader/preloader overlays */
        .loader, .preloader, .loading, [class*="loader"], [class*="preloader"] {
            display: none !important;
            opacity: 0 !important;
        }

        /* Show elements that might be hidden for animation */
        .word-inner, .char, .line, [data-aos], [data-scroll], [data-cue],
        [data-sal], [data-animate], [data-animation], [data-wow],
        .hero-text span, .hero-fade, [class*="hero"] span,
        .aos-init, .aos-animate, [class*="animate"], [class*="motion"],
        [class*="fadeIn"], [class*="slideIn"], [class*="reveal"] {
            opacity: 1 !important;
            transform: none !important;
            visibility: visible !important;
        }

        /* Reset Tailwind animation utility classes */
        .translate-y-full, .translate-x-full, .-translate-y-full, .-translate-x-full,
        .translate-y-1\/2, .-translate-y-1\/2, .translate-y-\[100\%\], .translate-y-\[110\%\] {
            transform: none !important;
        }

        /* Force visibility on common hidden-for-animation patterns */
        .opacity-0, [class*="opacity-0"] {
            opacity: 1 !important;
        }

        /* Reset scale transforms used for animations */
        .scale-0, .scale-50, .scale-75 {
            transform: none !important;
        }

        html.lenis, html.lenis-smooth,
        body.lenis, body.lenis-smooth,
        .lenis-wrapper, .lenis-content,
        [data-lenis-prevent], [data-scroll-container] {
            overflow: visible !important;
            height: auto !important;
        }

        /* Fix flex containers that might cut off content */
        body.flex.items-center,
        body.flex.justify-center {
            align-items: flex-start !important;
            min-height: 100vh;
            height: auto !important;
        }

        /* Ensure main content scrolls */
        main, #__next, #__nuxt, #app, #root, .main-content {
            overflow: visible !important;
            height: auto !important;
        }
        """

        # Add the fix CSS as a style tag at the end of head
        head = soup.find('head')
        if head:
            fix_style = soup.new_tag('style')
            fix_style['data-scroll-fix'] = 'true'
            fix_style.string = scroll_fix_css
            head.append(fix_style)
            self.log("   ✅ Injetado CSS para corrigir scroll")

        # 6. Remove Lenis/Locomotive script tags that might interfere
        scripts_removed = 0
        for script in soup.find_all('script'):
            src = script.get('src', '') or ''
            script_text = script.string or ''

            # Check for smooth scroll libraries
            if any(x in src.lower() for x in ['lenis', 'locomotive', 'smooth-scroll']):
                script.decompose()
                scripts_removed += 1
            elif any(x in script_text.lower() for x in ['new lenis', 'new locomotivescroll', 'smoothscroll']):
                script.decompose()
                scripts_removed += 1

        if scripts_removed > 0:
            self.log(f"   ✅ Removidos {scripts_removed} scripts de smooth scroll")

    def _process_srcset(self, srcset, base=None, page_dir='.'):
        """Process a srcset attribute and return the rewritten version"""
        if not srcset:
            return srcset

        new_parts = []
        parts = srcset.split(',')

        for part in parts:
            part = part.strip()
            if not part:
                continue

            tokens = part.split()
            if not tokens:
                continue

            url = tokens[0]
            descriptor = ' '.join(tokens[1:]) if len(tokens) > 1 else ''

            if url.startswith('data:'):
                new_parts.append(part)
                continue

            local_path = self._get_resource(url, base)
            if local_path and local_path != url:
                local_path = self._assign(local_path, page_dir)
                new_parts.append(f"{local_path} {descriptor}".strip())
            else:
                new_parts.append(part)

        return ', '.join(new_parts) if new_parts else srcset

    def _slugify_page_url(self, page_url):
        """Deriva a pasta de destino de uma página a partir do path da URL. Home -> '.'"""
        parsed = urlparse(page_url)
        path = parsed.path.strip('/')
        if not path:
            return '.'
        segments = [re.sub(r'[^a-zA-Z0-9_-]', '_', seg) for seg in path.split('/') if seg]
        if not segments:
            return '.'
        return '/'.join(segments)

    def _normalize_url(self, url):
        parsed = urlparse(url)
        netloc = _normalize_domain(parsed.netloc)
        path = parsed.path.rstrip('/') or '/'
        query = f"?{parsed.query}" if parsed.query else ''
        return f"{netloc}{path}{query}"

    def _load_page(self, page, url):
        """Navega até a URL, trata iframe wrapper e scroll de lazy-load. Retorna (html_content, effective_url)"""
        self.log(f"🌐 Carregando {url}...")
        try:
            page.goto(url, wait_until='load', timeout=60000)
            self.log("✓ Página carregada (load)")
            page.wait_for_timeout(3000)
            self.log("✓ Recursos adicionais carregados")
        except Exception as e:
            self.log(f"⚠️ Aviso de carregamento: {str(e)[:100]}")
            self.log("⚠️ Tentando continuar mesmo assim...")

        effective_url = page.url
        page.wait_for_timeout(2000)

        iframe_content, is_iframe, base_override = extract_iframe_content(page, self.log)
        if base_override:
            effective_url = base_override

        if not is_iframe:
            self.log("📜 Rolando página para carregar conteúdo lazy...")
            self._scroll_page(page)
            page.wait_for_timeout(3000)

        if is_iframe and iframe_content:
            html_content = iframe_content
            self.log("✨ Usando conteúdo extraído do iframe")
        else:
            html_content = page.content()

        return html_content, effective_url

    def _transform_page(self, html_content, page_dir):
        """Processa o HTML de uma página (BeautifulSoup): assets, scroll-fix, SPA cleanup. Retorna o soup."""
        soup = BeautifulSoup(html_content, 'html.parser')

        # Fix scroll-blocking issues for offline viewing
        self._fix_scroll_blocking(soup)

        # Remove any remaining iframes that are wrappers (like Aura preview frames)
        for iframe in soup.find_all('iframe'):
            srcdoc = iframe.get('srcdoc', '')
            if srcdoc or 'preview' in str(iframe.get('class', '')).lower():
                iframe.decompose()

        # 1. Process external stylesheets
        self.log("🎨 Processando stylesheets...")
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href')
            if not href or href.startswith('data:'):
                continue

            abs_url = urljoin(self.base_url, href)

            css_content = None
            if abs_url in self.network_resources:
                try:
                    css_content = self.network_resources[abs_url]['body'].decode('utf-8', errors='ignore')
                except:
                    pass

            if not css_content:
                try:
                    response = self.session.get(abs_url, timeout=15, verify=False)
                    if response.status_code == 200:
                        css_content = response.text
                except:
                    pass

            if css_content:
                css_local_ref_dir = 'css' if self.typed_assets else '.'
                css_content = self._rewrite_css_urls(css_content, abs_url, local_ref_dir=css_local_ref_dir)
                local_path = self._save_resource(abs_url, css_content.encode('utf-8'), 'text/css')
                if local_path:
                    link['href'] = self._assign(local_path, page_dir)
            # Remove crossorigin/integrity — local files don't have CORS headers,
            # so these attributes silently block the stylesheet in Chrome
            for attr in ['crossorigin', 'integrity']:
                if link.has_attr(attr):
                    del link[attr]

        # 2. Process inline <style> tags
        self.log("✨ Processando estilos inline...")
        for style_tag in soup.find_all('style'):
            if style_tag.string:
                style_tag.string = self._rewrite_css_urls(style_tag.string, self.base_url, local_ref_dir=page_dir)

        # 3. Process scripts
        self.log("📝 Processando scripts...")
        for script in soup.find_all('script', src=True):
            src = script.get('src')
            if not src or src.startswith('data:'):
                continue

            local_path = self._get_resource(src)
            if local_path and local_path != src:
                script['src'] = self._assign(local_path, page_dir)
                for attr in ['integrity', 'crossorigin', 'nonce']:
                    if script.has_attr(attr):
                        del script[attr]

        # 4. Process all image-related elements
        self.log("🖼️ Processando imagens...")
        for elem in soup.find_all(['img', 'source', 'video', 'audio', 'picture', 'input']):
            src = elem.get('src')

            # Check lazy loading attributes first
            for attr in ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-image', 'data-bg']:
                if elem.get(attr):
                    lazy_src = elem[attr]
                    local_path = self._get_resource(lazy_src)
                    if local_path and local_path != lazy_src:
                        elem['src'] = self._assign(local_path, page_dir)
                        del elem[attr]
                        src = None  # Already handled
                    break

            if src and not src.startswith('data:'):
                local_path = self._get_resource(src)
                if local_path and local_path != src:
                    elem['src'] = self._assign(local_path, page_dir)

            srcset = elem.get('srcset')
            if srcset:
                elem['srcset'] = self._process_srcset(srcset, page_dir=page_dir)

            data_srcset = elem.get('data-srcset')
            if data_srcset:
                elem['data-srcset'] = self._process_srcset(data_srcset, page_dir=page_dir)

            if elem.name == 'video' and elem.get('poster'):
                poster = elem['poster']
                local_path = self._get_resource(poster)
                if local_path and local_path != poster:
                    elem['poster'] = self._assign(local_path, page_dir)

        # 5. Process inline style attributes
        self.log("🔗 Processando atributos de estilo inline...")
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'url(' in style:
                elem['style'] = self._rewrite_css_urls(style, self.base_url, local_ref_dir=page_dir)

        # 6. Process favicons and other link tags with URLs
        for link in soup.find_all('link'):
            if link.get('href') and link.get('rel'):
                rel = link['rel']
                if isinstance(rel, list):
                    rel = ' '.join(rel)
                if 'icon' in rel.lower() or 'apple-touch' in rel.lower() or 'manifest' in rel.lower():
                    href = link['href']
                    if not href.startswith('data:'):
                        local_path = self._get_resource(href)
                        if local_path and local_path != href:
                            link['href'] = self._assign(local_path, page_dir)

        # 7. Process meta tags with image URLs (og:image, etc.)
        for meta in soup.find_all('meta', attrs={'content': True}):
            prop = meta.get('property', '') or meta.get('name', '')
            if 'image' in prop.lower():
                content = meta['content']
                if content and not content.startswith('data:') and ('http' in content or content.startswith('/')):
                    local_path = self._get_resource(content)
                    if local_path and local_path != content:
                        meta['content'] = self._assign(local_path, page_dir)

        # 8. Process background images in divs and other elements
        for elem in soup.find_all(attrs={'data-background': True}):
            bg = elem['data-background']
            if bg and not bg.startswith('data:'):
                local_path = self._get_resource(bg)
                if local_path and local_path != bg:
                    elem['data-background'] = self._assign(local_path, page_dir)

        # 9. Handle SPA frameworks (Gatsby, Next.js, Nuxt, React/Vite, etc.)
        # These frameworks use client-side routing that doesn't work offline
        # The HTML is already server-rendered, so we remove ALL framework scripts
        is_gatsby = soup.find(id='___gatsby') is not None
        is_nextjs = soup.find(id='__next') is not None or self._detect_nextjs(soup)
        is_nuxt = soup.find(id='__nuxt') is not None

        root_elem = soup.find(id='root')
        has_module_script = any(
            s.get('type') == 'module' and s.get('src', '')
            for s in soup.find_all('script')
        )
        is_react_vite = (
            root_elem is not None and
            has_module_script and
            not is_nextjs and not is_gatsby and not is_nuxt
        )

        if is_gatsby or is_nextjs or is_nuxt or is_react_vite:
            if is_gatsby:
                framework = 'Gatsby'
            elif is_nextjs:
                framework = 'Next.js'
            elif is_nuxt:
                framework = 'Nuxt'
            else:
                framework = 'React/Vite'
            self.log(f"⚛️ Detectado {framework} - removendo scripts do framework (HTML já capturado pelo Playwright)...")

            scripts_removed = 0
            safe_keywords = ['google', 'analytics', 'gtm', 'gtag', 'facebook', 'pixel',
                           'elfsight', 'hubspot', 'intercom', 'crisp', 'drift', 'hotjar',
                           'clarity', 'segment', 'mixpanel', 'amplitude', 'adobe', 'privacy']

            for script in soup.find_all('script'):
                src = script.get('src', '')
                script_text = script.string or ''

                is_safe = any(safe in src.lower() for safe in safe_keywords)

                if not is_safe:
                    should_remove = False

                    if is_gatsby and ('framework-' in src or 'app-' in src or
                                     'commons-' in src or 'component-' in src or
                                     'webpack-runtime' in src or 'polyfill' in src):
                        should_remove = True

                    if is_nextjs:
                        if src and not src.startswith(('http://', 'https://', '//')):
                            should_remove = True
                        if '_next/' in src or 'webpack' in src or 'polyfill' in src:
                            should_remove = True
                        if '__next' in script_text or 'self.__next' in script_text:
                            should_remove = True
                        if '-' in src and src.endswith('.js') and 'assets/' in src:
                            should_remove = True

                    if is_nuxt and ('_nuxt/' in src or '__NUXT__' in script_text or
                                   'nuxt' in src.lower()):
                        should_remove = True

                    if is_react_vite:
                        script_type = script.get('type', '')
                        if script_type == 'module' and src and not src.startswith(('http://', 'https://', '//')):
                            should_remove = True
                        if any(x in script_text for x in ['__vite__', 'import.meta', 'ReactDOM', '__react']):
                            should_remove = True

                    if ('hydrate' in script_text.lower() or
                        'window.__' in script_text or
                        'GATSBY' in script_text or
                        'pageData' in script_text or
                        'self.__next' in script_text or
                        '__NEXT_DATA__' in script_text):
                        should_remove = True

                    if should_remove:
                        script.decompose()
                        scripts_removed += 1

            links_removed = 0
            for link in soup.find_all('link', rel=lambda r: r and any(x in r for x in ['preload', 'prefetch', 'modulepreload'])):
                href = link.get('href', '')
                should_remove_link = False
                if '_next/' in href or (href.startswith('assets/') and '-' in href):
                    should_remove_link = True
                if is_react_vite:
                    rel = link.get('rel', [])
                    if isinstance(rel, list):
                        rel_str = ' '.join(rel)
                    else:
                        rel_str = rel
                    if 'modulepreload' in rel_str:
                        should_remove_link = True
                if should_remove_link:
                    link.decompose()
                    links_removed += 1

            self.log(f"   ✅ Removidos {scripts_removed} scripts e {links_removed} preloads do framework")

        return soup

    def _rewrite_nav_links(self, soup, page_dir, base_url, page_urls, multi_page):
        """Reescreve <a href> para páginas baixadas localmente; demais links internos viram '#' (como hoje)"""
        for a in soup.find_all('a', href=True):
            href = a['href']

            if multi_page and href and not href.startswith(('#', 'mailto:', 'tel:', 'javascript:', 'data:')):
                abs_url = urljoin(base_url, href)
                parsed = urlparse(abs_url)
                norm = self._normalize_url(abs_url)
                if norm in page_urls:
                    target_dir = page_urls[norm]
                    target_rel = 'index.html' if target_dir == '.' else f'{target_dir}/index.html'
                    rel = target_rel if page_dir == '.' else os.path.relpath(target_rel, start=page_dir).replace('\\', '/')
                    if parsed.fragment:
                        rel = f"{rel}#{parsed.fragment}"
                    a['href'] = rel
                    continue

            # Convert root links to stay on page (comportamento original preservado)
            if href == '/':
                a['href'] = '#'
            elif href.startswith('/') and not href.startswith('//'):
                a['href'] = '#'

    def process(self, login=None):
        """Baixa apenas a home (comportamento simples/original)"""
        return self.process_multi([self.url], login=login)

    def process_multi(self, pages, login=None):
        """Baixa uma ou mais páginas do mesmo site, ligando a navegação entre as que foram baixadas.

        Se `login` for informado (dict com login_url/username/password), faz login antes
        de baixar qualquer página, reaproveitando a mesma sessão/cookies do browser.
        """
        multi_page = len(pages) > 1

        page_dirs = {}
        page_urls = {}
        for p_url in pages:
            # Com uma única página, o resultado vai sempre para a raiz (index.html),
            # independente do path da URL — preserva o comportamento do modo simples original.
            d = self._slugify_page_url(p_url) if multi_page else '.'
            page_dirs[p_url] = d
            page_urls[self._normalize_url(p_url)] = d

        page_soups = {}
        page_base_urls = {}

        with sync_playwright() as p:
            self.log("🚀 Iniciando navegador...")
            browser = p.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)

            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
            )

            page = context.new_page()

            def capture_response(response):
                try:
                    url = response.url
                    if response.status == 200 and not url.startswith(('data:', 'blob:')):
                        try:
                            body = response.body()
                            resource_data = {
                                'body': body,
                                'content_type': response.headers.get('content-type', '')
                            }
                            self.network_resources[url] = resource_data
                            request_url = response.request.url
                            if request_url != url:
                                self.network_resources[request_url] = resource_data
                        except:
                            pass
                except:
                    pass

            page.on("response", capture_response)

            login_page_norm = self._normalize_url(login['login_url']) if login else None
            login_page_in_selection = login_page_norm is not None and any(
                self._normalize_url(p_url) == login_page_norm for p_url in pages
            )

            ordered_pages = list(pages)
            did_login = False
            if login_page_in_selection:
                # A própria página de login está na seleção: visita ela PRIMEIRO, antes de
                # logar, pra capturar o formulário real/público — se já estivesse logado, o
                # site normalmente redireciona a página de login pra longe do formulário.
                ordered_pages.sort(key=lambda p_url: 0 if self._normalize_url(p_url) == login_page_norm else 1)
            elif login:
                perform_login(page, login['login_url'], login['username'], login['password'], self.log)
                did_login = True

            for i, page_url in enumerate(ordered_pages, start=1):
                if multi_page:
                    self.log(f"🌐 Baixando página {i}/{len(ordered_pages)}: {page_url}")

                html_content, effective_url = self._load_page(page, page_url)

                page_dir = page_dirs[page_url]
                page_base_urls[page_dir] = effective_url
                page_urls[self._normalize_url(effective_url)] = page_dir
                self.base_url = effective_url

                # Setup/refresh requests session with browser cookies (fallback downloads)
                cookies = context.cookies()
                self.session = requests.Session()
                self.session.headers.update({
                    'User-Agent': USER_AGENT,
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Referer': self.base_url,
                })
                for cookie in cookies:
                    self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))

                self.log(f"📦 Capturados {len(self.network_resources)} recursos de rede")

                self.log("🔧 Processando HTML e assets...")
                soup = self._transform_page(html_content, page_dir)
                page_soups[page_dir] = soup

                # Snapshot pós-transformação (paths de assets já reescritos para os locais reais)
                # de TODAS as páginas, usado como referência para a geração do Design System via IA
                page_html_str = str(soup)
                self._page_htmls.append((page_url, page_html_str))
                if page_url == pages[0]:
                    self._home_html = page_html_str

                # Se essa era a própria página de login (visitada antes de logar), faz o
                # login agora, antes de seguir pras próximas páginas.
                if login_page_in_selection and not did_login and self._normalize_url(page_url) == login_page_norm:
                    perform_login(page, login['login_url'], login['username'], login['password'], self.log)
                    did_login = True

            browser.close()

        self.log("🔗 Ligando páginas baixadas entre si..." if multi_page else "🔗 Corrigindo links de navegação...")
        for page_dir, soup in page_soups.items():
            base_url_for_page = page_base_urls.get(page_dir, self.url)
            self._rewrite_nav_links(soup, page_dir, base_url_for_page, page_urls, multi_page)

        for page_dir, soup in page_soups.items():
            if page_dir == '.':
                out_path = os.path.join(self.output_dir, 'index.html')
            else:
                page_folder = os.path.join(self.output_dir, page_dir)
                os.makedirs(page_folder, exist_ok=True)
                out_path = os.path.join(page_folder, 'index.html')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(str(soup))

        suffix = f", {len(pages)} página(s)" if multi_page else ""
        self.log(f"✅ Concluído! {len(self.resource_cache)} assets salvos{suffix}")
        return True

    def _scroll_page(self, page):
        """Scroll the page to trigger lazy loading"""
        try:
            # First, try to disable smooth scroll libraries (Lenis, Locomotive, etc.)
            page.evaluate("""
                () => {
                    // Disable Lenis smooth scroll
                    if (window.lenis) {
                        try { window.lenis.destroy(); } catch(e) {}
                    }
                    // Disable Locomotive Scroll
                    if (window.locomotiveScroll) {
                        try { window.locomotiveScroll.destroy(); } catch(e) {}
                    }
                    // Reset any scroll-behavior smooth
                    document.documentElement.style.scrollBehavior = 'auto';
                    document.body.style.scrollBehavior = 'auto';

                    // Remove overflow hidden that might prevent scrolling
                    if (getComputedStyle(document.body).overflow === 'hidden') {
                        document.body.style.overflow = 'auto';
                    }
                    if (getComputedStyle(document.documentElement).overflow === 'hidden') {
                        document.documentElement.style.overflow = 'auto';
                    }
                }
            """)

            # Find the actual scroll container (some sites use custom containers)
            scroll_container = page.evaluate("""
                () => {
                    // Check for common scroll container patterns
                    const selectors = [
                        '[data-scroll-container]',
                        '.scroll-container',
                        '.smooth-scroll',
                        'main',
                        '#__next',
                        '#__nuxt',
                        '#app'
                    ];

                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.scrollHeight > window.innerHeight) {
                            return sel;
                        }
                    }
                    return null;
                }
            """)

            if scroll_container:
                self.log(f"🔍 Detectado container de scroll customizado: {scroll_container}")

            total_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            viewport_height = page.evaluate("window.innerHeight")

            # Limit scroll iterations to prevent infinite loops
            max_iterations = 20
            iteration = 0

            current = 0
            while current < total_height and iteration < max_iterations:
                # Scroll using multiple methods for better compatibility
                page.evaluate(f"""
                    (pos) => {{
                        window.scrollTo(0, pos);
                        document.documentElement.scrollTop = pos;
                        document.body.scrollTop = pos;

                        // Also try scrolling custom containers
                        const containers = document.querySelectorAll('[data-scroll-container], .scroll-container, main');
                        containers.forEach(c => {{ c.scrollTop = pos; }});
                    }}
                """, current)

                page.wait_for_timeout(600)
                current += viewport_height
                iteration += 1

                new_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
                if new_height > total_height:
                    total_height = new_height

            # Scroll back to top
            page.evaluate("""
                () => {
                    window.scrollTo(0, 0);
                    document.documentElement.scrollTop = 0;
                    document.body.scrollTop = 0;
                }
            """)
            page.wait_for_timeout(1000)
        except Exception as e:
            self.log(f"⚠️ Erro no scroll: {e}")


def get_site_name(url):
    """Extract a clean site name from URL for the zip filename"""
    parsed = urlparse(url)
    # Get domain without www
    domain = parsed.netloc.replace('www.', '')
    # Clean special characters
    clean_name = re.sub(r'[^a-zA-Z0-9.-]', '_', domain)
    # Add path info if present (cleaned)
    if parsed.path and parsed.path != '/':
        path_part = re.sub(r'[^a-zA-Z0-9]', '_', parsed.path.strip('/'))[:30]
        clean_name = f"{clean_name}_{path_part}"
    return clean_name


def zip_directory(folder_path, output_path):
    """Create a zip file from a directory"""
    base_name = output_path.replace('.zip', '')
    shutil.make_archive(base_name, 'zip', folder_path)
    return base_name + '.zip'
