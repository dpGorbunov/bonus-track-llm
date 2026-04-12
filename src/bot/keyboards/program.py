from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def program_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Профиль", callback_data="cmd:profile"),
            InlineKeyboardButton(text="Если успеете", callback_data="cmd:if_time"),
        ],
    ])


def detail_keyboard(project_rank: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Назад", callback_data="cmd:back"),
            InlineKeyboardButton(text="Вопросы к проекту", callback_data=f"questions:{project_rank}"),
        ],
    ])


def confirm_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Все верно", callback_data="profile:confirm"),
            InlineKeyboardButton(text="Заново", callback_data="profile:retry"),
        ],
    ])


def project_buttons_keyboard(
    project_list: list[tuple[int, str]],
    include_pdf: bool = True,
) -> InlineKeyboardMarkup:
    """Inline buttons for each recommended project."""
    buttons: list[list[InlineKeyboardButton]] = []
    for rank, title in project_list:
        label = f"#{rank} {title}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"project:{rank}")])

    bottom = [
        InlineKeyboardButton(text="Профиль", callback_data="cmd:profile"),
        InlineKeyboardButton(text="Если успеете", callback_data="cmd:if_time"),
    ]
    buttons.append(bottom)

    if include_pdf:
        buttons.append([InlineKeyboardButton(text="Скачать PDF", callback_data="cmd:export_pdf")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def support_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад к программе", callback_data="support:back")],
    ])
