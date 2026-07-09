"""
Verifica se as chaves de IA configuradas (Gemini/Anthropic) estão válidas.

Uso:
    uv run python check_ai_keys.py
"""
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

import ai_provider


def _mask(key):
    if len(key) <= 8:
        return '*' * len(key)
    return f"{key[:4]}...{key[-4:]}"


def main():
    print(f"Modelo Gemini configurado (GEMINI_MODEL): {ai_provider.GEMINI_MODEL}")
    print(f"Modelo Anthropic configurado (ANTHROPIC_MODEL): {ai_provider.ANTHROPIC_MODEL}")
    print()

    keys = ai_provider.gemini_keys()
    if not keys:
        print("Gemini: nenhuma chave configurada (defina GEMINI_API_KEYS, separadas por vírgula, ou GEMINI_API_KEY)")
    else:
        print(f"Gemini: {len(keys)} chave(s) configurada(s)")
        for i, key in enumerate(keys, start=1):
            ok, message = ai_provider.validate_gemini_key(key)
            status = "✅" if ok else "❌"
            print(f"  {status} Chave #{i} ({_mask(key)}): {message}")

    print()
    if ai_provider.anthropic_available():
        print("Anthropic: ANTHROPIC_API_KEY configurada (não validada nesse script — sem checagem leve de modelo/chave na API)")
    else:
        print("Anthropic: não configurada")

    print()
    provider = ai_provider.active_provider()
    print(f"Provedor ativo nas gerações (Gemini tem prioridade por ser gratuito): {provider or 'NENHUM — configure ao menos uma chave'}")


if __name__ == '__main__':
    main()
