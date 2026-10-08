path = "c:/Users/walpe/OneDrive/Desktop/Career-Assistant-AI-v2/backend/app/modules/parsing/sources/hh.py"
with open(path, "rb") as f:
    content = f.read()
idx = content.find(b"__all__ = [")
print("Found at", idx)
print(repr(content[idx:idx + 200]))