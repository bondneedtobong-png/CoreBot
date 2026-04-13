"""FSM-состояния для сценариев управления аккаунтами."""
from aiogram.fsm.state import State, StatesGroup


class AccountUpload(StatesGroup):
    """Загрузка Tdata: ZIP → прокси → название в списке → конвертация."""
    waiting_for_file = State()
    waiting_for_proxy = State()
    waiting_for_list_label = State()
    processing = State()


class EditProfileFSM(StatesGroup):
    """Редактирование текстовых полей профиля (Telethon)."""
    waiting_for_name = State()
    waiting_for_bio = State()
    waiting_for_username = State()


class AccountPhotoManagement(StatesGroup):
    """Управление фото профиля userbot-аккаунта."""
    waiting_for_new_photo = State()
    waiting_for_photo_number_to_delete = State()


class Set2FAFSM(StatesGroup):
    """Установка 2FA через WorkerManager."""
    waiting_for_password = State()
    waiting_for_password_confirm = State()


class EditTagsFSM(StatesGroup):
    """Редактирование тегов аккаунта в БД."""
    waiting_for_tags = State()


class GroupManageFSM(StatesGroup):
    """Создание группы аккаунтов по названию."""
    waiting_for_group_name = State()


class GroupBulk2FAFSM(StatesGroup):
    """Массовая установка одного пароля 2FA для всех аккаунтов в группе."""
    waiting_for_password = State()
    waiting_for_password_confirm = State()


class GroupBulkProfileFSM(StatesGroup):
    """Массовое редактирование профиля по группе: отдельные поля."""
    waiting_for_name_template = State()
    waiting_for_username_template = State()
    waiting_for_bio_template = State()
    waiting_for_photo = State()


class AccountListLabelFSM(StatesGroup):
    """Локальная подпись аккаунта в списке бота."""
    waiting_for_label = State()
