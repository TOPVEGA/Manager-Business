import html
import aiohttp
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Union


class ButtonStyle(str, Enum):
    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"


@dataclass
class RichMessageButton:
    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None
    style: ButtonStyle = ButtonStyle.PRIMARY


@dataclass
class RichMessageRow:
    buttons: List[RichMessageButton] = field(default_factory=list)
    center: bool = True


def _style_to_str(style) -> str:
    if not style:
        return "primary"
    if style == ButtonStyle.SUCCESS or style == "success":
        return "success"
    if style == ButtonStyle.DANGER or style == "danger":
        return "danger"
    return "primary"


def _btn_to_tag(btn: RichMessageButton) -> str:
    label = html.escape(str(btn.text), quote=False)
    style = _style_to_str(btn.style)
    if btn.callback_data is not None:
        data = html.escape(str(btn.callback_data), quote=True)
        return f'<tg-button type="callback_data" style="{style}" data="{data}">{label}</tg-button>'
    if btn.url:
        url = html.escape(str(btn.url), quote=True)
        return f'<tg-button type="url" style="{style}" url="{url}">{label}</tg-button>'
    return ""


async def send_rich(
    bot_token: str,
    chat_id: int,
    blocks: List[Union[str, RichMessageRow, Dict[str, Any]]],
    reply_to: Optional[int] = None,
    edit_message_id: Optional[int] = None,
    is_rtl: bool = True,
) -> Optional[dict]:
    text_parts: List[str] = []
    button_rows_html = ""

    for block in blocks:
        if isinstance(block, RichMessageRow):
            tags = "".join(_btn_to_tag(b) for b in block.buttons)
            if tags:
                align = "center" if block.center else "left"
                button_rows_html += f'<tg-button-row align="{align}">{tags}</tg-button-row>'
            continue

        if isinstance(block, str):
            text_parts.append(block.replace("\n", "<br/>"))
            continue

        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")).replace("\n", "<br/>"))
            elif btype == "buttons":
                tags = "".join(_btn_to_tag(b) for b in block.get("buttons", []))
                if tags:
                    align = "center" if block.get("center", True) else "left"
                    button_rows_html += f'<tg-button-row align="{align}">{tags}</tg-button-row>'

    full_html = "".join(text_parts) + button_rows_html
    if not full_html.strip():
        full_html = "ㅤ"

    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": full_html, "is_rtl": is_rtl},
    }
    method = "sendRichMessage"

    if edit_message_id:
        payload["message_id"] = edit_message_id
        method = "editMessageText"
    elif reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/{method}", json=payload
            ) as resp:
                res = await resp.json(content_type=None)
                if res.get("ok"):
                    return res.get("result")
                print(f"[send_rich] فشل {method}: {res.get('description')}")
                return None
    except Exception as e:
        print(f"[send_rich] استثناء: {e}")
        return None
