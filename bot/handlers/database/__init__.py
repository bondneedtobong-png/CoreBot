from aiogram import Router

from bot.handlers.database.crm_tools import router as database_crm_tools_router
from bot.handlers.database.exports_delete import router as database_exports_delete_router
from bot.handlers.database.menu import router as database_menu_router
from bot.handlers.database.sheet_212 import router as database_sheet_212_router
from bot.handlers.database.sheet_new import router as database_sheet_new_router

database_router = Router()
database_router.include_router(database_menu_router)
database_router.include_router(database_exports_delete_router)
database_router.include_router(database_crm_tools_router)
database_router.include_router(database_sheet_new_router)
database_router.include_router(database_sheet_212_router)

__all__ = ("database_router",)
