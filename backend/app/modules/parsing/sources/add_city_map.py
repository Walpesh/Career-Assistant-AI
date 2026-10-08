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

new = (
    '__all__ = ["HHAdapter"]' + CRLF + CRLF
    + '#: Карта городов для фильтрации через hh.ru area (docs/04 §4.1).' + CRLF
    + '#: Обновляется с https://github.com/hhru/api (список territory_id).' + CRLF
    + '_CITY_AREA_MAP: dict[str, int] = {' + CRLF
    + '    "Москва": 1,' + CRLF
    + '    "Московская область": 1,' + CRLF
    + '    "Санкт-Петербург": 2,' + CRLF
    + '    "Ленинградская область": 2,' + CRLF
    + '    "Новосибирск": 64,' + CRLF
    + '    "Новосибирская область": 64,' + CRLF
    + '    "Екатеринбург": 304,' + CRLF
    + '    "Свердловская область": 304,' + CRLF
    + '    "Казань": 420,' + CRLF
    + '    "Республика Татарстан": 420,' + CRLF
    + '    "Нижний Новгород": 50,' + CRLF
    + '    "Нижегородская область": 50,' + CRLF
    + '    "Челябинск": 79,' + CRLF
    + '    "Челябинская область": 79,' + CRLF
    + '    "Омск": 158,' + CRLF
    + '    "Омская область": 158,' + CRLF
    + '    "Самара": 242,' + CRLF
    + '    "Самарская область": 242,' + CRLF
    + '    "Уфа": 364,' + CRLF
    + '    "Республика Башкортостан": 364,' + CRLF
    + '    "Красноярск": 164,' + CRLF
    + '    "Красноярский край": 164,' + CRLF
    + '    "Воронеж": 66,' + CRLF
    + '    "Воронежская область": 66,' + CRLF
    + '    "Пермь": 294,' + CRLF
    + '    "Пермский край": 294,' + CRLF
    + '    "Волгоград": 65,' + CRLF
    + '    "Волгоградская область": 65,' + CRLF
    + '    "Калининград": 307,' + CRLF
    + '    "Калининградская область": 307,' + CRLF
    + '    "Ростов-на-Дону": 23,' + CRLF
    + '    "Ростовская область": 23,' + CRLF
    + '    "Иркутск": 218,' + CRLF
    + '    "Иркутская область": 218,' + CRLF
    + '    "Томск": 136,' + CRLF
    + '    "Томская область": 136,' + CRLF
    + '    "Тюмень": 88,' + CRLF
    + '    "Тюменская область": 88,' + CRLF
    + '    "Хабаровск": 51,' + CRLF
    + '    "Хабаровский край": 51,' + CRLF
    + '    "Ярославль": 53,' + CRLF
    + '    "Ярославская область": 53,' + CRLF
    + '    "Барнаул": 112,' + CRLF
    + '    "Алтайский край": 112,' + CRLF
    + '    "Кемерово": 151,' + CRLF
    + '    "Кемеровская область": 151,' + CRLF
    + '    "Нижний Тагил": 49,' + CRLF
    + '    "Серов": 153,' + CRLF
    + '    "Магнитогорск": 208,' + CRLF
    + '    "Новокузнецк": 192,' + CRLF
    + '    "Орел": 148,' + CRLF
    + '    "Орловская область": 148,' + CRLF
    + '    "Саратов": 154,' + CRLF
    + '    "Саратовская область": 154,' + CRLF
    + '    "Сургут": 139,' + CRLF
    + '    "Ханты-Мансийский АО": 139,' + CRLF
    + '    "Тольятти": 176,' + CRLF
    + '    "Белгород": 130,' + CRLF
    + '    "Белгородская область": 130,' + CRLF
    + '    "Липецк": 68,' + CRLF
    + '    "Липецкая область": 68,' + CRLF
    + '    "Вологда": 100,' + CRLF
    + '    "Вологодская область": 100,' + CRLF
    + '    "Псков": 803,' + CRLF
    + '    "Псковская область": 803,' + CRLF
    + '    "Иваново": 60,' + CRLF
    + '    "Ивановская область": 60,' + CRLF
    + '    "Калуга": 61,' + CRLF
    + '    "Калужская область": 61,' + CRLF
    + '    "Кострома": 63,' + CRLF
    + '    "Костромская область": 63,' + CRLF
    + '    "Тверь": 67,' + CRLF
    + '    "Тверская область": 67,' + CRLF
    + '    "Рязань": 74,' + CRLF
    + '    "Рязанская область": 74,' + CRLF
    + '    "Грозный": 210,' + CRLF
    + '    "Махачкала": 410,' + CRLF
    + '    "Макхачкала": 410,' + CRLF
    + '}' + CRLF + CRLF + CRLF
    + '#: Статусы «вакансия удалена на hh.ru» (docs/04 §5 → get_vacancy вернёт None).' + CRLF
    + '_NOT_FOUND_STATUSES = (404, 410)' + CRLF + CRLF + CRLF
    + 'class HHAdapter(BaseSourceAdapter):'
)

if old in content:
    content = content.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("OK")
else:
    print("NOT FOUND")