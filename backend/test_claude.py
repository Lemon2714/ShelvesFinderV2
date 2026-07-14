"""
Quick Claude API diagnostic — run from the backend folder:
  python test_claude.py
"""
import os, sys
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY", "")
print(f"\n{'='*55}")
print("  Claude API Diagnostic")
print(f"{'='*55}")
print(f"  API key present : {'YES' if api_key else 'NO'}")
if api_key:
    print(f"  Key prefix      : {api_key[:20]}...")
    print(f"  Key length      : {len(api_key)} chars")
print()

if not api_key:
    print("❌  ANTHROPIC_API_KEY is not set in .env")
    sys.exit(1)

try:
    import anthropic
    print(f"  anthropic pkg   : {anthropic.__version__}")
except ImportError:
    print("❌  anthropic package not installed — run: pip install anthropic")
    sys.exit(1)

client = anthropic.Anthropic(api_key=api_key)

models_to_test = [
    "claude-3-haiku-20240307",
    "claude-3-sonnet-20240229",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]

print("  Testing models:")
print(f"  {'-'*45}")
working = []
for model in models_to_test:
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=10,
            messages=[{"role": "user", "content": "Say: ok"}],
        )
        print(f"  ✅  {model}")
        working.append(model)
    except anthropic.NotFoundError:
        print(f"  ❌  {model}  (not found / no access)")
    except anthropic.AuthenticationError:
        print(f"  🔑  {model}  (authentication failed)")
        break
    except Exception as e:
        print(f"  ⚠️   {model}  ({type(e).__name__}: {e})")

print(f"\n{'='*55}")
if working:
    print(f"  Working models: {working}")
    print(f"\n  Add to your .env:")
    print(f"  CLAUDE_CHAT_MODEL={working[0]}")
    print(f"  CLAUDE_ORCHESTRATOR_MODEL={working[-1]}")
else:
    print("  No models accessible with this API key.")
    print("  Possible causes:")
    print("  1. Key has no model permissions (check console.anthropic.com)")
    print("  2. Key is for Bedrock/Vertex AI, not direct Anthropic API")
    print("  3. Account has no active credits/subscription")
print(f"{'='*55}\n")
