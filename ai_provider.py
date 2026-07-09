import os
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
import anthropic

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5')


class AIGenerationError(Exception):
    pass


def gemini_keys():
    """Lê as chaves Gemini configuradas. Aceita GEMINI_API_KEYS (separadas por vírgula,
    uma por conta) ou GEMINI_API_KEY (uma única chave)."""
    raw = os.environ.get('GEMINI_API_KEYS') or os.environ.get('GEMINI_API_KEY') or ''
    return [k.strip() for k in raw.split(',') if k.strip()]


def gemini_available():
    return len(gemini_keys()) > 0


def anthropic_available():
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def is_available():
    return gemini_available() or anthropic_available()


def active_provider():
    """Gemini tem prioridade por ser gratuito; Anthropic é usado como fallback."""
    if gemini_available():
        return 'gemini'
    if anthropic_available():
        return 'anthropic'
    return None


def _is_rate_limit_error(e):
    code = getattr(e, 'code', None)
    if code == 429:
        return True
    return '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e).upper()


def validate_gemini_key(api_key, model=None):
    """Verifica se uma chave Gemini específica consegue acessar o modelo configurado.
    Retorna (ok: bool, message: str). Não gasta cota de geração (só metadados do modelo)."""
    model = model or GEMINI_MODEL
    try:
        client = genai.Client(api_key=api_key)
        client.models.get(model=model)
        return True, f"Chave válida para o modelo '{model}'"
    except genai_errors.APIError as e:
        return False, f"[{e.code}] {e}"
    except Exception as e:
        return False, str(e)


def _generate_gemini(system_prompt, user_content, max_tokens, log_fn):
    keys = gemini_keys()
    if not keys:
        raise AIGenerationError("Nenhuma GEMINI_API_KEY/GEMINI_API_KEYS configurada")

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=max_tokens,
    )

    last_error = None
    for i, key in enumerate(keys):
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_content,
                config=config,
            )
            try:
                finish_reason = response.candidates[0].finish_reason
                if finish_reason and 'MAX_TOKENS' in str(finish_reason):
                    log_fn("⚠️ Resposta truncada por limite de tokens (Gemini)")
            except (IndexError, AttributeError):
                pass

            text = (response.text or '').strip()
            if not text:
                raise AIGenerationError("Resposta vazia da API Gemini")
            return text
        except Exception as e:
            last_error = e
            is_last_key = i == len(keys) - 1
            if _is_rate_limit_error(e):
                log_fn(f"⚠️ Chave Gemini #{i + 1}/{len(keys)} atingiu o limite de uso" + ("" if is_last_key else " — tentando próxima chave..."))
            else:
                log_fn(f"⚠️ Erro com a chave Gemini #{i + 1}/{len(keys)}: {e}" + ("" if is_last_key else " — tentando próxima chave..."))
            if not is_last_key:
                continue
            raise AIGenerationError(f"Falha ao chamar API Gemini (todas as {len(keys)} chave(s) falharam): {last_error}")


def _generate_anthropic(system_prompt, user_content, max_tokens, log_fn):
    client = anthropic.Anthropic()
    try:
        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        raise AIGenerationError(f"Falha ao chamar API Anthropic: {e}")

    if message.stop_reason == 'max_tokens':
        log_fn("⚠️ Resposta truncada por limite de tokens (Anthropic)")

    text = "".join(block.text for block in message.content if getattr(block, 'type', None) == 'text').strip()
    if not text:
        raise AIGenerationError("Resposta vazia da API Anthropic")
    return text


def generate_text(system_prompt, user_content, max_tokens=16000, log_callback=None):
    """Gera texto usando o provedor de IA configurado. Gemini tem prioridade (gratuito);
    se todas as chaves Gemini falharem e Anthropic também estiver configurada, cai para
    Anthropic como fallback automático."""
    log = log_callback or (lambda m: None)
    provider = active_provider()
    if provider is None:
        raise AIGenerationError(
            "Nenhum provedor de IA configurado (defina GEMINI_API_KEYS/GEMINI_API_KEY ou ANTHROPIC_API_KEY)"
        )

    if provider == 'gemini':
        try:
            return _generate_gemini(system_prompt, user_content, max_tokens, log)
        except AIGenerationError as e:
            if anthropic_available():
                log(f"⚠️ Gemini indisponível ({e}) — usando Anthropic como fallback...")
                return _generate_anthropic(system_prompt, user_content, max_tokens, log)
            raise

    return _generate_anthropic(system_prompt, user_content, max_tokens, log)
