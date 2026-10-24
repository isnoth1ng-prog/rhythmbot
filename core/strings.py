import random

COMMAND_PREFIX = ""

STATUS_SEARCHING = "Ищу трек…"
ERROR_PREFIX = "Ошибка: "
ERROR_LONG_AUDIO = "Трек длиннее 15 минут."
ERROR_SEARCH_ALL_TOO_LONG = "Все найденные треки длиннее 15 минут."
ERROR_TOO_LARGE = "Файл больше {} МБ.".format(50)
ERROR_NO_RESULTS = "Ничего не найдено."

TOO_FAST_CALLBACK = ""

BUTTON_REQUESTER = "{}"
BUTTON_NOT_RIGHT = "Другой вариант"
BUTTON_CANCEL = "Отмена"
UNTITLED_SONG = "Без названия"
UNKNOWN_VALUE = "неизвестно"
NOT_FOR_YOU = "Эта кнопка не для тебя."
SONG_UPDATED = "Трек обновлён."
INFO_EXPIRED = "Информация устарела."
FAILED_TO_UPDATE = "Не удалось обновить сообщение: {}"


def get_song_info_message(data, views, likes, dislikes):
    year = data.get("upload_date", "")
    year = year[:4] if year else UNKNOWN_VALUE
    return (
        f"Исполнитель: {data.get('artist', UNKNOWN_VALUE)}\n"
        f"Год: {year}\n"
        f"Прослушивания: {views}\n"
        f"Нравится: {likes} | Не нравится: {dislikes}"
    )
