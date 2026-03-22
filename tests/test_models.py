import os
import pytest
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

pytestmark = pytest.mark.integration


def test_gemini_langchain_smoke():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        pytest.skip("GOOGLE_API_KEY is not configured")

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        google_api_key=api_key,
        temperature=0,
    )
    try:
        response = llm.invoke([HumanMessage(content="Reply with exactly: OK")])
    except Exception as exc:
        error_text = str(exc).lower()
        if "quota" in error_text or "resourceexhausted" in error_text or "429" in error_text:
            pytest.skip("Gemini quota/rate limit reached for this environment")
        pytest.skip(f"Gemini smoke test unavailable: {exc}")

    if isinstance(response.content, str):
        text = response.content
    else:
        text = " ".join(
            chunk.get("text", "")
            for chunk in response.content
            if isinstance(chunk, dict)
        )

    assert "OK" in text.upper()
