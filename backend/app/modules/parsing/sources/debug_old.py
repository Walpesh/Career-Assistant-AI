path = "c:/Users/walpe/OneDrive/Desktop/Career-Assistant-AI-v2/backend/app/modules/parsing/sources/hh.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

CRLF = "\r\n"

# File structure:
# __all__ = ["HHAdapter"]
# (1 blank line)
# #: comment
# (2 blank lines)
# _NOT_FOUND_STATUSES = (404, 410)
# (2 blank lines)
# class HHAdapter(BaseSourceAdapter):
old = (
    '__all__ = ["HHAdapter"]' + CRLF + CRLF
    + '#: Статусы «вакансия удалена на hh.ru» (docs/04 §5 → get_vacancy вернёт None).' + CRLF
    + '_NOT_FOUND_STATUSES = (404, 410)' + CRLF + CRLF + CRLF
    + 'class HHAdapter(BaseSourceAdapter):'
)

# Create debug output
debug_path = "c:/Users/walpe/OneDrive/Desktop/Career-Assistant-AI-v2/backend/app/modules/parsing/sources/debug_old.txt"
with open(debug_path, "w", encoding="utf-8") as f:
    f.write("OLD STRING:\n")
    f.write(repr(old))
    f.write("\n\n")
    f.write("CONTENT around __all__:\n")
    idx = content.find('__all__ = ["HHAdapter"]')
    f.write(repr(content[idx:idx + 300]))

print("OLD:", repr(old[:100]))
print("CONTENT snippet:", repr(content[1365:1600]))