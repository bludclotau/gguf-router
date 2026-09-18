import re


def clean_output(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[User\]|\[Assistant\]|\[grounding.*?\]", "", text, flags=re.I)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.S | re.I)
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"\{.*?system_fingerprint.*?\}", "", text, flags=re.S)
    text = text.strip()
    if "." in text:
        text = text.rsplit(".", 1)[0] + "."
    return text
